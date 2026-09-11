# -*- coding: utf-8 -*-
"""微知 v2 · 题目质量与延迟复习闭环（CP12，方案 M4）。

方案 M4 的目标是「用**真实回忆表现**，而不是阅读完成状态，决定后续学习」。
这带来两条硬要求：

1. **每道题都要能映射到学习目标与证据**——否则答错了也不知道是哪个能力缺口，
   掌握度就无从更新，整条自适应链路断在这里。
2. **题目质量要有客观门禁**：答案唯一、选项无歧义、题干不泄露答案。
   方案 M4 的验收门槛写的是「Golden Set 中不存在两个合理正确选项」，
   而「两个都说得通」是模型出题最典型的失败模式，必须能自动拦下。

所以本模块的检查全部是确定性的字符串/结构分析，不靠模型自评。
模型只负责出题，判卷由程序做。

复习闭环：到期 → 作答 → 反馈（含错误原因）→ 更新掌握度 → 调整下次间隔。
反馈必须给 error_reason——方案 M4 明确「用户能查看错误原因，而不只看到正确答案」。

用法::

    import assessment
    items = assessment.generate(provider, draft, claims, concept="m2")
    fb = assessment.submit("g-1", "m2", draft_id, item_idx=0, chosen=2, confidence=0.8)
"""
import re

from datetime import datetime

import db
import mastery

TASK = "quiz_gen"

# 题量由学习目标决定，不是固定三道（范围决策 D1）。
# 传 None 时交给模型按目标判断；MAX_COUNT 只是防跑飞的安全上限，
# 不是产品规则——「每卡三道题」已被明确废除。
MAX_COUNT = 5
LAYERS = ("immediate", "day1", "day7")
LAYER_HINT = ("由学习目标决定数量：单一判断目标出 1 道，"
              "同时含概念、边界与迁移的目标才出 3 道。宁可少出，不要拿同义重复凑数。")

# 语面歧义套路：出现即判坏题
_AMBIGUOUS = re.compile(r"(以上都|以上均|都不对|都正确|全部正确|以上皆)")
# 干扰项长度差超过这个比例就算参差（正确答案往往被写得特别长，成了破题线索）
_MAX_LEN_SKEW = 0.6
MIN_QUESTION_CHARS = 10
MIN_OPTION_CHARS = 2


def _norm(text):
    """归一化：去掉空白与标点，用于选项去重与泄露检测。"""
    return re.sub(r"[\s，。、；：（）()\[\]【】,.;:!！?？\"'“”‘’\-—]", "", (text or "")).lower()


# ---------- 结构与质量校验 ----------

def validate_quiz(data):
    """结构校验，返回错误列表。"""
    errs = []
    if not isinstance(data, dict):
        return ["题目集必须是 JSON 对象"]
    items = data.get("items")
    if not isinstance(items, list) or not items:
        return ["items 应为非空数组"]
    if len(items) > MAX_COUNT:
        errs.append("items %d 道 >%d" % (len(items), MAX_COUNT))
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            errs.append("items[%d] 应为对象" % i)
            continue
        # 方案 M4 门槛：100% 题目映射到学习目标
        if len((item.get("objective") or "").strip()) < 4:
            errs.append("items[%d].objective 缺失（题目必须映射到学习目标）" % i)
        cites = item.get("cites")
        if not isinstance(cites, list) or not cites:
            errs.append("items[%d].cites 为空（答案必须能追溯到证据）" % i)
        elif not all(isinstance(c, int) for c in cites):
            errs.append("items[%d].cites 必须是整数数组" % i)
        if len((item.get("question") or "").strip()) < MIN_QUESTION_CHARS:
            errs.append("items[%d].question 过短" % i)
        opts = item.get("options")
        if not isinstance(opts, list) or len(opts) < 3:
            errs.append("items[%d].options 少于 3 个" % i)
            continue
        if not all(str(o).strip() for o in opts):
            errs.append("items[%d].options 有空选项" % i)
        ans = item.get("answer")
        if not isinstance(ans, int) or not (0 <= ans < len(opts)):
            errs.append("items[%d].answer 越界" % i)
        if not (item.get("explanation") or "").strip():
            errs.append("items[%d].explanation 为空" % i)
        # 方案 M4：用户要能看到错误原因，不能只给正确答案
        if not (item.get("error_reason") or "").strip():
            errs.append("items[%d].error_reason 为空（选错了要知道错在哪）" % i)
        # 范围决策 D2：随堂题与复习题合并为分层题库
        layer = item.get("layer")
        if layer not in LAYERS:
            errs.append("items[%d].layer=%r 不在 %s 里" % (i, layer, "/".join(LAYERS)))
    if items and all(isinstance(x, dict) for x in items):
        if not any(x.get("layer") == "immediate" for x in items):
            errs.append("至少要有一道 layer=immediate 的题（学完当场要能测）")
    return errs


def check_answer_uniqueness(item):
    """答案唯一性：选项必须互斥，且不能出现相互包含的「都对」型选项。

    「两个选项都说得通」是模型出题最典型的失败模式，
    也是方案 M4 验收门槛点名要拦的东西。
    """
    issues = []
    opts = [str(o).strip() for o in (item.get("options") or [])]
    seen = {}
    for i, o in enumerate(opts):
        key = _norm(o)
        if key in seen:
            issues.append(("选项重复", "第 %d 项与第 %d 项完全相同" % (seen[key] + 1, i + 1)))
        else:
            seen[key] = i
    for i, a in enumerate(opts):
        for j, b in enumerate(opts):
            if i != j and _norm(a) and _norm(a) in _norm(b):
                issues.append(("选项包含", "第 %d 项被第 %d 项包含，两者可能都成立" % (i + 1, j + 1)))
    return issues


def check_option_ambiguity(item):
    """选项歧义：语面套路 + 长度参差。"""
    issues = []
    for i, o in enumerate(item.get("options") or []):
        if _AMBIGUOUS.search(str(o)):
            issues.append(("语面歧义", "第 %d 项是「%s」这类万能选项" % (i + 1, o)))
    opts = [str(o).strip() for o in (item.get("options") or [])]
    if len(opts) >= 3:
        lengths = [len(o) for o in opts]
        lo, hi = min(lengths), max(lengths)
        if hi and (hi - lo) / float(hi) > _MAX_LEN_SKEW:
            issues.append(("选项参差",
                           "最短 %d 字 / 最长 %d 字，正确答案容易被长度出卖" % (lo, hi)))
    return issues


def check_answer_leak(item):
    """答案泄露：题干里不该出现正确选项的原话。"""
    issues = []
    q = _norm(item.get("question"))
    opts = item.get("options") or []
    ans = item.get("answer")
    if not isinstance(ans, int) or not (0 <= ans < len(opts)):
        return issues
    correct = _norm(opts[ans])
    if len(correct) >= 6 and correct in q:
        issues.append(("答案泄露", "题干里出现了正确选项的原话"))
    else:
        # 正确选项的大部分内容出现在题干里，同样算泄露
        for piece in re.split(r"[，,、；;]", _norm(opts[ans])):
            if len(piece) >= 8 and piece in q:
                issues.append(("答案泄露", "题干里出现了正确选项的片段「%s」" % piece))
                break
    return issues


QUALITY_CHECKS = (check_answer_uniqueness, check_option_ambiguity, check_answer_leak)


def check_quiz_quality(items):
    """对整套题跑质量门禁，返回 [(标签, 详情)]。"""
    issues = []
    if len(items) < 1:
        return [("题目为空", "没有可用的题目")]
    for i, item in enumerate(items):
        for check in QUALITY_CHECKS:
            for tag, detail in check(item):
                issues.append((tag, "第 %d 题：%s" % (i + 1, detail)))
    return issues


def quality_report(items):
    """结构化质量报告，可直接喂给质检汇总（方案 6.3 的题目质量指标）。"""
    issues = check_quiz_quality(items)
    dist = {}
    for tag, _ in issues:
        dist[tag] = dist.get(tag, 0) + 1
    total = len(items) or 1
    mapped = sum(1 for it in items if (it.get("objective") or "").strip())
    coverage = round(mapped / float(total), 3)
    if coverage < 1.0:
        issues.append(("目标未全覆盖",
                       "%d/%d 道题映射到学习目标，方案 M4 要求 100%%" % (mapped, total)))
    return {
        # 方案 M4 门槛是「100% 题目映射到学习目标」，所以覆盖率不足也算不合格
        "passed": not issues,
        "total": len(items),
        "objective_coverage": coverage,   # 方案 M4：应为 1.0
        "issue_distribution": dist,
        "issues": [{"tag": t, "detail": d} for t, d in issues],
    }


# ---------- 生成 ----------

def build_inputs(draft, claims, count=None):
    import card_writer
    return {
        "objective": draft.get("objective") or "",
        "body_block": card_writer.render_body(draft),
        "evidence_block": card_writer.render_evidence(claims or []),
        "count_hint": ("固定出 %d 道。" % count) if count else LAYER_HINT,
    }


def generate(provider, draft, claims, concept=None, count=None, draft_id=None):
    """出题并落库。答错的题会被门禁拦下重试（由 Provider 负责重试次数）。

    返回 (items, report)。
    """
    valid_ids = {c.get("claim_idx") for c in (claims or [])}

    def validate(data):
        errs = validate_quiz(data)
        for i, item in enumerate(data.get("items") or []):
            for c in (item.get("cites") or []):
                if isinstance(c, int) and c not in valid_ids:
                    errs.append("items[%d] 引用了不存在的证据 #%s" % (i, c))
        return errs

    data = provider.generate_json(TASK, validate, build_inputs(draft, claims, count))
    items = [dict(item) for item in (data.get("items") or [])]
    report = quality_report(items)
    if draft_id is not None:
        items = attach(draft_id, items, concept=concept)
    else:
        items = _with_ids(items, concept)
    return items, report


def _with_ids(items, concept=None):
    """统一补齐题目 id 与归属。

    放在这里而不是让每个调用方自己加：题目一旦少了 id，
    复习闭环的「第几题」就对不上号，而这种错很晚才会暴露。
    """
    out = []
    for i, item in enumerate(items or []):
        it = dict(item)
        it["id"] = it.get("id") or "q%d" % (i + 1)
        if concept:
            it["concept"] = concept
        out.append(it)
    return out


def attach(draft_id, items, concept=None):
    """把题目挂到草稿上并落库，返回补齐 id 之后的题目集。"""
    items = _with_ids(items, concept)
    for it in items:
        it["draft_id"] = draft_id
    db.save_v2_draft_assessment(draft_id, items)
    return items


# ---------- 复习闭环 ----------

def _draft_for(draft_id):
    return db.get_v2_card_draft_by_id(draft_id)


def due_reviews(goal_key, now=None, limit=None):
    """到期该复习的卡（含题目与掌握度），风险高的排前面。

    这是方案 M4 的「到期复习 → 作答 → 反馈 → mastery 更新」的入口。
    只取已发布或草稿状态的卡；被门禁拒掉的卡不该进入复习。
    """
    out = []
    for state in mastery.due(goal_key, now=now):
        concept = state.get("concept")
        rows = [r for r in db.list_v2_card_drafts(limit=200)
                if r.get("goal_key") == goal_key and r.get("concept") == concept
                and r.get("status") in ("draft", "published")]
        for row in rows:
            items = row.get("assessment") or []
            if not items:
                continue
            out.append({
                "draft_id": row["id"],
                "title": (row.get("payload") or {}).get("title"),
                "concept": concept,
                "objective": row.get("objective"),
                "capability_gap": row.get("capability_gap"),
                "items": items,
                "risk": state.get("risk"),
                "mastery": state.get("score"),
            })
    return out[:limit] if limit else out


def submit(goal_key, concept, draft_id, question_idx, chosen, confidence=None,
           elapsed_ms=None, hint_used=False, now=None):
    """交一次作答，返回给用户看的反馈（含错误原因）。

    判卷由程序做：拿 item["answer"] 比对，不让模型参与——
    判卷要是也能错，掌握度整条链路就都不可信了。
    """
    row = _draft_for(draft_id)
    if not row:
        return None, "卡片不存在"
    items = row.get("assessment") or []
    if not (0 <= question_idx < len(items)):
        return None, "题目序号越界"
    item = items[question_idx]

    correct = (chosen == item.get("answer"))
    # 用户自报的错误类型优先（他不知道怎么归类时才留空）
    error_type = None if correct else _guess_error_type(item, chosen)

    state = mastery.record(
        goal_key, concept, correct,
        confidence=confidence, elapsed_ms=elapsed_ms, hint_used=hint_used,
        error_type=error_type, detail=item.get("question"), draft_id=draft_id,
        question_idx=question_idx, now=now,
    )
    return {
        "correct": correct,
        "chosen": chosen,
        "answer": item.get("answer"),
        "correct_option": (item.get("options") or [None])[item.get("answer")]
        if isinstance(item.get("answer"), int) and item.get("options") else None,
        "explanation": item.get("explanation"),
        # 方案 M4：用户能查看错误原因，而不只看到正确答案
        "error_reason": None if correct else item.get("error_reason"),
        "error_type": error_type,
        "objective": item.get("objective"),
        "mastery_after": state["score"],
        "next_review_at": state["next_review_at"],
    }, "ok"


def by_layer(items, layer):
    """按时间层取题。范围决策 D2：学完立即 / 24h / 7 天，不再分两套题库。"""
    return [it for it in (items or []) if it.get("layer") == layer]


def _guess_error_type(item, chosen):
    """粗分类错误类型。只做确定性判断，不给用户编心理活动。"""
    opts = item.get("options") or []
    if not isinstance(chosen, int) or not (0 <= chosen < len(opts)):
        return "无效选项"
    if _AMBIGUOUS.search(str(opts[chosen])):
        return "语面歧义"
    correct = opts[item.get("answer")] if isinstance(item.get("answer"), int) else ""
    if _norm(correct) in _norm(opts[chosen]) or _norm(opts[chosen]) in _norm(correct):
        return "概念混淆"
    if re.search(r"\d", str(correct)) or re.search(r"\d", str(opts[chosen])):
        return "数字记错"
    return "理解偏差"


def daily_plan(goal_key, now=None, review_limit=3, new_limit=1):
    """今天的安排：先复习到期的，再补一个最小新包。

    方案 M4「使用 mastery 和遗忘风险调整今日安排」的落点。
    复习优先于新内容——到期的卡不复习，前面投入的学习时间就白花了。
    """
    reviews = due_reviews(goal_key, now=now, limit=review_limit)
    return {
        "goal_key": goal_key,
        "reviews": reviews,
        "review_count": len(reviews),
        "new_pack_slots": max(0, new_limit) if len(reviews) < review_limit else 0,
        "reason": ("有 %d 张卡到期，先复习再学新的" % len(reviews)) if reviews
        else "没有到期复习，可以推进新的学习包",
    }
