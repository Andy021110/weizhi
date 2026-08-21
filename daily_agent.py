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
from datetime import datetime, timedelta

from openai import OpenAI

import db

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
2. regen_candidates 只能从输入给出的可自动修复清单里选（source_url 必须原样匹配）；选中的会被系统自动修复，不要再在通知里提到它们。
3. recommendations 只能从【今日新卡候选】里挑（source_url 必须原样匹配），最多 3 条；候选不足 3 条就少给，不要硬凑。
4. notifications 的 type 只能是 daily_summary / daily_picks / review_due / streak_warn / stale_warn 之一（badcase_pending 由系统自动生成，你不用管）。
5. 没必要的通知不要发（例如今天没有拖欠复习，就不发 review_due；过时卡 <3 张就不发 stale_warn）。
"""

AGENT_USER = """【今日质检报告】
{report}

【规则信号】
{signals}

【今日新卡候选（从这里挑 recommendations 的 Top 3）】
{picks}

【可自动修复清单（评分 2-3 分，可从这里挑 regen_candidates）】
{regenable}

输出 JSON（字段名必须一致）：
{{
  "summary": "今日状态一句话（30字内）",
  "notifications": [
    {{
      "type": "daily_summary",
      "title": "通知标题，15字内",
      "body": "通知正文，60字内",
      "level": "info|warn|action"
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
1. daily_summary 必发一条。
2. 有值得读的新卡 → recommendations 给 Top 3（附 why）；没有好卡就不给（不要硬凑）。
3. 复习拖欠（due>0 且完成率<60%）→ 发 review_due，level=warn。
4. 断签或连续下滑 → 发 streak_warn，level=warn。
5. 库内过时卡 ≥3 张 → 发 stale_warn，level=warn（正文给数量和最老的一张标题）。
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
    for a in report.get("auto_actions") or []:
        if not a.get("success") and a.get("source_url"):
            pending.append({
                "source_url": a["source_url"], "title": a.get("title") or "",
                "score": a.get("score"), "reason": "自动修复失败：" + (a.get("error") or ""),
            })
    # 复习拖欠
    due = metrics.get("due_today") or 0
    compliance = metrics.get("compliance_today")
    review_due = bool(due > 0 and (compliance is None or compliance < 0.6))
    # 断签
    last_active = db.get_user_state("last_active_date")
    streak = db.get_user_state("streak")
    try:
        streak = int(streak) if streak else 0
    except (TypeError, ValueError):
        streak = 0
    gap_days = None
    if last_active:
        try:
            gap_days = (datetime.now() - datetime.strptime(last_active, "%Y-%m-%d")).days
        except ValueError:
            gap_days = None
    streak_broken = bool(gap_days is not None and gap_days >= 2)
    # 学习下滑（对比昨日）
    study_today = metrics.get("study_today") or 0
    study_y = (y_report.get("metrics") or {}).get("study_today") or 0 if y_report else None
    decline = bool(study_y is not None and study_y > 0 and study_today < study_y * 0.5)
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
        "streak": {"days": streak, "last_active": last_active, "broken": streak_broken},
        "study": {"today": study_today, "yesterday": study_y, "decline": decline},
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
                        "auto_actions": [{"title": a.get("title"), "success": a.get("success")}
                                         for a in (report.get("auto_actions") or [])],
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
        if n.get("type") not in ("daily_summary", "daily_picks", "review_due", "streak_warn", "stale_warn"):
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
    """规则兜底：LLM 失败时用模板生成通知。"""
    metrics = report.get("metrics") or {}
    notifs = []
    # 每日摘要（必发）
    n_auto = len([a for a in (report.get("auto_actions") or []) if a.get("success")])
    body = "检查 %d 张，通过率 %s，badcase %d 个" % (
        report.get("checked") or 0,
        ("%.0f%%" % ((report.get("pass_rate") or 0) * 100)) if report.get("pass_rate") is not None else "-",
        len(report.get("badcases") or []),
    )
    if n_auto:
        body += "，已自动修复 %d 张" % n_auto
    notifs.append({"type": "daily_summary", "title": "今日巡检摘要",
                   "body": body, "level": "info"})
    # 待拍板
    if signals["pending"]:
        names = "、".join((b.get("title") or "")[:10] for b in signals["pending"][:3])
        notifs.append({"type": "badcase_pending",
                       "title": "%d 张卡待你拍板" % len(signals["pending"]),
                       "body": names + "。点「📋 质检」查看并处理。" , "level": "action"})
    # 复习拖欠
    rd = signals["review_due"]
    if rd["due"] > 0 and (rd["compliance"] is None or rd["compliance"] < 0.6):
        notifs.append({"type": "review_due",
                       "title": "今天有 %d 张复习到期" % rd["due"],
                       "body": "到期未复习，打开页面即可复习。" , "level": "warn"})
    # 断签 / 下滑
    if signals["streak"]["broken"]:
        notifs.append({"type": "streak_warn", "title": "连击已中断",
                       "body": "上次学习是 %s，回来续上吧。" % (signals["streak"]["last_active"] or "前几天"),
                       "level": "warn"})
    elif signals["study"]["decline"]:
        notifs.append({"type": "streak_warn", "title": "学习量下滑",
                       "body": "今日学习 %d 张 < 昨日 %d 张。" % (
                           signals["study"]["today"], signals["study"]["yesterday"] or 0),
                       "level": "warn"})
    # 荐食兜底：取今日新卡前 3（LLM 挂了也要有东西可看）
    recs = [{"source_url": p["source_url"], "title": p["title"], "why": "今日新卡，建议优先浏览"}
            for p in today_picks()[:3]]
    return {"regen_candidates": [], "notifications": notifs[:4],
            "recommendations": recs,
            "summary": "已按规则生成今日通知"}


def execute(api_key, decision, signals, backup_dir, dry_run=False):
    """执行决策：写通知 + 自动修复（带备份）。返回动作记录。"""
    from daily_check import _backup_card, _backup_count, _update_backup_new
    from reader import regen_card
    results = []
    # 1. 写通知（去重：同 type 同 title 当天不重复写）
    existing = {n.get("type") + "|" + n.get("title")
                for n in db.list_notifications(limit=100)
                if (n.get("date") or "") == datetime.now().strftime("%Y-%m-%d")}
    for n in decision.get("notifications") or []:
        key = n.get("type") + "|" + n.get("title")
        if key in existing:
            continue
        db.add_notification(n["type"], n["title"], n["body"], level=n.get("level", "info"))
    # 2. 自动修复
    for src in decision.get("regen_candidates") or []:
        old = db.get_card(src)
        if not old:
            results.append({"source_url": src, "success": False, "reason": "卡不存在"})
            continue
        if _backup_count(src, backup_dir) >= 2:
            results.append({"source_url": src, "success": False, "reason": "已自动修复2次，转人工"})
            continue
        if dry_run:
            results.append({"source_url": src, "success": True, "reason": "dry-run"})
            continue
        backup = _backup_card(old, backup_dir)
        r = regen_card(api_key, src)
        ok = bool(r.get("success"))
        if ok:
            _update_backup_new(backup_dir, backup, (r.get("card") or {}).get("source_url"))
        results.append({"source_url": src, "title": old.get("title") or "",
                        "success": ok, "reason": None if ok else r.get("error"),
                        "backup": backup if ok else None,
                        "new_source_url": (r.get("card") or {}).get("source_url") if ok else None})
    # 3. 修复结果补一条通知（有修复时）
    ok_n = [r for r in results if r.get("success")]
    if ok_n:
        db.add_notification(
            "action_log", "自动修复 %d 张" % len(ok_n),
            "已自动重生成：%s。不满意可到「📋 质检」回滚。" % "、".join(
                (r.get("title") or "")[:10] for r in ok_n[:3]),
            level="info")
    # 4. 剩余待拍板（系统按规则生成，避免与自动修复重复/矛盾；修复失败的卡也留给用户）
    done_urls = {r.get("source_url") for r in results if r.get("success")}
    remaining = [b for b in signals["pending"] if b.get("source_url") not in done_urls]
    if remaining:
        names = "、".join((b.get("title") or "")[:10] for b in remaining[:3])
        db.add_notification(
            "badcase_pending", "%d 张卡待你拍板" % len(remaining),
            names + "。点「📋 质检」查看评分与理由，决定重生成或保留。", level="action")
    # 5. 荐食（A1 daily_picks）：LLM 挑的 Top 3 + 为什么值得读
    recs = decision.get("recommendations") or []
    if recs:
        body = "；".join("%s（%s）" % (r.get("title", "")[:18], r.get("why", "")) for r in recs[:3])
        db.add_notification("daily_picks", "今日荐读 Top %d" % len(recs), body, level="info")
    # 6. 过时卡提醒（C2）：系统规则兜底，≥3 张才发（LLM 没发就补）
    stale = signals.get("stale") or []
    has_stale = any(n.get("type") == "stale_warn" for n in (decision.get("notifications") or []))
    if len(stale) >= 3 and not has_stale:
        oldest = sorted(stale, key=lambda s: s.get("days", 0), reverse=True)[0]
        db.add_notification(
            "stale_warn", "%d 张卡可能过时" % len(stale),
            "最早：「%s」（发布于 %s），建议重看或清理。" % ((oldest.get("title") or "")[:18], oldest.get("published")),
            level="warn")
    # 7. 薄弱卡提醒（画像）：复习记错 ≥2 次的未掌握卡，≥2 张时提醒优先复习
    weak = signals.get("weak") or []
    if len(weak) >= 2:
        names = "、".join((w.get("title") or "")[:12] for w in weak[:3])
        db.add_notification(
            "weak_review", "%d 张薄弱卡待巩固" % len(weak),
            names + "（记错 ≥2 次）。建议优先复习，必要时降低难度重看。", level="warn")
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
    results = execute(api_key, decision, signals, BACKUP_DIR)

    # 学习周报（周日）：画像 + 本周学习复盘
    if datetime.now().weekday() == 6 and not args.dry_run and not args.no_regen:
        weekly_report()

    print("== 管家决策 ==")
    print("summary:", decision.get("summary"))
    for n in decision.get("notifications") or []:
        print("通知[%s]: %s - %s" % (n.get("level"), n.get("title"), n.get("body")))
    for r in results:
        print("修复:", r.get("success"), "|", (r.get("title") or r.get("source_url") or "")[:30],
              "|", r.get("reason") or "")


def weekly_report():
    """周日学习周报（画像驱动）：本周学习量、复习准确率、薄弱点、兴趣主题。"""
    import sqlite3 as _sq
    today = datetime.now()
    week_ago = (today - timedelta(days=7)).strftime("%Y-%m-%d")
    conn2 = _sq.connect(os.path.join(BASE_DIR, "weizhi.db"), timeout=30)
    try:
        study_n = conn2.execute(
            "SELECT COUNT(*) FROM progress WHERE date >= ?", (week_ago,)
        ).fetchone()[0]
    finally:
        conn2.close()
    prof = db.get_profile()
    accuracy = prof.get("accuracy")
    weak = prof.get("weak") or []
    topics = prof.get("topics") or []
    body = "本周学习 %d 张；复习准确率 %s；%s。%s" % (
        study_n,
        ("%.0f%%" % (accuracy * 100)) if accuracy is not None else "-",
        ("兴趣主题：" + "、".join(topics[:4])) if topics else "兴趣主题待积累",
        ("薄弱卡 %d 张，建议优先巩固" % len(weak)) if len(weak) else "无薄弱卡，状态良好",
    )
    db.add_notification("weekly_report", "本周学习周报", body, level="info")


if __name__ == "__main__":
    main()
