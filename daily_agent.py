#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""微知 · M3 编排层（管家 Agent 的「思考」环节）

cron 3:35 跑（daily_check 3:30 之后），按 ReAct 循环落地：

1. Observation  读最新质检报告 + 昨日对比
2. 规则信号层    待拍板 badcase / 复习拖欠 / 断签 / 学习下滑（确定性信号）
3. Thought      DeepSeek 读信号 + 报告，产出「今日通知 + 自动修复清单」（JSON，失败→规则兜底）
4. Action       执行自动修复（带备份可回滚）+ 写入程序内通知（前端铃铛/弹窗弹出）
5. 汇报         通知里明确「哪些自动做了、哪些需要你拍板」

设计原则（延续 M3 安全子集）：
- 自动修复仅限「评分 2-3 分」区间（≤2 分已由 daily_check 自动修），每轮 ≤3 张、同卡备份 ≥2 次转人工
- 所有自动动作可回滚（备份文件）
- 通知最多 4 条/天，避免打扰

用法：python daily_agent.py [--dry-run]
"""
import argparse
import json
import os
import traceback
from datetime import datetime, timedelta

from openai import OpenAI

import db
import notifications

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPORTS_DIR = os.path.join(BASE_DIR, "quality_reports")
BACKUP_DIR = os.path.join(REPORTS_DIR, "backups")
DEEPSEEK_BASE_URL = "https://api.deepseek.com"

AGENT_SYSTEM = """你是一位「私人学习管家 Agent」，负责替用户打理他的 AI 从业者学习系统。
你每天会收到系统自动生成的「质量巡检报告」「规则信号」和「今日新卡候选」，你的任务：
1. 用一句话概括今天的学习系统状态（为用户写每日简报）。
2. **从今日新卡里挑出最值得用户读的 Top 3**，每条给一句「为什么值得读」（学 The Batch 的做法：不只报有什么，要讲为什么重要）——这是你的核心策展职责。
3. 决定今天要提醒用户什么（最多 3 条，去掉废话）：复习拖欠、断签预警、库内过时卡等。
4. 从「可自动修复清单」里挑选值得自动重生成的卡（最多 3 张）——它们质量分在 2-3 分之间，字段合格但内容欠佳。

语气：像一位负责的管家，简洁、具体、可执行，不啰嗦。中文输出。

铁律：
1. 只输出合法的 JSON，不要输出 JSON 以外的文字。
2. regen_candidates 只能从输入的【可修订清单】里选（source_url 必须原样匹配）。
   它们只会被**提出为修订候选**，不会自动改动卡片——采纳与否由用户在审核界面决定。
   所以不要在通知里提它们（那是审核界面的事）。
3. recommendations 只能从【今日新卡候选】里挑（source_url 必须原样匹配），最多 3 条；候选不足 3 条就少给，不要硬凑。
4. notifications 的 type **只能是 review_due**。其余类型一律不要发，发了也会被拦下：
   - 不发每日摘要/荐读推送/断签提醒/过时卡提醒/周报——这些都被明确取消了
   - badcase_pending 与 system_failure 由系统按规则生成，你不用管
5. 没有拖欠复习就不要发 review_due。**宁可一条都不发，也不要凑一条通知出来。**
   通知只该在"用户必须做点什么"时出现。
"""

AGENT_USER = """【今日质检报告】
{report}

【规则信号】
{signals}

【今日新卡候选（从这里挑 recommendations 的 Top 3）】
{picks}

【可修订清单（评分 2-3 分，可从这里挑 regen_candidates，只会生成候选）】
{regenable}

输出 JSON（字段名必须一致）：
{{
  "summary": "今日状态一句话（30字内）",
  "notifications": [
    {{
      "type": "review_due",
      "title": "通知标题，15字内",
      "body": "通知正文，60字内",
      "level": "warn"
    }}
  ],
  "recommendations": [
    {{
      "source_url": "必须是候选里的 source_url",
      "title": "卡标题",
      "why": "为什么值得读，30字内"
    }}
  ],
  "regen_candidates": ["source_url", "..."],
  "reason": "你的决策依据，40字内"
}}

要求：
1. 有值得读的新卡 → recommendations 给 Top 3（附 why）；没有好卡就不给（不要硬凑）。
   （recommendations 不再推送，只作为 App 内的发现层数据。）
2. 复习拖欠（due>0 且完成率<60%）→ 发一条 review_due，level=warn。
3. notifications 数组可以为空。**发通知的门槛是"用户必须做点什么"，不是"有话要说"。**
"""


def load_config():
    """从 config.json 读取配置，失败返回空 dict。"""
    path = os.path.join(BASE_DIR, "config.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def latest_report():
    """读最新质检报告，无则 None。"""
    if not os.path.isdir(REPORTS_DIR):
        return None
    try:
        files = sorted(f for f in os.listdir(REPORTS_DIR) if f.endswith(".json"))
    except OSError:
        return None
    if not files:
        return None
    try:
        with open(os.path.join(REPORTS_DIR, files[-1]), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def yesterday_report(report):
    """读昨天报告（report['date'] 前一天），用于趋势对比。"""
    try:
        d = datetime.strptime(report["date"], "%Y-%m-%d") - timedelta(days=1)
    except (KeyError, ValueError):
        return None
    p = os.path.join(REPORTS_DIR, d.strftime("%Y-%m-%d") + ".json")
    if not os.path.isfile(p):
        return None
    try:
        with open(p, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def rule_signals(report, y_report):
    """确定性规则信号：待拍板 badcase / 复习拖欠 / 断签 / 下滑 / 过时卡。"""
    badcases = report.get("badcases") or []
    metrics = report.get("metrics") or {}
    # 待拍板：评分 2-3 的 AI badcase + 自动修复失败的
    pending = []
    for b in badcases:
        if b.get("source") == "ai" and b.get("score") is not None and 2 < b["score"] <= 3:
            pending.append(b)
    # 待审修订候选也算"需要你审核"——它们不会自己生效，堆着就是待办
    import revisions
    for rv in revisions.pending(limit=50):
        pending.append({
            "source_url": rv["source_url"], "title": rv.get("title") or "",
            "score": None,
            "reason": "有修订候选待审：%s" % (rv.get("reason") or "巡检发现问题"),
            "studied": rv.get("studied"),
        })
    # 复习拖欠
    due = metrics.get("due_today") or 0
    compliance = metrics.get("compliance_today")
    review_due = bool(due > 0 and (compliance is None or compliance < 0.6))
    # 学习量（只作为背景信息给模型看，不据此发通知）
    #
    # 断签/下滑信号已移除（范围决策）：它们唯一的用途是生成 streak_warn，
    # 而「断签焦虑」被明确取消。用连续天数施压会把学习变成打卡。
    study_today = metrics.get("study_today") or 0
    study_y = (y_report.get("metrics") or {}).get("study_today") or 0 if y_report else None
    # 过时卡（C2）：fast >180 天 / event >14 天，未掌握且未读的
    stale = []
    for c in db.load_cards(None):
        t = c.get("timeliness")
        if t == "trending":
            t = "evolving"
        pub = c.get("published")
        if not pub:
            continue
        try:
            days = (datetime.now() - datetime.strptime(str(pub)[:10], "%Y-%m-%d")).days
        except ValueError:
            continue
        if (t == "fast" and days > 180) or (t == "event" and days > 14):
            stale.append({"source_url": c.get("source_url"), "title": c.get("title") or "",
                          "published": str(pub)[:10], "days": days})
    return {
        "pending": pending,
        "review_due": {"due": due, "compliance": compliance},
        "study": {"today": study_today, "yesterday": study_y},
        "stale": stale,
        "weak": db.weak_cards(limit=5),  # 学习画像：薄弱卡
        "profile": {k: db.get_profile().get(k) for k in ("topics", "accuracy")},
    }


def today_picks():
    """今日新卡候选（荐食用）：今天入库的卡，含推荐所需字段。
    排序：命中「关注主题 + 学习画像兴趣主题」的排前面（荐食优先匹配）。"""
    cfg = load_config()
    topics = [t for t in (cfg.get("interested_topics") or []) if t]
    topics += [t for t in (db.get_profile().get("topics") or []) if t]
    cards = db.load_cards(datetime.now().strftime("%Y-%m-%d"))
    out = []
    for c in cards:
        # 影子卡（v2 评审用）可见但不进荐读，避免挤掉用户在学的正片
        if db.is_shadow_card(c):
            continue
        out.append({
            "source_url": c.get("source_url"),
            "title": c.get("title") or "",
            "summary": (c.get("summary") or "")[:80],
            "timeliness": c.get("timeliness"),
            "credibility": c.get("credibility"),
        })
    if topics:
        def rel(p):
            text = ((p.get("title") or "") + (p.get("summary") or "")).lower()
            return -sum(1 for t in topics if t.lower() in text)
        out.sort(key=rel)
    return out[:20]


def think(api_key, report, signals):
    """LLM 决策：读信号产出今日通知 + 自动修复清单。失败返回 None（走规则兜底）。"""
    if not api_key:
        return None
    regenable = [b for b in signals["pending"] if b.get("score") is not None]
    client = OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL, timeout=60)
    try:
        resp = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": AGENT_SYSTEM},
                {"role": "user", "content": AGENT_USER.format(
                    report=json.dumps({
                        "date": report.get("date"),
                        "checked": report.get("checked"),
                        "pass_rate": report.get("pass_rate"),
                        "avg_score": report.get("avg_score"),
                        "badcases": [{"title": b.get("title"), "score": b.get("score"),
                                      "reason": b.get("reason"), "source": b.get("source")}
                                     for b in (report.get("badcases") or [])],
                        "metrics": report.get("metrics") or {},
                        "revision_candidates": db.count_pending_revisions(),
                    }, ensure_ascii=False),
                    signals=json.dumps(signals, ensure_ascii=False, default=str),
                    picks=json.dumps(today_picks(), ensure_ascii=False, default=str),
                    regenable=json.dumps(
                        [{"source_url": b.get("source_url"), "title": b.get("title"),
                          "score": b.get("score"), "reason": b.get("reason")} for b in regenable],
                        ensure_ascii=False, default=str),
                )},
            ],
            temperature=0.4,
            response_format={"type": "json_object"},
        )
        return json.loads(resp.choices[0].message.content)
    except Exception:
        return None


def validate_decision(decision, signals):
    """校验 LLM 输出：regen_candidates 必须在待拍板范围内且 ≤3；notifications 合法且 ≤4；
    recommendations 必须在今日新卡候选里且 ≤3。"""
    if not isinstance(decision, dict):
        return None
    valid_urls = {b.get("source_url") for b in signals["pending"] if b.get("source_url")}
    regen = decision.get("regen_candidates") or []
    if not isinstance(regen, list):
        regen = []
    regen = [u for u in regen if u in valid_urls and db.get_card(u)][:3]  # 过滤已删卡
    notifs = decision.get("notifications") or []
    if not isinstance(notifs, list):
        notifs = []
    cleaned = []
    for n in notifs:
        if not isinstance(n, dict) or not n.get("title") or not n.get("body"):
            continue
        # 白名单只有一个来源（notifications.KEEP），避免两处各写一份迟早漂移
        if n.get("type") not in notifications.KEEP:
            continue
        if n.get("level") not in ("info", "warn", "action"):
            n["level"] = "info"
        cleaned.append({"type": n["type"], "title": str(n["title"])[:20],
                        "body": str(n["body"])[:120], "level": n["level"]})
    # 荐食校验：只能从今日新卡里挑
    pick_urls = {p.get("source_url") for p in today_picks()}
    recs = decision.get("recommendations") or []
    if not isinstance(recs, list):
        recs = []
    cleaned_recs = []
    for r in recs:
        if not isinstance(r, dict) or not r.get("source_url") or not r.get("why"):
            continue
        if r["source_url"] not in pick_urls:
            continue
        cleaned_recs.append({"source_url": r["source_url"],
                             "title": str(r.get("title") or "")[:30],
                             "why": str(r.get("why") or "")[:60]})
    return {"regen_candidates": regen, "notifications": cleaned[:4],
            "recommendations": cleaned_recs[:3],
            "summary": str(decision.get("summary") or "")[:40]}


def fallback(report, signals):
    """规则兜底：LLM 失败时用模板生成通知。

    只生成三类允许的通知里的两类（到期复习 / 待拍板）。
    每日摘要、断签提醒、过时卡、荐读推送都不再产生（范围决策）——
    兜底路径也要守同一条规则，否则模型挂了反而会发更多通知。
    """
    notifs = []
    # 待拍板
    if signals["pending"]:
        names = "、".join((b.get("title") or "")[:10] for b in signals["pending"][:3])
        notifs.append({"type": "badcase_pending",
                       "title": "%d 张卡待你拍板" % len(signals["pending"]),
                       "body": names + "。点「📋 质检」查看并处理。", "level": "action"})
    # 复习拖欠
    rd = signals["review_due"]
    if rd["due"] > 0 and (rd["compliance"] is None or rd["compliance"] < 0.6):
        notifs.append({"type": "review_due",
                       "title": "今天有 %d 张复习到期" % rd["due"],
                       "body": "到期未复习，打开页面即可复习。", "level": "warn"})
    # 荐读兜底：取今日新卡前 3。**不再推送**，只留给 App 内的发现层。
    recs = [{"source_url": p["source_url"], "title": p["title"], "why": "今日新卡，建议优先浏览"}
            for p in today_picks()[:3]]
    return {"regen_candidates": [], "notifications": notifs,
            "recommendations": recs,
            "summary": "已按规则生成今日待办"}


def execute(api_key, decision, signals, backup_dir, dry_run=False):
    """执行决策：写通知 + **提出修订候选**。返回候选记录。

    范围决策：不允许「自愈 Agent」静默修改已发布的卡片。所以这里不再直接
    重生成写回，只提出候选（`revisions.propose`）；采纳与否在审核界面里定。

    残留的 `backup_dir` 参数保留只为兼容调用方——现在不改卡，自然也不需要备份。
    """
    import revisions
    results = []
    # 1. 写通知（去重：同 type 同 title 当天不重复写）
    existing = {n.get("type") + "|" + n.get("title")
                for n in db.list_notifications(limit=100)
                if (n.get("date") or "") == datetime.now().strftime("%Y-%m-%d")}
    for n in decision.get("notifications") or []:
        key = n.get("type") + "|" + n.get("title")
        if key in existing:
            continue
        notifications.emit(n["type"], n["title"], n["body"], level=n.get("level", "info"))
    # 2. 提出修订候选（不改动已发布的卡）
    for src in decision.get("regen_candidates") or []:
        old = db.get_card(src)
        if not old:
            results.append({"source_url": src, "success": False, "reason": "卡不存在"})
            continue
        if dry_run:
            results.append({"source_url": src, "success": True, "reason": "dry-run"})
            continue
        r = revisions.propose(api_key, src, reason="每日巡检判定为坏卡", origin="auto")
        results.append({"source_url": src, "title": old.get("title") or "",
                        "success": "revision_id" in r,
                        "reason": None if "revision_id" in r else r.get("error"),
                        "revision_id": r.get("revision_id"),
                        "studied": r.get("studied"),
                        "changes": r.get("changes") or []})
    # 3. 剩余待拍板（系统按规则生成，避免与自动修复重复/矛盾；修复失败的卡也留给用户）
    #
    # 原来的「自动修复成功」通知已删除：那是"普通成功通知"，属于噪音
    # （范围决策）。修复结果仍在返回的 results 里，需要时看得到。
    done_urls = {r.get("source_url") for r in results if r.get("success")}
    remaining = [b for b in signals["pending"] if b.get("source_url") not in done_urls]
    if remaining:
        names = "、".join((b.get("title") or "")[:10] for b in remaining[:3])
        notifications.pending_review(len(remaining), names)
    # 4. 荐读 Top 3：**不再推送**，存进 user_state 供 App 内的发现层读取。
    #    直接删掉会让"为你挑出值得读的 Top 3"这个能力悄悄消失——数据先留着。
    recs = decision.get("recommendations") or []
    if recs:
        db.set_user_state("daily_picks", json.dumps(recs, ensure_ascii=False))
    # 5. 过时卡 / 薄弱卡：不再单独发通知（属于内容质量，交给质检流程与复习提醒）
    return results


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="只打印不写库/不修复")
    parser.add_argument("--no-regen", action="store_true", help="只发通知不自动修复")
    args = parser.parse_args()

    api_key = load_config().get("deepseek_api_key", "")
    report = latest_report()
    if not report:
        print("暂无质检报告（daily_check 还没跑过），跳过编排。")
        return
    y_report = yesterday_report(report)
    signals = rule_signals(report, y_report)

    if args.dry_run:
        print("== 信号 ==")
        print(json.dumps({k: v for k, v in signals.items() if k != "pending"},
                         ensure_ascii=False, default=str))
        print("== 待拍板 %d ==" % len(signals["pending"]))
        for b in signals["pending"]:
            print(" -", (b.get("title") or "")[:30], "| score:", b.get("score"))
        return

    decision = think(api_key, report, signals)
    if not decision:
        decision = fallback(report, signals)  # LLM 失败兜底
    decision = validate_decision(decision, signals) or fallback(report, signals)
    if args.no_regen:
        decision["regen_candidates"] = []
    try:
        results = execute(api_key, decision, signals, BACKUP_DIR)
    except Exception as exc:  # noqa: BLE001
        # 走到这里说明自动修复也救不回来——这是三类通知里唯一需要人的那一类
        traceback.print_exc()
        notifications.failure("每日质检编排", exc,
                              "自动修复已尝试且失败，需要人工确认卡片库状态")
        raise

    # 学习周报已下线（范围决策：「每日生成报告」类通知取消）。
    # 周报里的有效信息（延迟回忆率、薄弱点、兴趣主题）改在「质检」面板里看。

    print("== 管家决策 ==")
    print("summary:", decision.get("summary"))
    for n in decision.get("notifications") or []:
        print("通知[%s]: %s - %s" % (n.get("level"), n.get("title"), n.get("body")))
    for r in results:
        print("修复:", r.get("success"), "|", (r.get("title") or r.get("source_url") or "")[:30],
              "|", r.get("reason") or "")


if __name__ == "__main__":
    main()
