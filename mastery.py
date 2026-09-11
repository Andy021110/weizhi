# -*- coding: utf-8 -*-
"""微知 v2 · 掌握度与遗忘风险（CP10，方案 M2/M4）。

方案的两条硬要求决定了这里只能写确定性逻辑，不能交给模型：

1. M2：「根据复习结果更新 concept mastery，**不直接修改历史卡片**」——
   掌握度是独立一层状态，卡片一旦生成就是历史，改掌握度不动卡片。
2. M4：「使用 mastery 和遗忘风险调整今日安排」——它会直接影响用户每天看到什么，
   必须是可复算、可解释、可回归测试的，不能让模型每天给出不一样的判断。

掌握度不是「答对率」。一道题答对只说明这一次能想起来；要判断是否真的掌握，
看的是**在不同时间点重复回忆的表现**。所以这里有三个量：

- `score`：掌握度 0-1。答对时向上逼近 1，答错时向下打折，
  且**答错扣得比答对加得狠**（错一次说明之前的印象是虚的）。
- `interval_days`：下次复习间隔。掌握度越高间隔越长。
- `risk`：遗忘风险 0-1，由「距上次复习多久」除以「当前记忆稳定期」得到。

用法::

    import mastery
    st = mastery.record("g-1", "m2", correct=True, confidence=0.8)
    mastery.due("g-1")          # 今天该复习的
    mastery.weakest("g-1")      # 最薄弱的里程碑
"""
import math
from datetime import datetime, timedelta

import db

# 初始掌握度。给 0.3 而不是 0：完全没见过和学过但没验过是两回事，
# 用 0 会让「没学过」和「学得很差」混在一起，规划器就分不出该先补哪个。
DEFAULT_SCORE = 0.3

# 达标线。方案 M2 要求「每张卡能映射到一个能力缺口」——低于这条线就还是缺口。
MASTERED = 0.7

GAIN = 0.35      # 答对时向 1 逼近的比例
LOSS = 0.50      # 答错时向下打折的比例（比 GAIN 大，因为错一次推翻的是既有印象）
MAX_INTERVAL = 60.0
MIN_STABILITY_DAYS = 1.0
MAX_STABILITY_DAYS = 14.0


def _now(now=None):
    return now or datetime.now()


def _parse(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(ts)
    except ValueError:
        return None


def update_score(score, correct, confidence=None):
    """掌握度增量更新。

    `confidence` 是用户作答时的自评置信度。**高置信但答错**是最危险的信号——
    说明他建立了错误的信心，比「不确定所以答错」更需要回头重讲。
    所以这种情形额外多扣一点。
    """
    score = DEFAULT_SCORE if score is None else float(score)
    if correct:
        score = score + (1.0 - score) * GAIN
    else:
        score = score - score * LOSS
        if confidence is not None and confidence >= 0.7:
            score -= 0.05   # 自信地答错，多扣
    return round(max(0.0, min(1.0, score)), 4)


def stability_days(score):
    """记忆稳定期（天）：掌握得越牢，同样的间隔下滑得越慢。"""
    return MIN_STABILITY_DAYS + (MAX_STABILITY_DAYS - MIN_STABILITY_DAYS) * float(score or 0)


def risk(state, now=None):
    """遗忘风险 0-1。没复习过视为最高风险。"""
    if not state or not state.get("last_review_at"):
        return 1.0
    last = _parse(state["last_review_at"])
    if not last:
        return 1.0
    days = max(0.0, (_now(now) - last).total_seconds() / 86400.0)
    stab = stability_days(state.get("score"))
    # 指数衰减：刚复习完风险接近 0，过了约一个稳定期升到 ~0.63，两个稳定期接近 0.86
    return round(1.0 - math.exp(-days / stab), 4)


def next_interval(score, ease=None):
    """下次复习间隔（天）。答对多、掌握高就拉长，但不超上限。"""
    base = stability_days(score)
    scaled = base * (1.0 + float(score or 0) * 2.0) * ((ease or 2.5) / 2.5)
    return round(max(1.0, min(MAX_INTERVAL, scaled)), 2)


def record(goal_key, concept, correct, confidence=None, elapsed_ms=None,
           hint_used=False, error_type=None, detail=None, draft_id=None,
           question_idx=None, now=None):
    """记一次作答并更新掌握度，返回更新后的状态。

    先写原始记录再更新汇总——万一更新逻辑出错，原始记录还在，可以重算。
    """
    stamp = _now(now)
    state = db.get_v2_mastery(goal_key, concept) or {}
    score = update_score(state.get("score"), correct, confidence)

    # 用了提示的答对不算真会：掌握度按答对算，但间隔不放大
    interval = next_interval(score, state.get("ease"))
    if hint_used:
        interval = max(1.0, interval * 0.5)

    db.log_v2_review(
        goal_key, concept, correct, draft_id=draft_id, question_idx=question_idx,
        confidence=confidence, elapsed_ms=elapsed_ms, hint_used=hint_used,
        error_type=error_type, detail=detail,
    )
    fields = {
        "score": score,
        "attempts": (state.get("attempts") or 0) + 1,
        "correct": (state.get("correct") or 0) + (1 if correct else 0),
        "interval_days": interval,
        "last_review_at": stamp.isoformat(timespec="seconds"),
        "next_review_at": (stamp + timedelta(days=interval)).isoformat(timespec="seconds"),
    }
    db.upsert_v2_mastery(goal_key, concept, **fields)
    return dict(fields, goal_key=goal_key, concept=concept)


def due(goal_key, now=None, limit=None):
    """到期该复习的掌握度条目，遗忘风险高的排前面。

    排序按风险降序而不是按到期时间升序：同一天到期时，
    快要忘光的那个先练，收益最大。
    """
    stamp = _now(now)
    items = []
    for concept, state in (db.get_v2_mastery(goal_key) or {}).items():
        nxt = _parse(state.get("next_review_at"))
        if nxt is None or nxt <= stamp:
            items.append(dict(state, risk=risk(state, now)))
    items.sort(key=lambda s: -s["risk"])
    return items[:limit] if limit else items


def weakest(goal_key, concepts=None, now=None, limit=None):
    """最薄弱的里程碑：未达标里掌握度最低的。达标的不再占用学习预算。"""
    states = db.get_v2_mastery(goal_key) or {}
    out = []
    for concept in (concepts if concepts is not None else states.keys()):
        state = states.get(concept) or {}
        score = state.get("score")
        if score is not None and score >= MASTERED:
            continue
        out.append(dict(state or {"concept": concept},
                        concept=concept,
                        score=DEFAULT_SCORE if score is None else score,
                        risk=risk(state, now)))
    out.sort(key=lambda s: (s["score"], -s["risk"]))
    return out[:limit] if limit else out


def explain(state, now=None):
    """把一条掌握度翻译成人话，用于「为什么推荐这张卡」。"""
    if not state:
        return "还没有复习记录，属于能力缺口"
    score = float(state.get("score") or 0)
    attempts = state.get("attempts") or 0
    r = risk(state, now)
    if attempts == 0:
        return "还没测过，无法判断"
    return "掌握度 %.2f（%d 次作答，对 %d 次），遗忘风险 %.2f" % (
        score, attempts, state.get("correct") or 0, r)
