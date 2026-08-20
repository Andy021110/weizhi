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

import db

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
REPORTS_DIR = os.path.join(BASE_DIR, "quality_reports")
DEEPSEEK_BASE_URL = "https://api.deepseek.com"


def load_config():
    """从 config.json 读取配置，失败返回空 dict（不打日志，cron 静默）。"""
    path = os.path.join(BASE_DIR, "config.json")
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

    quiz_min = 2 if template in ("t1_vocab", "t4_trivia", "t5_skill") else 3
    quiz = card.get("quiz") or []
    if len(quiz) < quiz_min:
        issues.append(("quiz不足", "%d<%d" % (len(quiz), quiz_min)))
    rq = card.get("review_quiz") or []
    if len(rq) < 3:
        issues.append(("复习题不足", "review_quiz %d<3" % len(rq)))
    if template in ("t2_reading", "t3_math", "t5_skill", "t6_code") and not card.get("open_question"):
        issues.append(("缺简答题", "open_question 为空"))
    if template == "t2_reading" and len(card.get("body") or "") < 600:
        issues.append(("精读正文过短", "body %d字<600" % len(card.get("body") or "")))

    if db.find_duplicate(template, card.get("title"), exclude_source_url=card.get("source_url")):
        issues.append(("重复卡", "同模板同标题已存在"))

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
    from prompts import QUALITY_SCORE_SYSTEM, QUALITY_SCORE_USER, TEMPLATE_META
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
    streak = db.get_user_state("streak")
    try:
        streak = int(streak) if streak else 0
    except (TypeError, ValueError):
        streak = 0
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
        "streak": streak,
    }


def pick_candidates(limit=20, check_all=False):
    """候选选择：最新 48h 卡优先；不足 20 张时按 id 游标轮转补足（存 user_state.qc_cursor）。
    周日或 --check-all 时全量检查。"""
    today = datetime.now()
    if check_all or today.weekday() == 6:
        return db.load_cards(None)
    fresh = db.load_cards_since((datetime.now() - timedelta(hours=48)).isoformat())
    if len(fresh) >= limit:
        return fresh[:limit]

    all_cards = sorted(db.load_cards(None), key=lambda c: c.get("id") or 0)
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

    os.makedirs(REPORTS_DIR, exist_ok=True)
    path = os.path.join(REPORTS_DIR, report["date"] + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    cleanup()
    print("已生成质检报告：%s（检查 %d 张，badcase %d 个，通过率 %s）" % (
        path, checked, len(badcases),
        ("%.1f%%" % (report["pass_rate"] * 100)) if report["pass_rate"] is not None else "-"))


if __name__ == "__main__":
    main()
