# -*- coding: utf-8 -*-
"""微知 v2 · 卡片写作器（CP3）。

把 GoalSpec + 证据片段 → 一份受证据约束的中文 CardDraft。

和 v1 的关键差别：v1 是「整篇正文塞进 prompt，模型自己找重点」，
所以生成的事实无法回溯、无法做数字核验。这里只喂已经定位好的证据，
并要求每段标出引用编号——模型负责讲清楚，**不负责决定什么是事实**。

流程：校验 GoalSpec → 校验证据数量（方案 3.1 至少两条）→ 组装输入 →
调 Provider（内置 Schema 校验与重试）→ 校验引用有效性 → 落 v2_card_drafts。

注意：本模块**不做发布**。质量门禁（CP4）通过之后才允许成为候选包。

用法::

    from providers import FakeTextProvider
    from card_writer import write_card
    draft, draft_id = write_card(FakeTextProvider(...), goal, claims)
"""
import card_gates
import schema_v2
import db
import evidence
import providers
from prompts import BANNED_PHRASES

TASK = "card_writer"


def render_evidence(claims):
    """把证据渲染成给模型看的编号清单。编号即 cites 要填的值。"""
    return "\n".join(
        "[#%d][%s] %s" % (c.get("claim_idx", i), c.get("kind") or "fact",
                           (c.get("text") or "").strip())
        for i, c in enumerate(claims)
    )


def build_inputs(goal, claims, source=None, objective=None, repair_notes=None):
    """组装模型输入。所有值都是字符串/整数，方便做哈希与缓存。

    `repair_notes` 是门禁拦截意见，只在「修复一次」那一轮才有内容。
    """
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


def _make_validator(claims):
    """Schema 校验器：结构校验 + 引用有效性。

    引用了不存在的证据编号属于可重试错误——交给 Provider 重试比在这里直接判死更划算。
    """
    def validate(draft):
        errs = schema_v2.validate_card_draft(draft)
        unknown = schema_v2.unknown_cites(draft, claims)
        if unknown:
            errs.append("引用了不存在的证据编号: %s"
                        % "、".join("#%s(%s)" % (c, k) for k, c in unknown))
        return errs
    return validate


def _prepare_claims(goal, claims, max_claims):
    """过滤 + 选取要喂给模型的证据。证据太多时模型会把一张卡写成综述。"""
    usable = [c for c in (claims or []) if c.get("usable", True)]
    return evidence.select_claims(usable, goal, limit=max_claims)


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

    inputs = build_inputs(goal, claims, source, objective)
    prompt_version = prompt_version or provider.prompt_version
    input_hash = providers.make_input_hash(TASK, prompt_version, provider.name, inputs)

    draft = provider.generate_json(
        TASK, _make_validator(claims), inputs, idempotency_key=input_hash
    )

    draft_id = db.save_v2_card_draft(
        input_hash=input_hash,
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
    validator = _make_validator(claims)

    inputs = build_inputs(goal, claims, source, objective)
    base_hash = providers.make_input_hash(TASK, prompt_version, provider.name, inputs)

    draft = provider.generate_json(TASK, validator, inputs, idempotency_key=base_hash)
    issues = card_gates.run_gates(draft, claims)

    repaired = 0
    while issues and repaired < max_repair:
        repaired += 1
        repair_inputs = build_inputs(
            goal, claims, source, objective,
            repair_notes=card_gates.render_issues(issues),
        )
        draft = provider.generate_json(
            TASK, validator, repair_inputs,
            idempotency_key="%s:repair%d" % (base_hash, repaired),
        )
        issues = card_gates.run_gates(draft, claims)

    report = card_gates.gate_report(draft, claims)
    report["repair_attempts"] = repaired

    draft_id = db.save_v2_card_draft(
        input_hash=base_hash,
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
    if issues:
        return False, "门禁未过: " + "；".join("%s(%s)" % i for i in issues)

    db.set_v2_card_draft_status(draft_id, "published", gate_report=card_gates.gate_report(row["payload"], claims))
    return True, "ok"
