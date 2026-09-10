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


def build_inputs(goal, claims, source=None, objective=None):
    """组装模型输入。所有值都是字符串/整数，方便做哈希与缓存。"""
    source = source or {}
    return {
        "schema_version": schema_v2.SCHEMA_VERSION,
        "banned": "、".join(BANNED_PHRASES),
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


def write_card(provider, goal, claims, source=None, source_id=None,
               objective=None, prompt_version=None):
    """生成一份 CardDraft 并落库。返回 (draft, draft_id)。

    抛 ValueError：GoalSpec 不合法或证据不足——这是**输入问题**，重试没有意义。
    抛 ProviderError：模型侧失败——已重试过，调用方可记录后跳过。
    """
    errs = schema_v2.validate_goal_spec(goal)
    if errs:
        raise ValueError("GoalSpec 不合法: " + "；".join(errs))

    claims = [c for c in (claims or []) if c.get("usable", True)]
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
