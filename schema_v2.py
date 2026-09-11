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

# 段落上限。真实跑通后发现：不设上限时模型会把一张卡写成一篇综述
# （一次实测写了 13 段 2192 字），那不是「一目标一卡」，是一篇文章。
MAX_BLOCKS_PER_KIND = {"explanation": 5, "examples": 3, "boundaries": 3}

# 正文段落的下限。真人盲评反馈「正文被结构字段挤薄」之后加的：
# 一段解释至少要有「论断 + 一层推演」的量，两句话就收尾的段落不算解释。
MIN_EXPLANATION_CHARS = 100
MIN_EXAMPLE_CHARS = 80
MIN_TRANSFER_CHARS = 15

# 中文技术材料的阅读速度约每分鐘 300-400 字，取中间值估算单卡时长。
_CHARS_PER_MINUTE = 320


def estimate_minutes(draft):
    """按正文体量估单卡时长（4-15 分钟）。确定性计算，不交给模型。"""
    prose = sum(len((b.get("text") or "")) for _k, b in iter_blocks(draft))
    return max(4, min(15, round(prose / _CHARS_PER_MINUTE)))

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
        cap = MAX_BLOCKS_PER_KIND.get(kind)
        if cap and len(blocks) > cap:
            errs.append("%s 有 %d 段 > 上限 %d（一张卡不是一篇文章）"
                        % (kind, len(blocks), cap))
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


def _check_blocks(errs, draft, kind, min_chars, min_n, max_n):
    blocks = draft.get(kind)
    if blocks is None:
        errs.append("缺字段: %s" % kind)
        return
    if not isinstance(blocks, list):
        errs.append("%s 应为数组" % kind)
        return
    if len(blocks) < min_n:
        errs.append("%s 只有 %d 段 < %d" % (kind, len(blocks), min_n))
    if len(blocks) > max_n:
        errs.append("%s 有 %d 段 > %d（一张卡不是一篇文章）" % (kind, len(blocks), max_n))
    for i, block in enumerate(blocks):
        if not isinstance(block, dict):
            errs.append("%s[%d] 不是对象" % (kind, i))
            continue
        text = (block.get("text") or "").strip()
        if len(text) < min_chars:
            errs.append("%s[%d].text 过短（%d 字 < %d，没有展开「为什么」）"
                        % (kind, i, len(text), min_chars))
        cites = block.get("cites")
        if not isinstance(cites, list) or not cites:
            errs.append("%s[%d].cites 为空（事实性段落必须引用证据）" % (kind, i))
        elif not all(isinstance(c, int) for c in cites):
            errs.append("%s[%d].cites 必须是整数 claim_idx 数组" % (kind, i))


def validate_card_body(body):
    """校验正文那一次调用的输出（不含边界/关键点/迁移任务）。"""
    errs = []
    if not isinstance(body, dict):
        return ["正文必须是 JSON 对象"]
    for key in ("schema_version", "objective", "title", "lead", "explanation", "examples"):
        if key not in body:
            errs.append("缺字段: %s" % key)
    if body.get("schema_version") != SCHEMA_VERSION:
        errs.append("schema_version 应为 %s" % SCHEMA_VERSION)

    objective = (body.get("objective") or "").strip()
    if not objective:
        errs.append("objective 为空（卡片必须服务唯一明确的学习目标）")
    elif len(objective) > 80:
        errs.append("objective %d 字 >80，说明目标不唯一" % len(objective))
    if not (body.get("title") or "").strip():
        errs.append("title 为空")
    if not (body.get("lead") or "").strip():
        errs.append("lead 为空")

    _check_blocks(errs, body, "explanation", MIN_EXPLANATION_CHARS, 3, 5)
    _check_blocks(errs, body, "examples", MIN_EXAMPLE_CHARS, 1, 3)
    return errs


def validate_card_structure(struct):
    """校验结构那一次调用的输出（边界/关键点/迁移任务）。"""
    errs = []
    if not isinstance(struct, dict):
        return ["结构必须是 JSON 对象"]
    for key in ("boundaries", "key_points", "transfer_task"):
        if key not in struct:
            errs.append("缺字段: %s" % key)

    # 讲不出真实边界时宁可不写，所以 boundaries 允许为空数组，但不许类型错
    blocks = struct.get("boundaries")
    if blocks is not None and not isinstance(blocks, list):
        errs.append("boundaries 应为数组")
    elif isinstance(blocks, list):
        if len(blocks) > MAX_BLOCKS_PER_KIND["boundaries"]:
            errs.append("boundaries 有 %d 条 > %d" % (len(blocks), MAX_BLOCKS_PER_KIND["boundaries"]))
        for i, block in enumerate(blocks):
            if not isinstance(block, dict):
                errs.append("boundaries[%d] 不是对象" % i)
                continue
            if len((block.get("text") or "").strip()) < _MIN_BLOCK_TEXT:
                errs.append("boundaries[%d].text 过短（<%d 字）" % (i, _MIN_BLOCK_TEXT))
            cites = block.get("cites")
            if not isinstance(cites, list) or not cites:
                errs.append("boundaries[%d].cites 为空（边界必须有证据依据）" % i)
            elif not all(isinstance(c, int) for c in cites):
                errs.append("boundaries[%d].cites 必须是整数 claim_idx 数组" % i)

    kp = struct.get("key_points")
    if not isinstance(kp, list) or len(kp) < 2:
        errs.append("key_points 至少 2 条")
    elif len(kp) > 5:
        errs.append("key_points %d 条 >5" % len(kp))

    if len((struct.get("transfer_task") or "").strip()) < MIN_TRANSFER_CHARS:
        errs.append("transfer_task 过短或缺失（迁移任务是检验是否真学会的关键）")
    return errs


def merge_card(body, struct):
    """把两次调用的输出合成一份完整 CardDraft。"""
    draft = dict(body)
    draft["boundaries"] = (struct or {}).get("boundaries") or []
    draft["key_points"] = (struct or {}).get("key_points") or []
    draft["transfer_task"] = (struct or {}).get("transfer_task") or ""
    # 时长由程序按体量算，不问模型——省一次判断，也避免它自报「7 分钟」糊弄
    draft["estimated_minutes"] = estimate_minutes(draft)
    return draft


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

# 英文月份名 → 数字。没有这一步，英文材料里的 "September 8, 2026" 与
# 模型译成的「2026 年 9 月 8 日」对不上，门禁会把正确翻译误判为编造。
_MONTHS = {
    "jan": "1", "feb": "2", "mar": "3", "apr": "4", "may": "5", "jun": "6",
    "jul": "7", "aug": "8", "sep": "9", "oct": "10", "nov": "11", "dec": "12",
}
_MONTH_RE = re.compile(r"\b(jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\b", re.I)


def normalize_numbers(text):
    """归一化数字表达，让中英混排材料的数字可以互相对齐。

    两处归一化：英文月份名 → 数字（September → 9）、千分位逗号去掉
    （8,192 → 8192）。少任何一处，模型写对了也会被判成编造。
    """
    if not text:
        return ""

    def repl(m):
        return _MONTHS.get(m.group(1)[:3].lower(), m.group(0))

    text = _MONTH_RE.sub(repl, text)
    return re.sub(r"(?<=\d),(?=\d{3}(?!\d))", "", text)


# 型号名/许可证名里的数字：「PatchTST-FM-r2」「Apache-2.0」「OpenMDW-1.0」。
# 它们是专有名词的一部分，不是可核验事实，当数字查会大量误报。
# 要求开头至少 2 个字母，避免把「v3.1.4」这类真版本号也一并放过。
_IDENTIFIER = re.compile(r"[A-Za-z]{2,}[A-Za-z0-9]*(?:[-_.][A-Za-z0-9]+)+")


def numbers_to_check(text):
    """抽取需要核验的数字，跳过专有名词里的数字。"""
    return NUMBER_RE.findall(_IDENTIFIER.sub(" ", text or ""))
