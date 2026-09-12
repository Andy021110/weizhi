# -*- coding: utf-8 -*-
"""修订候选：人工审核之后才允许改动已发布的卡片。

范围决策原文：

> 应该合并成一个候选质量状态机：
> 生成 → 程序门禁 → 模型评测 → 人工审核 → 发布 → 发现问题 → 创建新版本
>
> 不应该让「自愈 Agent」静默修改已经发布的卡片。

**这条改的是行为，不只是流程**：原来的自动修复会直接重生成并写回卡片，
而写回的实现是「存新卡 + 删旧卡」——`delete_card` 会连带删掉 `progress`，
所以一次自动修复会把用户在这张卡上的完成记录一并抹掉。
已学过的卡尤其不能这样变。

现在：发现问题只**提出候选**（`propose`），卡片保持原样；
采纳时**原地更新**（`db.apply_card_revision`），学习进度与复习状态都留着。
"""
from weizhi.core import db


def diff_summary(prev, payload):
    """列出这次修订到底改了什么（字段级 + 体量变化）。

    审核界面不能只给一个"重新生成好了"——那等于让人凭信任按键。
    给到「哪些字段变了、变长还是变短」才谈得上审核。
    """
    fields = [
        ("title", "标题"), ("summary", "导语"), ("body", "正文"),
        ("think_question", "思考题"), ("think_answer", "思考题答案"),
        ("difficulty", "难度"),
    ]
    out = []
    for key, label in fields:
        before = (prev or {}).get(key) or ""
        after = (payload or {}).get(key) or ""
        if str(before) == str(after):
            continue
        out.append({"field": key, "label": label,
                    "before": len(str(before)), "after": len(str(after)),
                    "kind": "len"})
    for key, label in (("core_points", "核心观点"), ("quiz", "随堂题"),
                       ("review_quiz", "复习题"), ("examples", "例句"),
                       ("steps", "步骤")):
        before = (prev or {}).get(key) or []
        after = (payload or {}).get(key) or []
        if len(before) == len(after):
            continue
        out.append({"field": key, "label": label,
                    "before": len(before), "after": len(after), "kind": "count"})
    return out


def propose(api_key, source_url, reason="", origin="auto"):
    """生成一份修订候选，**不改动已发布的卡片**。返回结果 dict。

    `origin`：`auto`（巡检自动提出）/ `manual`（用户点重新生成）。
    `studied` 会记在候选上——审核界面要能一眼看出"这张卡我已经学过了"。
    """
    from weizhi.serve import reader

    old = db.get_card(source_url)
    if not old:
        return {"error": "卡片不存在"}
    gen = reader.regen_card(api_key, source_url, apply=False)
    if gen.get("error"):
        return {"error": gen["error"]}
    payload = gen.get("card") or {}

    rev_id = db.save_card_revision(
        source_url, payload, reason=reason or "巡检发现问题",
        origin=origin, studied=db.was_studied(source_url), prev_payload=old)
    return {
        "revision_id": rev_id,
        "source_url": source_url,
        "title": payload.get("title") or old.get("title"),
        "reason": reason,
        "studied": db.was_studied(source_url),
        "changes": diff_summary(old, payload),
    }


def apply(rev_id, note=None):
    """采纳一份候选：原地写回卡片，保留学习进度。返回 (结果, 错误)。"""
    rev = db.get_card_revision(rev_id)
    if not rev:
        return None, "候选不存在"
    if rev["status"] != "pending":
        return None, "候选已经处理过了（%s）" % rev["status"]
    prev = db.get_card(rev["source_url"])
    if not prev:
        return None, "原卡片已不存在，无法采纳（可改为丢弃）"
    if not db.apply_card_revision(rev["source_url"], rev["payload"]):
        return None, "写回失败"
    db.decide_card_revision(rev_id, "applied", note=note, prev_payload=prev)
    return {"revision_id": rev_id, "source_url": rev["source_url"],
            "studied": rev["studied"], "applied": True}, None


def reject(rev_id, note=None):
    """丢弃一份候选。卡片不受影响。"""
    rev = db.get_card_revision(rev_id)
    if not rev:
        return None, "候选不存在"
    if rev["status"] != "pending":
        return None, "候选已经处理过了（%s）" % rev["status"]
    db.decide_card_revision(rev_id, "rejected", note=note)
    return {"revision_id": rev_id, "rejected": True}, None


def pending(limit=50):
    """待审候选（含变更摘要），给审核界面用。"""
    out = []
    for rev in db.list_card_revisions(status="pending", limit=limit):
        current = db.get_card(rev["source_url"]) or {}
        out.append({
            "id": rev["id"],
            "source_url": rev["source_url"],
            "title": current.get("title") or (rev["payload"] or {}).get("title") or "",
            "reason": rev["reason"],
            "origin": rev["origin"],
            "studied": rev["studied"],
            "created_at": rev["created_at"],
            "changes": diff_summary(current, rev["payload"]),
        })
    return out


def count_pending():
    return db.count_pending_revisions()


def revert(rev_id, note="回滚修订"):
    """把一份已采纳的候选回滚回采纳前的内容（原地，保留进度）。"""
    rev = db.get_card_revision(rev_id)
    if not rev:
        return None, "候选不存在"
    if rev["status"] != "applied":
        return None, "只有已采纳的候选才能回滚"
    prev = rev.get("prev_payload")
    if not prev:
        return None, "这份候选没有留下采纳前的内容，无法回滚"
    if not db.apply_card_revision(rev["source_url"], prev):
        return None, "回滚写回失败"
    db.decide_card_revision(rev_id, "rejected", note=note)
    return {"revision_id": rev_id, "reverted": True}, None
