#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""微知 · 质量巡检 M1（cron 每天 3:30 运行）

流程：
1. rule_check：客观规则零成本检测（think_answer 缺失/过短、quiz 题数、review_quiz<3、
   同模板同标题重复、精读 body<600、字段类型错误、选项/答案越界）
2. ai_score：DeepSeek 4 维度打分（temperature=0.2），单卡失败跳过不计入 checked
3. usage_metrics：体验日报（日学习量/复习完成率/复习正确率/掌握率/连击/到期遵守度）
4. pick_candidates：最新 48h 卡 + 按 id 游标轮转补足 20 张；周日全量
5. 报告落盘 quality_reports/YYYY-MM-DD.json，只留最近 30 天

用法：
    python daily_check.py            # 正常巡检（周日自动全量）
    python daily_check.py --limit 1  # 只查 1 张（测试）
    python daily_check.py --dry-run  # 只打印不落盘
"""
import argparse
import json
import os
import sys
from datetime import datetime, timedelta

from openai import OpenAI

import traceback

from weizhi.core import db
from weizhi.core import notifications
from weizhi.core import paths


REPORTS_DIR = os.path.join(paths.data_dir(), "quality_reports")
DEEPSEEK_BASE_URL = "https://api.deepseek.com"


def load_config():
    """从 config.json 读取配置，失败返回空 dict（不打日志，cron 静默）。"""
    path = os.path.join(paths.data_dir(), "config.json")
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _card_text(card):
    """把卡片压缩成供 AI 打分的纯文本。"""
    parts = []
    parts.append("标题：" + (card.get("title") or ""))
    if card.get("summary"):
        parts.append("摘要：" + str(card["summary"]))
    if card.get("body"):
        parts.append("正文：" + str(card["body"])[:800])
    for key in ("intuition", "hook", "why", "pseudocode"):
        if card.get(key):
            parts.append(str(card[key])[:400])
    if card.get("core_points"):
        parts.append("核心观点：" + "；".join(str(x) for x in card["core_points"]))
    if card.get("fun_facts"):
        parts.append("有趣事实：" + "；".join(str(x) for x in card["fun_facts"]))
    if card.get("steps"):
        parts.append("步骤：" + "；".join(
            (s.get("step") + "：" + str(s.get("detail", ""))[:60]) if isinstance(s, dict) else str(s)
            for s in card["steps"]))
    if card.get("think_question"):
        parts.append("思考题：" + str(card["think_question"]))
    if card.get("quiz"):
        parts.append("选择题数：" + str(len(card["quiz"])))
    if card.get("review_quiz"):
        parts.append("复习题数：" + str(len(card["review_quiz"])))
    return "\n".join(parts)[:2500]


def rule_check(card):
    """客观规则检测，返回 [(问题标签, 详情)]，空列表 = 通过。"""
    issues = []
    template = card.get("template") or (card.get("_meta") or {}).get("template") or "t2_reading"
    ta = (card.get("think_answer") or "").strip()
    if not card.get("think_question"):
        issues.append(("缺思考题", "think_question 为空"))
    if not ta:
        issues.append(("缺思考题答案", "think_answer 为空"))
    elif len(ta) < 150:
        issues.append(("回答过短", "think_answer %d字<150" % len(ta)))

    # 范围决策：固定题量（随堂三道 + 复习三道）已废除，题量由学习目标决定。
    # 唯一硬要求是「学完当场要能测」——至少一道即时题；24 小时/7 天层可有可无。
    # 注意不要退回旧写法：那是把刚废掉的规则又固化回来。
    quiz = card.get("quiz") or []
    rq = card.get("review_quiz") or []
    if not quiz:
        issues.append(("缺即时题", "学完当场要能测，quiz 不能为空"))
    if not (quiz or rq):
        issues.append(("无题目", "这张卡没有任何可验证的题目"))
    if template in ("t2_reading", "t3_math", "t5_skill", "t6_code") and not card.get("open_question"):
        issues.append(("缺简答题", "open_question 为空"))
    # 范围决策的软范围是「简单概念 600–900 / 技术机制 900–1500」，
    # 所以 600 是软范围的下限而不是一条独立硬规则；数学与代码类正文可以更短，
    # 因此这里只对 t2_reading 生效。
    if template == "t2_reading" and len(card.get("body") or "") < 600:
        issues.append(("精读正文过短", "body %d字<600（软范围下限）" % len(card.get("body") or "")))

    if db.find_duplicate(template, card.get("title"), exclude_source_url=card.get("source_url")):
        issues.append(("重复卡", "同模板同标题已存在"))

    # 时效性（四档，兼容旧 trending）：快变/时点类必须有发布时间（有真实来源时）；除稳定类外必须标权威度
    t = card.get("timeliness")
    if t == "trending":
        t = "evolving"
    if t in ("fast", "event"):
        has_real_src = bool(card.get("source_url") and str(card.get("source_url")).startswith("http"))
        if has_real_src and not card.get("published"):
            issues.append(("缺发布时间", "快变/时点内容未标注发布时间"))
    if t in ("evolving", "fast", "event") and not card.get("credibility"):
        issues.append(("缺权威度", "时效内容未标注来源权威度"))

    for f in ("quiz", "review_quiz"):
        v = card.get(f)
        if v is not None and not isinstance(v, list):
            issues.append(("字段类型错误", "%s 不是数组" % f))
    if card.get("open_question") is not None and not isinstance(card.get("open_question"), dict):
        issues.append(("字段类型错误", "open_question 不是对象"))

    for i, q in enumerate(quiz or []):
        if not isinstance(q, dict):
            issues.append(("quiz结构错误", "quiz[%d] 不是对象" % i))
            break
        opts = q.get("options")
        if not isinstance(opts, list) or len(opts) < 2:
            issues.append(("选项不足", "quiz[%d] options<2" % i))
            break
        ans = q.get("answer")
        if isinstance(ans, int) and not (0 <= ans < len(opts)):
            issues.append(("答案越界", "quiz[%d] answer=%s 超出范围" % (i, ans)))
            break
    return issues


def ai_score(api_key, card):
    """DeepSeek 4 维度打分。成功返回 dict，失败返回 None（单卡容错）。"""
    if not api_key:
        return None
    from weizhi.core.prompts import QUALITY_SCORE_SYSTEM, QUALITY_SCORE_USER, TEMPLATE_META
    template = card.get("template") or (card.get("_meta") or {}).get("template") or "t2_reading"
    label = TEMPLATE_META.get(template, {}).get("label", "精读")
    client = OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL, timeout=60)
    try:
        resp = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": QUALITY_SCORE_SYSTEM},
                {"role": "user", "content": QUALITY_SCORE_USER.format(
                    template_label=label,
                    title=card.get("title") or "",
                    content=_card_text(card),
                )},
            ],
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content)
        data["score"] = float(data.get("score") or 0)
        data["verdict"] = (data.get("verdict") or ("badcase" if data["score"] < 3 else "pass"))
        return data
    except Exception:
        return None


def usage_metrics():
    """体验日报（口径见方案：progress 当日条数含散卡+任务卡；复习完成率=当日复习/当日到期）。"""
    today = datetime.now().strftime("%Y-%m-%d")
    yesterday = (datetime.now() - timedelta(days=1)).strftime("%Y-%m-%d")
    rate, mastered, total = db.mastery_rate()
    comp, comp_done, comp_due = db.compliance_on_date(today)
    acc, acc_total = db.review_accuracy_on_date(today)
    return {
        "date": today,
        "study_today": db.study_on_date(today),
        "study_yesterday": db.study_on_date(yesterday),
        "due_today": db.count_due_on_date(today),
        "reviewed_today": db.reviewed_on_date(today),
        "compliance_today": comp,
        "compliance_done": comp_done,
        "compliance_due": comp_due,
        "accuracy_today": acc,
        "accuracy_total": acc_total,
        "mastery_rate": rate,
        "mastered": mastered,
        "reviewing_total": total,
        # 累计学习天数取代连续打卡（范围决策）。
        # 连续天数会给用户施压、断一天就归零，衡量不出学进去多少；
        # 累计天数不施压。它**不是**质量指标——质量看延迟回忆率与目标完成度。
        "study_days": db.study_days(),
        "usage_today": db.events_on_date(today),  # M2：思考题/简答/日历/搜索使用率
    }


def pick_candidates(limit=20, check_all=False):
    """候选选择：最新 48h 卡优先；不足 20 张时按 id 游标轮转补足（存 user_state.qc_cursor）。
    周日或 --check-all 时全量检查。"""
    today = datetime.now()
    # 影子卡不参与质检与自动修复：评审阶段要看的就是 v2 的原始产出，
    # 让 v1 的修复流水线去改它，等于把要评估的东西先改了一遍。
    def _keep(cards):
        return [c for c in cards if not db.is_shadow_card(c)]

    if check_all or today.weekday() == 6:
        return _keep(db.load_cards(None))
    fresh = _keep(db.load_cards_since((datetime.now() - timedelta(hours=48)).isoformat()))
    if len(fresh) >= limit:
        return fresh[:limit]

    all_cards = sorted(_keep(db.load_cards(None)), key=lambda c: c.get("id") or 0)
    if not all_cards:
        return fresh
    try:
        cursor = int(db.get_user_state("qc_cursor") or 0)
    except (TypeError, ValueError):
        cursor = 0
    window = [c for c in all_cards if (c.get("id") or 0) > cursor]
    if not window:
        window = all_cards  # 绕回
    seen = {c.get("id") for c in fresh}
    picked = list(fresh)
    for c in window:
        if len(picked) >= limit:
            break
        if c.get("id") not in seen:
            picked.append(c)
    new_cursor = max((c.get("id") or 0) for c in picked)
    if new_cursor:
        db.set_user_state("qc_cursor", str(new_cursor))
    return picked


def cleanup(days=30):
    """只保留最近 30 天报告。"""
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    if not os.path.isdir(REPORTS_DIR):
        return
    for f in os.listdir(REPORTS_DIR):
        if f.endswith(".json") and f[:-5] < cutoff:
            try:
                os.remove(os.path.join(REPORTS_DIR, f))
            except OSError:
                pass


def _backup_card(card, backup_dir):
    """自动重生成前把旧卡完整备份（含完成记录）。返回相对路径 backups/{日期}/{md5}.json。"""
    import hashlib
    src = card.get("source_url") or ""
    h = hashlib.md5(src.encode("utf-8")).hexdigest()
    day = card.get("date") or datetime.now().strftime("%Y-%m-%d")
    d = os.path.join(backup_dir, day)
    os.makedirs(d, exist_ok=True)
    data = {
        "source_url": src,
        "card": card,
        "card_date": day,
        "progress_dates": db.progress_dates_of(src),
    }
    path = os.path.join(d, h + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    return os.path.join(day, h + ".json")


def _backup_count(source_url, backup_dir):
    """该卡已有备份数（用于限制同一卡最多自动修复 2 次，防反复烧钱）。"""
    import hashlib
    if not source_url or not os.path.isdir(backup_dir):
        return 0
    h = hashlib.md5(source_url.encode("utf-8")).hexdigest()
    n = 0
    for root, _dirs, files in os.walk(backup_dir):
        if h + ".json" in files:
            n += 1
    return n


def _update_backup_new(backup_dir, rel, new_src):
    """regen 成功后把新卡 source_url 补进备份文件（回滚时删除新卡用）。"""
    if not rel or not new_src:
        return
    try:
        p = os.path.join(backup_dir, rel)
        with open(p, "r", encoding="utf-8") as f:
            data = json.load(f)
        data["new_source_url"] = new_src
        with open(p, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except (OSError, json.JSONDecodeError):
        pass


def propose_revisions(api_key, badcases, max_actions=3):
    """对判定的坏卡**提出修订候选**，不直接改卡。

    范围决策：不允许「自愈 Agent」静默修改已发布的卡片，重新发布前要过人工。
    原来的实现是备份 + 重生成写回，而写回会「存新卡 + 删旧卡」，
    连带删掉用户的完成记录——已学过的卡尤其不能这样变。

    返回候选记录，写入报告的 revisions 字段。
    """
    from weizhi.ops import revisions
    candidates = []
    for b in badcases:
        if b["source"] == "ai" and (b.get("score") is not None) and b["score"] <= 2.0:
            candidates.append(b)
        elif b["source"] == "rule" and "重复卡" not in (b.get("issues") or []):
            candidates.append(b)
    out = []
    for b in candidates[:max_actions]:
        src = b.get("source_url")
        if not src:
            continue
        r = revisions.propose(api_key, src, reason=b.get("reason") or "巡检判定为坏卡",
                              origin="auto")
        out.append({
            "source_url": src,
            "title": b.get("title") or "",
            "score": b.get("score"),
            "success": "revision_id" in r,
            "revision_id": r.get("revision_id"),
            "studied": r.get("studied"),
            "changes": r.get("changes") or [],
            "error": r.get("error"),
        })
    return out


def build_summary(report):
    """badcase 收集总结：问题类型分布 + 模板分布 + 修复统计 + 近 7 天趋势。

    前端质检弹窗的「汇总」区块数据源；同时沉淀为 skill 可复用的质检复盘数据。
    """
    badcases = report.get("badcases") or []
    issue_counter = {}
    for b in badcases:
        issues = b.get("issues") or []
        key = issues[0] if issues else ((b.get("reason") or "其他")[:10])
        issue_counter[key] = issue_counter.get(key, 0) + 1
    tpl_counter = {}
    for b in badcases:
        c = db.get_card(b.get("source_url") or "")
        tpl = (c or {}).get("template") or "未知"
        tpl_counter[tpl] = tpl_counter.get(tpl, 0) + 1
    acts = report.get("revisions") or []
    ok = [a for a in acts if a.get("success")]
    trend = []
    for i in range(6, -1, -1):
        d = (datetime.now() - timedelta(days=i)).strftime("%Y-%m-%d")
        p = os.path.join(REPORTS_DIR, d + ".json")
        if not os.path.exists(p):
            continue
        try:
            with open(p, encoding="utf-8") as f:
                r = json.load(f)
            trend.append({
                "date": d,
                "checked": r.get("checked"),
                "pass_rate": r.get("pass_rate"),
                "badcases": len(r.get("badcases") or []),
            })
        except (json.JSONDecodeError, OSError):
            pass
    return {
        "issues": sorted(issue_counter.items(), key=lambda x: -x[1]),
        "templates": sorted(tpl_counter.items(), key=lambda x: -x[1]),
        "repair": {
            "ok": len(ok),
            "total": len(acts),
            "rate": round(len(ok) / len(acts), 3) if acts else None,
        },
        "trend": trend,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=20, help="检查张数上限")
    parser.add_argument("--dry-run", action="store_true", help="只打印不落盘")
    parser.add_argument("--check-all", action="store_true", help="全量检查（忽略轮转）")
    args = parser.parse_args()

    api_key = load_config().get("deepseek_api_key", "")
    cards = pick_candidates(limit=args.limit, check_all=args.check_all)
    if not cards:
        print("暂无卡片，跳过巡检。")
        return

    badcases = []
    scores = []
    checked = 0
    for card in cards:
        src = card.get("source_url") or ""
        title = card.get("title") or "(无标题)"
        issues = rule_check(card)
        if issues:
            checked += 1
            badcases.append({
                "source_url": src, "title": title,
                "reason": "客观规则：" + "；".join(d for _, d in issues),
                "issues": [t for t, _ in issues],
                "score": None, "verdict": "badcase", "source": "rule",
            })
            continue
        score = ai_score(api_key, card)
        if not score:
            continue  # AI 打分失败：跳过不计入 checked
        checked += 1
        scores.append(score["score"])
        if score.get("verdict") == "badcase":
            badcases.append({
                "source_url": src, "title": title,
                "reason": "AI 判定：" + (score.get("suggestion") or "质量不达标"),
                "issues": score.get("issues") or [],
                "score": score["score"], "verdict": "badcase", "source": "ai",
            })

    report = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "checked": checked,
        "badcases": badcases,
        "metrics": usage_metrics(),
        "avg_score": round(sum(scores) / len(scores), 2) if scores else None,
        "pass_rate": round((checked - len(badcases)) / checked, 3) if checked else None,
    }

    if args.dry_run:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    # M3 安全子集（CP24 起改语义）：**只提出修订候选，不改卡**。
    # 原来这里会直接重生成写回并「存新卡 + 删旧卡」，连带删掉用户的完成记录；
    # 现在候选挂在审核界面，采纳与否由人决定。
    report["revisions"] = propose_revisions(api_key, badcases)

    # 收集总结：问题类型分布 + 模板分布 + 修复统计 + 近 7 天趋势
    report["summary"] = build_summary(report)

    os.makedirs(REPORTS_DIR, exist_ok=True)
    path = os.path.join(REPORTS_DIR, report["date"] + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    cleanup()
    revs = report.get("revisions") or []
    ok_revs = [a for a in revs if a.get("success")]
    print("已生成质检报告：%s（检查 %d 张，badcase %d 个，通过率 %s，提出修订候选 %d/%d）" % (
        path, checked, len(badcases),
        ("%.1f%%" % (report["pass_rate"] * 100)) if report["pass_rate"] is not None else "-",
        len(ok_revs), len(revs)))


if __name__ == "__main__":
    try:
        main()
    except Exception as exc:  # noqa: BLE001
        # 未捕获异常 = 这一轮巡检彻底没跑成，属于「系统任务失败且无法自动恢复」。
        # 只在真失败时报：能重试、能降级、能自动修复的都不发，否则这个类型
        # 会变成另一种噪音，而它存在的唯一理由就是稀有。
        traceback.print_exc()
        if "--dry-run" not in sys.argv:
            notifications.failure("每日质检巡检", exc,
                                  "本轮没有产出质检报告，需要人工看一眼")
        raise
