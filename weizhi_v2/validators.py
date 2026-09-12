"""Deterministic publication gates for learning packs."""
import hashlib
import json
import re

from .content_quality import learning_character_count

NUMBER_RE = re.compile(r"(?<![\w.])\$?\d+(?:\.\d+)?%?")


def _numbers(text):
    return {x.lstrip("$").rstrip("%") for x in NUMBER_RE.findall((text or "").replace(",", ""))}


def evidence_hash(claims):
    payload = "\n".join(
        "%s\t%s\t%s\t%s" % (
            c.get("id", ""), c.get("verification_status", ""),
            c.get("claim_text", ""), c.get("evidence_text", ""),
        )
        for c in sorted(claims, key=lambda item: item.get("id", ""))
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def validate_card(card, claims, allow_user_authored=False):
    errors = []
    objective = (card.get("learning_objective") or "").strip()
    if not objective:
        errors.append("缺少学习目标")
    minutes = card.get("estimated_minutes")
    if not isinstance(minutes, int) or not 5 <= minutes <= 10:
        errors.append("单卡预计时间必须为5-10分钟")

    content = card.get("content") or {}
    if content.get("sections") and learning_character_count(card) < minutes * 60:
        errors.append("结构化正文信息量与预计学习时间不匹配")
    quiz = content.get("quiz") or []
    if not quiz:
        errors.append("至少需要一道随堂题")
    claim_map = {c.get("id"): c for c in claims}
    declared_claim_ids = set(card.get("claim_ids") or [])
    for claim_id in declared_claim_ids:
        claim = claim_map.get(claim_id)
        if not claim or claim.get("verification_status") != "supported":
            errors.append("声明了无效证据：%s" % claim_id)

    for item in content.get("factual_sentences") or []:
        text = (item.get("text") or "").strip()
        refs = item.get("claim_ids") or []
        if text and not refs and not allow_user_authored:
            errors.append("事实句缺少证据引用")
            continue
        for claim_id in refs:
            if claim_id not in declared_claim_ids:
                errors.append("事实句引用未登记到卡片：%s" % claim_id)
            claim = claim_map.get(claim_id)
            if not claim or claim.get("verification_status") != "supported":
                errors.append("引用了无效证据：%s" % claim_id)
                continue
            missing = _numbers(text) - _numbers(claim.get("evidence_text"))
            if missing:
                errors.append("数字未出现在证据中：%s" % ",".join(sorted(missing)))

    for i, question in enumerate(quiz):
        options = question.get("options") or []
        answer = question.get("answer")
        if len(options) < 2:
            errors.append("第%d题选项不足" % (i + 1))
        if not isinstance(answer, int) or not 0 <= answer < len(options):
            errors.append("第%d题答案索引无效" % (i + 1))
        normalized = [str(x).strip().casefold() for x in options]
        if len(normalized) != len(set(normalized)):
            errors.append("第%d题存在重复选项" % (i + 1))
        if question.get("objective_id") != card.get("concept_id"):
            errors.append("第%d题未映射当前学习目标" % (i + 1))
    return errors


def validate_pack(cards, claims, provenance="verified"):
    errors = []
    if provenance == "verified" and not claims:
        errors.append("核验型学习包必须包含证据主张")
    if not 1 <= len(cards) <= 3:
        errors.append("学习包必须包含1-3张卡")
    objectives = [(c.get("learning_objective") or "").strip().casefold() for c in cards]
    if len(objectives) != len(set(objectives)):
        errors.append("学习包内存在重复学习目标")
    positions = [c.get("position") for c in cards]
    if positions != list(range(1, len(cards) + 1)):
        errors.append("卡片顺序必须从1连续递增")
    for i, card in enumerate(cards):
        for message in validate_card(card, claims, allow_user_authored=provenance == "user_authored"):
            errors.append("第%d张卡：%s" % (i + 1, message))
    return errors
