# -*- coding: utf-8 -*-
"""微知 v2 · 影子生产任务（每天生成候选，但不进用户的推送）。

**为什么需要影子模式**：方案第 10 节明确「在实验完成前，候选内容继续保留
人工发布门槛」。直接让 v2 接管每日推送，等于跳过全部验证；而完全不跑，
又拿不到任何可以评估的东西。影子模式是这两者之间的唯一合理位置：

    每天跑 v2 → 桥接成 v1 卡片 → 标记 shadow → 用户能在历史里看到
                                              ↓
                              不进荐读、不进自动修复、不参与质检

三条隔离由 `db.is_shadow_card` 统一实现（daily_agent / daily_check 都已接）。
这么做的理由：评审阶段要看的是 **v2 的原始产出**。如果让 v1 的自愈 Agent
去修改它，等于把要评估的东西先改了一遍，评估结果就不可信了。

**失败处理**：任何一步失败都只记日志并跳过当天，绝不让 v2 的故障影响
v1 的正常生产。抓取、模型、图片任一环节挂了，v1 照常跑。

用法::

    python v2_shadow.py                # 生成一轮
    python v2_shadow.py --dry-run      # 只走链路不落库
    python v2_shadow.py --limit 2      # 一轮最多产出几张
"""
import argparse
import json
import os
import socket
import sys
import traceback
from datetime import datetime

import db
import evidence
import schema_v2

LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "v2_shadow.log")

# 影子卡先挂在一个稳定的 demo 目标下。等 M6 的「目标创建」前端上线后，
# 改为读取用户真实 active 目标——那时它才有能力相关性可算。
DEFAULT_GOAL = {
    "key": "shadow",
    "capability": "能说清材料讲的核心机制，并判断它适用到什么边界",
    "level": "有基础但不系统",
    "scene": "读完能在自己的项目里判断要不要用",
    "success_evidence": "能不查资料复述核心机制并说出一个不适用场景",
    "prereq": ["基本技术背景"],
    "milestones": ["复述核心机制", "举出一个不适用场景"],
    "daily_minutes": 60,
}


def log(msg):
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
    try:
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError:
        pass


def _config():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    if not os.path.exists(path):
        return {}
    return json.load(open(path, encoding="utf-8"))


def _provider(cfg):
    key = cfg.get("deepseek_api_key") or ""
    if not key or key.startswith("sk-你的"):
        return None
    from providers import DeepSeekProvider
    return DeepSeekProvider(api_key=key, timeout=180)


def fetch_materials(cfg, want=1, min_chars=1200):
    """从已配置信源取最新的、能抓全正文的几篇。

    只取不存——入库由 evidence.ingest_source 负责，这里不重复落盘逻辑。
    """
    import feedparser
    import trafilatura

    socket.setdefaulttimeout(30)
    out = []
    for src in (cfg.get("sources") or []):
        if len(out) >= want:
            break
        try:
            feed = feedparser.parse(src.get("rss"))
        except Exception as exc:  # noqa: BLE001
            log("信源解析失败 %s: %s" % (src.get("name"), exc))
            continue
        for entry in (feed.entries or [])[:5]:
            if len(out) >= want:
                break
            try:
                raw = trafilatura.fetch_url(entry.link)
                text = trafilatura.extract(raw, include_comments=False) if raw else None
            except Exception as exc:  # noqa: BLE001
                log("抓取失败 %s: %s" % (entry.link[:60], exc))
                continue
            if not text or len(text) < min_chars:
                continue
            out.append({"title": entry.title, "url": entry.link, "text": text,
                        "site": src.get("name"), "source_tier": "blog",
                        "kind": "evolving"})
    return out


def produce_one(provider, material, dry_run=False):
    """对一份材料跑完整 v2 链路并桥接入库。返回摘要 dict。"""
    import assessment
    import bridge_v1
    import card_writer
    import visual

    sid, claims = evidence.ingest_source(
        material.get("url") or "shadow:material", material["text"],
        title=material.get("title"))
    if len(claims) < evidence.MIN_CLAIMS_FOR_PACK:
        return {"ok": False, "why": "证据不足（%d 条）" % len(claims)}

    goal = schema_v2.make_goal(**DEFAULT_GOAL)
    draft, draft_id, report = card_writer.write_card_gated(
        provider, goal, claims,
        source={"title": material.get("title"), "url": material.get("url")},
        source_id=sid)
    if not report["passed"]:
        return {"ok": False, "why": "v2 门禁未过：%s" % report["issues"],
                "draft_id": draft_id}

    items, qreport = assessment.generate(provider, draft, claims,
                                         concept="m1", draft_id=draft_id)

    figures = []
    try:
        figures = visual.plan_visuals(provider, draft, claims)
        visual.attach(draft_id, [{k: v for k, v in f.items() if k != "_svg"}
                                 for f in figures])
    except Exception as exc:  # noqa: BLE001 - 配图失败不该作废整张卡
        log("配图失败（不影响出卡）: %s" % exc)

    card = bridge_v1.from_draft(
        draft_id, items=items, material=material, provider=provider,
        figures=figures, shadow=True)

    conflicts = bridge_v1.check_v1_compat(card)
    ok, issues = bridge_v1.publish_gate(card)
    if not ok:
        # 桥接卡过不了 v1 门禁就不再往下走：宁可不出，也不要污染卡片库
        return {"ok": False, "why": "v1 门禁未过：%s" % issues, "draft_id": draft_id}

    if dry_run:
        return {"ok": True, "dry_run": True, "title": card.get("title"),
                "draft_id": draft_id, "conflicts": conflicts,
                "questions": len(card["quiz"]) + len(card["review_quiz"])}

    saved = bridge_v1.save(card)
    return {"ok": bool(saved), "title": card.get("title"), "draft_id": draft_id,
            "saved": bool(saved), "conflicts": conflicts,
            "questions": len(card["quiz"]) + len(card["review_quiz"]),
            "figures": len(figures), "gate_passed": ok}


def run(limit=1, dry_run=False):
    db.init_db()
    cfg = _config()
    provider = _provider(cfg)
    if provider is None:
        log("没有可用的 deepseek_api_key，跳过本轮")
        return {"ok": False, "why": "no_api_key"}

    materials = fetch_materials(cfg, want=limit)
    if not materials:
        log("没有抓到可用材料，跳过本轮（属正常情况）")
        return {"ok": True, "produced": [], "skipped": "no_material"}

    produced = []
    for mat in materials:
        try:
            r = produce_one(provider, mat, dry_run=dry_run)
        except Exception:  # noqa: BLE001 - 单篇失败不影响其它，也不影响 v1
            log("生成异常：\n%s" % traceback.format_exc())
            r = {"ok": False, "why": "exception", "title": mat.get("title")}
        r["material"] = mat.get("title")
        produced.append(r)
        log("材料《%s》→ %s" % (mat.get("title"), "成功" if r.get("ok") else "失败：%s" % r.get("why")))
    return {"ok": True, "produced": produced}


def main(argv=None):
    ap = argparse.ArgumentParser(description="微知 v2 影子生产（不进用户推送）")
    ap.add_argument("--limit", type=int, default=1, help="一轮最多产出几张")
    ap.add_argument("--dry-run", action="store_true", help="只走链路不落库")
    ap.add_argument("--refresh", action="store_true",
                    help="不生成新卡，只按当前桥接规则刷新已入库的 v2 卡"
                         "（配图等派生字段）。桥接规则改动后用，不耗费模型调用。")
    args = ap.parse_args(argv)

    if args.refresh:
        import bridge_v1
        result = bridge_v1.refresh(limit=max(args.limit, 200), dry_run=args.dry_run)
        log("刷新完成：检查 %d 条，更新 %d 条，未命中 %d 条，跳过 %d 条"
            % (result["checked"], len(result["updated"]),
               len(result["missing"]), len(result["skipped"])))
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0

    result = run(limit=args.limit, dry_run=args.dry_run)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
