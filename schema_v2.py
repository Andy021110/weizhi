# -*- coding: utf-8 -*-
"""微知 v2 · 版本化数据结构与校验（CP3）。

两份结构：

- `GoalSpec`：方案 2.3 要求把模糊愿望拆成目标能力 / 当前水平 / 使用场景 /
  成功证据 / 前置知识 / 里程碑 / 每日预算。M1 只定义结构，Builder 留到 M2。
- `CardDraft`：方案 M1 要求的输出——标题、导语、解释段、例子、边界、关键点、
  迁移任务，且**每个事实性段落都带 cites 指向证据**。

为什么不引入 jsonschema：仓库现状是「标准库 + feedparser / trafilatura / openai」，
为一个结构校验加依赖不划算。手写校验器的错误信息也更适合直接喂给门禁和人工审核。

`schema_version` 一变，缓存键与校验规则同时变化，旧草稿不会混进新版本。
"""
import re

SCHEMA_VERSION = "1.0"

# --- GoalSpec（方案 2.3）---

GOAL_FIELDS = ("key", "capability", "level", "scene", "success_evidence",
               "prereq", "milestones", "daily_minutes")


def make_goal(key, capability, level="", scene="", success_evidence="",
              prereq=None, milestones=None, daily_minutes=60):
    """构造 GoalSpec。M1 阶段由调用方填，M2 的 Builder 会生成它。"""
    return {
        "key": key,
        "capability": capability,
        "level": level,
        "scene": scene,
        "success_evidence": success_evidence,
        "prereq": list(prereq or []),
        "milestones": list(milestones or []),
        "daily_minutes": daily_minutes,
    }


def validate_goal_spec(goal):
    """校验 GoalSpec，返回错误列表（空 = 通过）。"""
    errs = []
    if not isinstance(goal, dict):
        return ["GoalSpec 必须是对象"]
    for key in ("key", "capability"):
        if not (goal.get(key) or "").strip():
            errs.append("缺字段: goal.%s" % key)
    # 方案 3.1：每个 active 目标都要有可观察的成功证据
    if not (goal.get("success_evidence") or "").strip():
        errs.append("缺字段: goal.success_evidence（没有成功证据的目标不可验证）")
    if not isinstance(goal.get("prereq", []), list):
        errs.append("goal.prereq 应为数组")
    if not isinstance(goal.get("milestones", []), list):
        errs.append("goal.milestones 应为数组")
    minutes = goal.get("daily_minutes")
    if minutes is not None and (not isinstance(minutes, int) or not (0 < minutes <= 240)):
        errs.append("goal.daily_minutes 应为 1-240 的整数")
    return errs


# --- CardDraft ---

FACTUAL_KINDS = ("explanation", "examples", "boundaries")

CARD_DRAFT_SCHEMA = {
    "required": ["schema_version", "objective", "title", "lead", "explanation",
                 "key_points", "transfer_task", "estimated_minutes"],
    "properties": {
        "schema_version": "string",
        "objective": "string",
        "title": "string",
        "lead": "string",
        "explanation": "array",
        "examples": "array",
        "boundaries": "array",
        "key_points": "array",
        "transfer_task": "string",
        "estimated_minutes": "integer",
    },
}

_MIN_BLOCK_TEXT = 20


def iter_blocks(draft):
    """遍历所有事实性段落，产出 (kind, block)。门禁与数字核验都基于它。"""
    for kind in FACTUAL_KINDS:
        for block in (draft.get(kind) or []):
            if isinstance(block, dict):
                yield kind, block


def validate_card_draft(draft):
    """校验 CardDraft，返回错误列表（空 = 通过）。"""
    errs = []
    if not isinstance(draft, dict):
        return ["CardDraft 必须是 JSON 对象"]

    for key in CARD_DRAFT_SCHEMA["required"]:
        if key not in draft:
            errs.append("缺字段: %s" % key)

    if draft.get("schema_version") != SCHEMA_VERSION:
        errs.append("schema_version 应为 %s，实际 %r" % (SCHEMA_VERSION, draft.get("schema_version")))

    # 方案 M1 验收：正文覆盖唯一明确的学习目标
    objective = (draft.get("objective") or "").strip()
    if not objective:
        errs.append("objective 为空（卡片必须服务唯一明确的学习目标）")
    elif len(objective) > 80:
        errs.append("objective %d 字 >80，说明目标不唯一" % len(objective))

    if not (draft.get("title") or "").strip():
        errs.append("title 为空")
    if not (draft.get("lead") or "").strip():
        errs.append("lead 为空")

    for kind in FACTUAL_KINDS:
        blocks = draft.get(kind)
        if blocks is None:
            continue
        if not isinstance(blocks, list):
            errs.append("%s 应为数组" % kind)
            continue
        for i, block in enumerate(blocks):
            if not isinstance(block, dict):
                errs.append("%s[%d] 不是对象" % (kind, i))
                continue
            if len((block.get("text") or "").strip()) < _MIN_BLOCK_TEXT:
                errs.append("%s[%d].text 过短（<%d 字）" % (kind, i, _MIN_BLOCK_TEXT))
            cites = block.get("cites")
            if not isinstance(cites, list) or not cites:
                errs.append("%s[%d].cites 为空（事实性段落必须引用证据）" % (kind, i))
            elif not all(isinstance(c, int) for c in cites):
                errs.append("%s[%d].cites 必须是整数 claim_idx 数组" % (kind, i))

    examples = draft.get("examples")
    if examples is not None and (not isinstance(examples, list) or not examples):
        errs.append("examples 至少要有 1 条")

    kp = draft.get("key_points")
    if kp is not None and (not isinstance(kp, list) or len(kp) < 2):
        errs.append("key_points 至少 2 条")

    if len((draft.get("transfer_task") or "").strip()) < 10:
        errs.append("transfer_task 过短或缺失（迁移任务是检验是否真学会的关键）")

    minutes = draft.get("estimated_minutes")
    if not isinstance(minutes, int) or not (3 <= minutes <= 15):
        errs.append("estimated_minutes 应为 3-15 的整数（单卡 5-10 分钟预算）")

    return errs


def unknown_cites(draft, claims):
    """返回 draft 中指向不存在证据的 cite（按 claim_idx 比对）。"""
    valid = {c.get("claim_idx") for c in (claims or [])}
    bad = []
    for kind, block in iter_blocks(draft):
        for c in (block.get("cites") or []):
            if c not in valid:
                bad.append((kind, c))
    return bad


def body_text(draft):
    """拼接全文，供字数统计与数字核验使用。"""
    parts = [draft.get("title") or "", draft.get("lead") or ""]
    for _, block in iter_blocks(draft):
        parts.append(block.get("text") or "")
    parts.extend(draft.get("key_points") or [])
    parts.append(draft.get("transfer_task") or "")
    return "\n".join(parts)


# 数字：整数、小数、百分数、版本号片段。用于数字一致性门禁。
NUMBER_RE = re.compile(r"\d+(?:\.\d+)?%?|\d+(?:\.\d+)*")
