# -*- coding: utf-8 -*-
"""微知 v2 · 卡片质量门禁（CP4）。

方案 M1 要求生成后执行四项检查：引用覆盖、数字一致性、禁用表达、正文密度。
门禁的职责是「拦截」，不是「修改」——它只报告问题，改由 CardWriter 带着问题重生成。

签名对齐 `daily_check.rule_check` 的既有惯例：每个检查返回
`[(问题标签, 详情)]`，空列表 = 通过。这样门禁结果可以直接喂给
现有的质检报告与 badcase 汇总，不用另造一套。

关于阈值：`MIN_BODY_CHARS` / `MAX_BODY_CHARS` 是**实验变量不是产品规则**。
方案 2.2 明确把「每张卡固定 800-1000 字最合适」列为未验证假设，所以这里只设
一个宽口径的上下限防止空壳和灌水，具体目标字数交给 M6 的真实实验去定。
"""
import re

import schema_v2
from prompts import BANNED_PHRASES

# 宽口径护栏：只挡空壳与灌水，不写死「必须 800-1000 字」
MIN_BODY_CHARS = 200
MAX_BODY_CHARS = 2000
MIN_PROSE_CHARS = 120          # 解释+例子+边界的最少字数，防止只有干条条
MIN_BLOCKS = 2                 # 事实性段落总数下限

_CLAIM_INDEX = {}


def _claim_text(claims):
    return {c.get("claim_idx"): (c.get("text") or "") for c in (claims or [])}


def check_citation_coverage(draft, claims=None):
    """引用覆盖：每个事实性段落都要有 cites，且 cites 必须指向存在的证据。

    这是「所有事实性段落都关联至少一条 EvidenceClaim」这条验收门槛的落点。
    """
    issues = []
    valid = set(_claim_text(claims))
    for kind, block in schema_v2.iter_blocks(draft):
        cites = block.get("cites")
        if not cites:
            issues.append(("缺引用", "%s 段落未标注 cites" % kind))
            continue
        dangling = [c for c in cites if c not in valid]
        if dangling:
            issues.append(("引用悬空", "%s 引用了不存在的证据 %s" % (kind, dangling)))
    return issues


def check_number_consistency(draft, claims=None):
    """数字一致性：正文里的数字必须能在它所引用的证据原文中定位到。

    只查阿拉伯数字。中文数字（「四个阶段」）不在核验范围——它本来就写不精确，
    真要核验的是模型有没有编造「87.5%」「2025 年 3 月」这类硬数字。
    """
    issues = []
    texts = {k: schema_v2.normalize_numbers(v) for k, v in _claim_text(claims).items()}
    for kind, block in schema_v2.iter_blocks(draft):
        cited = "\n".join(texts.get(c, "") for c in (block.get("cites") or []))
        if not cited:
            continue  # 无引用由引用覆盖门禁负责，这里不重复报
        cited = schema_v2.normalize_numbers(cited)
        for num in schema_v2.NUMBER_RE.findall(block.get("text") or ""):
            if num not in cited:
                issues.append((
                    "数字无出处",
                    "%s 中的「%s」在所引证据里找不到" % (kind, num),
                ))
    return issues


def check_banned_phrases(draft, claims=None):
    """禁用表达：命中「值得注意」「在当今时代」这类通用填充句即拦截。

    方案 M1 验收门槛点名了这两句。词表在 prompts.BANNED_PHRASES，
    提示词里禁一次、门禁里再查一次——模型会漏，所以不能只靠提示词。
    """
    issues = []
    body = schema_v2.body_text(draft)
    for phrase in BANNED_PHRASES:
        if phrase in body:
            issues.append(("通用填充句", "命中禁用表达「%s」" % phrase))
    return issues


def check_body_density(draft, claims=None):
    """正文密度：防空壳、防灌水、防只有干条条。"""
    issues = []
    body = schema_v2.body_text(draft)
    n = len(body.strip())
    if n < MIN_BODY_CHARS:
        issues.append(("正文过短", "全文 %d 字 <%d" % (n, MIN_BODY_CHARS)))
    elif n > MAX_BODY_CHARS:
        issues.append(("正文过长", "全文 %d 字 >%d" % (n, MAX_BODY_CHARS)))

    prose = sum(
        len((b.get("text") or "").strip())
        for _, b in schema_v2.iter_blocks(draft)
    )
    if prose < MIN_PROSE_CHARS:
        issues.append(("展开不足", "解释/例子/边界合计 %d 字 <%d" % (prose, MIN_PROSE_CHARS)))

    blocks = list(schema_v2.iter_blocks(draft))
    if len(blocks) < MIN_BLOCKS:
        issues.append(("段落过少", "事实性段落 %d 个 <%d" % (len(blocks), MIN_BLOCKS)))
    return issues


GATES = (
    check_citation_coverage,
    check_number_consistency,
    check_banned_phrases,
    check_body_density,
)


def run_gates(draft, claims=None):
    """跑全部门禁，返回 [(标签, 详情)]。空列表 = 通过。"""
    issues = []
    for gate in GATES:
        issues.extend(gate(draft, claims))
    return issues


def render_issues(issues):
    """把门禁结果渲染成给模型看的修改意见（用于修复一次的重生成）。"""
    if not issues:
        return "（无）"
    return "\n".join("- [%s] %s" % (tag, detail) for tag, detail in issues)


def gate_report(draft, claims=None):
    """结构化门禁报告，落 v2_card_drafts.gate_report。"""
    per_gate = {}
    all_issues = []
    for gate in GATES:
        found = gate(draft, claims)
        per_gate[gate.__name__] = found
        all_issues.extend(found)
    tags = {}
    for tag, _ in all_issues:
        tags[tag] = tags.get(tag, 0) + 1
    return {
        "passed": not all_issues,
        "issues": [{"tag": t, "detail": d} for t, d in all_issues],
        "by_gate": per_gate,
        "issue_distribution": tags,
    }
