# -*- coding: utf-8 -*-
"""微知 v2 · 卡片写作器（CP3）。

把 GoalSpec + 证据片段 → 一份受证据约束的中文 CardDraft。

和 v1 的关键差别：v1 是「整篇正文塞进 prompt，模型自己找重点」，
所以生成的事实无法回溯、无法做数字核验。这里只喂已经定位好的证据，
并要求每段标出引用编号——模型负责讲清楚，**不负责决定什么是事实**。

流程：校验 GoalSpec → 校验证据数量（方案 3.1 至少两条）→ 组装输入 →
**两次模型调用**（先写正文，再派生结构）→ 校验引用有效性 → 落 v2_card_drafts。

为什么是两次调用：最初一次调用同时产出正文 + 边界 + 关键点 + 迁移任务，
真人盲评三轮全部选了旧方案，反馈「有边界和迁移任务的卡，正文被挤短了」。
根因是 schema 里的硬字段会抢占注意力预算。拆开后第一次调用只管把正文讲深，
第二次在正文已定稿的基础上派生结构字段，两者不再争预算。

注意：本模块**不做发布**。质量门禁（CP4）通过之后才允许成为候选包。

用法::

    from weizhi.core.providers import FakeTextProvider
    from weizhi.produce.card_writer import write_card
    draft, draft_id = write_card(FakeTextProvider(...), goal, claims)
"""
from weizhi.produce import card_gates
from weizhi.core import schema_v2
from weizhi.core import db
from weizhi.produce import evidence
from weizhi.core import providers
from weizhi.core.prompts import BANNED_PHRASES

TASK_BODY = "card_body"
TASK_STRUCTURE = "card_structure"
TASK_DRAFT = "card_draft"


def render_evidence(claims):
    """把证据渲染成给模型看的编号清单。编号即 cites 要填的值。"""
    return "\n".join(
        "[#%d][%s] %s" % (c.get("claim_idx", i), c.get("kind") or "fact",
                           (c.get("text") or "").strip())
        for i, c in enumerate(claims)
    )


def _common_inputs(goal, claims, source, objective, repair_notes=None):
    source = source or {}
    return {
        "schema_version": schema_v2.SCHEMA_VERSION,
        "banned": "、".join(BANNED_PHRASES),
        "repair_notes": repair_notes or "（首次生成，无）",
        "objective": objective or goal.get("capability", ""),
        "learner_level": goal.get("level") or "未说明",
        "scene": goal.get("scene") or "未说明",
        "success_evidence": goal.get("success_evidence") or "未说明",
        "prereq_block": "、".join(goal.get("prereq") or []) or "未说明",
        "source_title": source.get("title") or "未标注来源",
        "source_url": source.get("url") or "未标注链接",
        "evidence_block": render_evidence(claims),
    }


def build_body_inputs(goal, claims, source=None, objective=None, repair_notes=None):
    """第一次调用（写正文）的输入。"""
    return _common_inputs(goal, claims, source, objective, repair_notes)


def build_structure_inputs(goal, claims, source=None, objective=None,
                           body=None, repair_notes=None):
    """第二次调用（派生结构）的输入。

    `body_block` 是已定稿的正文——结构字段必须基于正文派生，所以正文一变，
    这次调用的输入哈希就变，缓存自然失效。
    """
    inputs = _common_inputs(goal, claims, source, objective, repair_notes)
    inputs["body_block"] = render_body(body or {})
    return inputs


def render_body(body):
    """把正文渲染成给结构调用看的文本。"""
    lines = []
    if body.get("title"):
        lines.append("标题：" + body["title"])
    if body.get("lead"):
        lines.append("导语：" + body["lead"])
    for i, block in enumerate(body.get("explanation") or [], 1):
        lines.append("解释 %d：%s" % (i, block.get("text") or ""))
    for i, block in enumerate(body.get("examples") or [], 1):
        lines.append("例子 %d：%s" % (i, block.get("text") or ""))
    return "\n".join(lines)


def _with_cite_check(validator, claims):
    """给校验器加上引用有效性检查。

    引用了不存在的证据编号属于可重试错误——交给 Provider 重试比直接判死更划算。
    """
    def validate(data):
        errs = validator(data)
        unknown = schema_v2.unknown_cites(data, claims)
        if unknown:
            errs.append("引用了不存在的证据编号: %s"
                        % "、".join("#%s(%s)" % (c, k) for k, c in unknown))
        return errs
    return validate


def _prepare_claims(goal, claims, max_claims):
    """过滤 + 选取要喂给模型的证据。证据太多时模型会把一张卡写成综述。"""
    usable = [c for c in (claims or []) if c.get("usable", True)]
    return evidence.select_claims(usable, goal, limit=max_claims)


def _generate_draft(provider, goal, claims, source, objective,
                    prompt_version, repair_notes=None):
    """两次调用产出一份完整 CardDraft。返回 (draft, draft_hash)。"""
    body_inputs = build_body_inputs(goal, claims, source, objective, repair_notes)
    body_hash = providers.make_input_hash(
        TASK_BODY, prompt_version, provider.name, body_inputs)
    body = provider.generate_json(
        TASK_BODY, _with_cite_check(schema_v2.validate_card_body, claims),
        body_inputs, idempotency_key=body_hash,
    )

    struct_inputs = build_structure_inputs(
        goal, claims, source, objective, body, repair_notes)
    struct_hash = providers.make_input_hash(
        TASK_STRUCTURE, prompt_version, provider.name, struct_inputs)
    struct = provider.generate_json(
        TASK_STRUCTURE, _with_cite_check(schema_v2.validate_card_structure, claims),
        struct_inputs, idempotency_key=struct_hash,
    )

    draft = schema_v2.merge_card(body, struct)
    # 草稿身份的哈希包含正文与结构两个哈希：任一部分变了就是新版本
    draft_hash = providers.make_input_hash(
        TASK_DRAFT, prompt_version, provider.name,
        {"body": body_hash, "structure": struct_hash},
    )
    return draft, draft_hash


def write_card(provider, goal, claims, source=None, source_id=None,
               objective=None, prompt_version=None,
               max_claims=evidence._DEFAULT_MAX_CLAIMS):
    """生成一份 CardDraft 并落库。返回 (draft, draft_id)。

    抛 ValueError：GoalSpec 不合法或证据不足——这是**输入问题**，重试没有意义。
    抛 ProviderError：模型侧失败——已重试过，调用方可记录后跳过。
    """
    errs = schema_v2.validate_goal_spec(goal)
    if errs:
        raise ValueError("GoalSpec 不合法: " + "；".join(errs))

    claims = _prepare_claims(goal, claims, max_claims)
    if len(claims) < evidence.MIN_CLAIMS_FOR_PACK:
        raise ValueError(
            "可用证据 %d 条 < %d，不足以规划学习包（方案 3.1）"
            % (len(claims), evidence.MIN_CLAIMS_FOR_PACK)
        )

    prompt_version = prompt_version or provider.prompt_version
    draft, draft_hash = _generate_draft(
        provider, goal, claims, source, objective, prompt_version)

    draft_id = db.save_v2_card_draft(
        input_hash=draft_hash,
        schema_version=draft.get("schema_version", schema_v2.SCHEMA_VERSION),
        payload=draft,
        source_id=source_id,
        goal_key=goal.get("key"),
        objective=draft.get("objective"),
        status="draft",
    )
    return draft, draft_id


def write_card_gated(provider, goal, claims, source=None, source_id=None,
                     objective=None, prompt_version=None, max_repair=1,
                     max_claims=evidence._DEFAULT_MAX_CLAIMS):
    """带质量门禁的卡片生成：失败最多修复一次，仍不合格则 rejected 且不成为候选包。

    返回 (draft, draft_id, report)。
    - report["passed"] 为 True：草稿状态 draft，可被 `promote_to_candidate` 发布
    - report["passed"] 为 False：草稿状态 rejected，失败原因留在 gate_report 里

    为什么只修一次：方案 M1 明确「最多修复一次」。修多轮既烧钱，
    又会把模型反复拉扯成没有信息量的安全表述。
    """
    errs = schema_v2.validate_goal_spec(goal)
    if errs:
        raise ValueError("GoalSpec 不合法: " + "；".join(errs))

    claims = _prepare_claims(goal, claims, max_claims)
    if len(claims) < evidence.MIN_CLAIMS_FOR_PACK:
        raise ValueError(
            "可用证据 %d 条 < %d，不足以规划学习包（方案 3.1）"
            % (len(claims), evidence.MIN_CLAIMS_FOR_PACK)
        )

    prompt_version = prompt_version or provider.prompt_version

    draft, draft_hash = _generate_draft(
        provider, goal, claims, source, objective, prompt_version)
    issues = card_gates.run_gates(draft, claims)
    blocking, _warnings = card_gates.split_severity(issues)

    repaired = 0
    # 只对阻断级问题发起修复。只有警告时再花一次调用不划算，
    # 警告照常写进报告让人来判断。
    while blocking and repaired < max_repair:
        repaired += 1
        notes = card_gates.render_issues(blocking)
        draft, draft_hash = _generate_draft(
            provider, goal, claims, source, objective,
            "%s+repair%d" % (prompt_version, repaired), notes)
        issues = card_gates.run_gates(draft, claims)
        blocking, _warnings = card_gates.split_severity(issues)

    report = card_gates.gate_report(draft, claims)
    report["repair_attempts"] = repaired

    draft_id = db.save_v2_card_draft(
        input_hash=draft_hash,
        schema_version=draft.get("schema_version", schema_v2.SCHEMA_VERSION),
        payload=draft,
        source_id=source_id,
        goal_key=goal.get("key"),
        objective=draft.get("objective"),
        # 方案 M1：仍不合格则保留失败原因，不创建候选包
        status="draft" if report["passed"] else "rejected",
        gate_report=report,
    )
    return draft, draft_id, report


def promote_to_candidate(draft_id, claims):
    """把草稿提升为候选包。门禁没过的草稿一律拒绝，返回 (ok, reason)。

    这是方案第 7 节「生产与发布之间需要质量门禁」的最后一道闸门：
    发布前**重新跑一次门禁**，不信历史状态——草稿可能被手工改过。
    """
    row = db.get_v2_card_draft_by_id(draft_id)
    if not row:
        return False, "草稿不存在"
    if row["status"] == "published":
        return False, "已经是候选包"

    issues = card_gates.run_gates(row["payload"] or {}, claims)
    blocking, _warnings = card_gates.split_severity(issues)
    if blocking:
        return False, "门禁未过: " + "；".join("%s(%s)" % i for i in blocking)

    db.set_v2_card_draft_status(draft_id, "published", gate_report=card_gates.gate_report(row["payload"], claims))
    return True, "ok"
