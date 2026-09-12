# -*- coding: utf-8 -*-
"""M4 复习流程：到期 → 逐题作答 → 服务端判卷 → 反馈 → 掌握度更新。

**为什么判卷必须搬到服务端**

v1 原先把每道题的 `answer` 随卡片一起发给浏览器，前端自己比对（三处都是：
复习、卡片详情、计划任务）。任何人打开开发者工具就能看到答案——「复习」这件事
就不成立了。更关键的是**掌握度**：它决定用户接下来看到什么内容，
这个判断不能建立在客户端报上来的结果上。

**为什么题库是「合并 + 分层」的**

范围决策 D2 明确：随堂题与复习题合并为「目标验证题库」，按时间分
（`immediate` / `day1` / `day7`），不再维护两套。v2 桥接过来的题自带 `layer`；
v1 老卡没有，按它原来所在的字段推断（随堂→immediate，复习→day1）。

**为什么一张卡答完才推进 SM-2**

SM-2（`db.schedule_review`）量的是「这张卡还记不记得」，不是「这一道题对不对」。
逐题推进会让一次复习被记成多次间隔跳跃。所以逐题只记明细，
卡的调度在整卡答完后更新一次。
"""
import db
import mastery

# 题目按时间层的展示顺序。学完当场 → 24 小时 → 7 天
LAYER_ORDER = ("immediate", "day1", "day7")

# v1 老卡没有 layer，按题目原本所在的字段推断
_FIELD_LAYER = (("quiz", "immediate"), ("review_quiz", "day1"))

# 下发前必须剥掉的字段：这些是答案，不是题面
SEALED_DROP = ("answer", "explanation", "error_reason")

# 判定「这张卡算过了」的正确率门槛
PASS_RATIO = 0.6


def merge_questions(card):
    """把随堂题与复习题合并成一个按时间分层的题库。

    **顺序必须是确定性的**：服务端判卷靠下标去找答案，顺序一变就会对错题。
    所以先按层排序（`sort` 稳定，同层内保持原顺序），最后统一编号。
    """
    out = []
    for field, fallback in _FIELD_LAYER:
        for q in (card.get(field) or []):
            if not isinstance(q, dict) or not q.get("question"):
                continue
            out.append({
                "layer": q.get("layer") or fallback,
                "question": q.get("question"),
                "options": list(q.get("options") or []),
                "answer": q.get("answer"),
                "explanation": q.get("explanation"),
                "error_reason": q.get("error_reason"),
                "objective": q.get("objective"),
            })
    order = {name: i for i, name in enumerate(LAYER_ORDER)}
    out.sort(key=lambda q: order.get(q["layer"], len(order)))
    for i, q in enumerate(out):
        q["index"] = i
    return out


def seal(questions):
    """剥掉答案与解析，只留题面。

    下发的是这一份；判卷时服务端重新从库里取完整题目。
    保留 `index`——它是**在完整题库里的下标**，判卷靠它定位答案。
    所以下面按层过滤只是选出子集，下标不动。
    """
    return [{k: v for k, v in q.items() if k not in SEALED_DROP} for q in questions]


def review_questions(card):
    """复习该考的题：`day1` 与 `day7` 两层。

    为什么不考 `immediate`：那一层的语义是「学完当场测」，它属于初学，
    不该在后续每次复习里反复出现——那会让复习永远停在同一层。

    老卡（v1）可能只有 immediate 层（没有复习题），这时回退到全部，
    否则它永远进不了复习。
    """
    all_q = merge_questions(card)
    later = [q for q in all_q if q["layer"] in ("day1", "day7")]
    return later or all_q


def study_questions(card):
    """初学时该做的题：`immediate` 层。没有就回退到全部。"""
    all_q = merge_questions(card)
    now = [q for q in all_q if q["layer"] == "immediate"]
    return now or all_q


def layering(questions):
    """各层各有多少题——前端用它显示"这次考哪几层"。"""
    counts = {}
    for q in questions:
        counts[q["layer"]] = counts.get(q["layer"], 0) + 1
    return counts


def queue_card(card):
    """复习队列里下发给前端的卡片：题库已密封（只含复习层）。"""
    return sealed_card(card, review_questions(card))


def study_card(card):
    """学习/自测时下发的卡片：题库已密封（只含 `immediate` 层）。

    `immediate` 的语义是「学完当场测」，所以阅读页的自测用它；
    后续回忆交给复习流程。
    """
    return sealed_card(card, study_questions(card))


# 任何时刻都只能把这些字段的**题干**下发，不能带答案
_OPEN_QUESTION_KEEP = ("question",)


def sealed_card(card, questions):
    """下发给前端的卡片：题库与参考答案全部密封。

    `quiz` / `review_quiz` / `open_question` 一并去掉——留着任何一个
    都等于把答案又发回去了。`open_question` 只保留题干（前端要显示问题），
    参考答案与评分要点由服务端在判分时自己取。
    """
    out = {k: v for k, v in card.items()
           if k not in ("quiz", "review_quiz", "open_question")}
    oq = card.get("open_question")
    if isinstance(oq, dict) and oq.get("question"):
        out["open_question"] = {k: oq.get(k) for k in _OPEN_QUESTION_KEEP}
        out["has_open_question"] = True
    out["questions"] = seal(questions)
    out["layers"] = layering(questions)
    out["question_count"] = len(out["questions"])
    return out


def open_question_of(card):
    """取这张卡的简答题（含参考答案）。**只给服务端判分用，不下发。**"""
    oq = card.get("open_question")
    if not isinstance(oq, dict):
        return None
    if not oq.get("reference_answer"):
        return None
    return oq


def seal_cards(cards):
    """批量密封（列表接口用）。"""
    return [study_card(c) for c in (cards or [])]


def confidence_proxy(elapsed_ms):
    """用作答时长近似「他当时有多确定」。

    为什么不弹一个「你刚才有多确定？」的选项：每道题多一次点击，
    复习会变得很烦，而拿到的只是一个自评。用时长的信号更诚实，
    也**不增加任何操作成本**。

    语义（与 `mastery.update_score` 的用法对齐）：
    - 很快答对 → 高置信，掌握度涨得多
    - 很快答错 → **高置信地答错**，最危险的信号（他建立了错误的信心），
      掌握度额外多扣。这正是这一步要抓的东西。
    - 犹豫很久 → 低置信，答对算「勉强」，答错也不算意外
    """
    if elapsed_ms is None:
        return None
    ms = max(0, int(elapsed_ms))
    if ms <= 6000:
        return 0.9
    if ms <= 20000:
        return 0.6
    return 0.3


def _v2_context(card):
    """这张卡是不是 v2 生成的？是的话给出 (goal_key, concept, draft_id)。

    只认 `_bridge.origin == "v2"`：v1 老卡没有里程碑与目标的概念，
    硬塞进 v2 的掌握度模型只会污染它。
    """
    b = card.get("_bridge") or {}
    if b.get("origin") != "v2":
        return None, None, None
    return b.get("goal_key"), b.get("milestone"), b.get("draft_id")


def answer_question(card_key, index, chosen, elapsed_ms=None, hint_used=False,
                    now=None):
    """服务端判卷。返回 (反馈, 错误信息)。

    反馈里的 `error_reason` 是范围决策与方案 M4 都要求的：
    「用户能查看错误原因，而不只看到正确答案」。
    """
    card = db.get_card(card_key)
    if not card:
        return None, "卡片不存在"
    questions = merge_questions(card)
    if not (0 <= index < len(questions)):
        return None, "题号越界"
    q = questions[index]
    options = q.get("options") or []
    if not (0 <= chosen < len(options)):
        return None, "选项越界"
    if q.get("answer") is None or not (0 <= q["answer"] < len(options)):
        # 题目本身没有可判的答案（老卡可能有）。这属于数据问题，
        # 不能假装用户答错了——那会污染掌握度。
        return None, "这道题没有可用答案，无法判卷"

    correct = chosen == q["answer"]
    feedback = {
        "card_key": card_key,
        "index": index,
        "layer": q["layer"],
        "objective": q.get("objective"),
        "correct": correct,
        "chosen": chosen,
        "correct_index": q["answer"],
        "correct_option": options[q["answer"]],
        "explanation": q.get("explanation") or "",
        # 答对时不显示错误原因（那会让人以为自己在犯错）
        "error_reason": None if correct else (q.get("error_reason") or ""),
        "mastery": None,
    }

    goal_key, concept, draft_id = _v2_context(card)
    if goal_key and concept:
        state = mastery.record(
            goal_key, concept, correct,
            confidence=confidence_proxy(elapsed_ms),
            elapsed_ms=elapsed_ms, hint_used=hint_used,
            error_type=None if correct else (q.get("error_reason") or "理解偏差"),
            detail=q.get("objective"),
            draft_id=draft_id, question_idx=index, now=now,
        )
        feedback["mastery"] = {
            "score": round(state.get("score") or 0.0, 3),
            "attempts": state.get("attempts"),
            "correct": state.get("correct"),
            "next_review_at": state.get("next_review_at"),
            "goal_key": goal_key,
            "concept": concept,
        }
    return feedback, None


def finish_card(card_key, correct, total, now=None):
    """一张卡复习完成：推进 v1 的 SM-2 状态。返回 (结果, 错误信息)。

    `correct` / `total` 由客户端上报。为什么不服务端自己算：v1 老卡没有
    逐题作答记录（v2 卡有，在 `v2_review_log` 里），为这一个数字引入
    会话状态或一张新表并不划算。要防的不是恶意作弊，而是**泄题**——
    那件事已经由 `seal` 解决了。所以这里只做范围钳制，不做事后审计。
    """
    card = db.get_card(card_key)
    if not card:
        return None, "卡片不存在"
    try:
        total = int(total or 0)
        correct = int(correct or 0)
    except (TypeError, ValueError):
        return None, "correct / total 必须是整数"
    if total <= 0:
        return None, "total 必须大于 0"
    if not (0 <= correct <= total):
        return None, "correct 必须在 0..total 之间"

    passed = correct >= _ceil_ratio(total)
    quality = 1 if passed else 0
    state = db.schedule_review(card_key, quality)
    if state is None:
        return None, "调度失败"
    return {
        "card_key": card_key,
        "correct": correct,
        "total": total,
        "passed": passed,
        "quality": quality,
        "state": state,
    }, None


def _ceil_ratio(total, ratio=PASS_RATIO):
    """答对几题算过。向上取整——1 道题时答对 1 道才算过。"""
    import math
    return max(1, int(math.ceil(total * ratio)))


def daily_summary(goal_key=None, now=None):
    """今天的复习概览，给前端做「今天该学什么」的头部。

    方案 M2/M4 要求这一步可解释：为什么是这几张卡。
    """
    import assessment
    due = assessment.due_reviews(goal_key, now=now) if goal_key else []
    plan = assessment.daily_plan(goal_key, now=now) if goal_key else None
    return {
        "goal_key": goal_key,
        "due_count": len(due),
        "due": [{"draft_id": d.get("draft_id"), "concept": d.get("concept"),
                 "question_count": len(d.get("items") or [])} for d in due],
        "plan": plan,
    }
