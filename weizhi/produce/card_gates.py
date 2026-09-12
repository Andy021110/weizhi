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

from weizhi.core import schema_v2
from weizhi.core.prompts import BANNED_PHRASES

# 宽口径护栏：只挡空壳与灌水，不写死「必须 800-1000 字」。
# MAX 从 2400 提到 3200：拆成两次调用后正文明显变厚（实测 1987-2248 字），
# 而按每分鐘 320 字算，3200 字正好是「10 分钟」的上限，在方案允许范围内。
# 真正防「写成文章」的是 schema 的段落数上限，不是这个字数上限。
MIN_BODY_CHARS = 450
MAX_BODY_CHARS = 3200
# 解释+例子+边界的最少字数。原先设 120 太松，放过了一批「骨架卡」：
# 目标、引用、迁移任务一应俱全，但每条 explanation 只有两句话，读完还是不懂。
# 真人反馈「有边界和迁移任务的卡，正文都被挤短了」之后提到 450，
# 让「不能为了结构牺牲解释」变成可强制的阈值，而不是提示词里的一句请求。
MIN_PROSE_CHARS = 450
MIN_BLOCKS = 2                 # 事实性段落总数下限


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
    # 全部证据拼在一起，用来区分「编造」和「引错条目」——这两件事严重性完全不同
    everything = "\n".join(texts.values())
    for kind, block in schema_v2.iter_blocks(draft):
        cited = "\n".join(texts.get(c, "") for c in (block.get("cites") or []))
        if not cited:
            continue  # 无引用由引用覆盖门禁负责，这里不重复报
        for num in schema_v2.numbers_to_check(block.get("text") or ""):
            if num in cited:
                continue
            if num in everything:
                # 数字是真的，只是引错了条目。属于引用质量问题，不是事实错误。
                issues.append((
                    "数字与引用不符",
                    "%s 中的「%s」在别的证据里能找到，但不在它所引的证据里" % (kind, num),
                ))
            else:
                issues.append((
                    "数字无出处",
                    "%s 中的「%s」在所有证据里都找不到" % (kind, num),
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


# 警告级问题：记录进报告，但不阻断发布。
# 「数字与引用不符」是**引用精度**问题——数字本身在证据里真实存在，只是引用
# 条目没对准（实测：会议年份 2026 是真的，模型引了 #2 而该内容在 #1）。
# 方案 M1 的门槛是「数字与证据一致」与「事实段落关联证据」，两条都满足，
# 所以不该因为引错条目就把一张内容正确的卡判死。
WARNING_TAGS = {"数字与引用不符"}


def run_gates(draft, claims=None):
    """跑全部门禁，返回 [(标签, 详情)]。空列表 = 通过。"""
    issues = []
    for gate in GATES:
        issues.extend(gate(draft, claims))
    return issues


def split_severity(issues):
    """把问题分成 (阻断级, 警告级)。阻断级才决定能否发布。"""
    blocking = [(t, d) for t, d in issues if t not in WARNING_TAGS]
    warnings = [(t, d) for t, d in issues if t in WARNING_TAGS]
    return blocking, warnings


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
    blocking, warnings = split_severity(all_issues)
    return {
        # 只有阻断级问题才决定能否发布；警告级照常记录供人工判断
        "passed": not blocking,
        "issues": [{"tag": t, "detail": d} for t, d in all_issues],
        "blocking": [{"tag": t, "detail": d} for t, d in blocking],
        "warnings": [{"tag": t, "detail": d} for t, d in warnings],
        "by_gate": per_gate,
        "issue_distribution": tags,
    }
