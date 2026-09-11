# -*- coding: utf-8 -*-
"""微知 v2 → v1 桥接层：让 v2 生成的卡片出现在现有前端里。

**为什么需要这一层**：v2 的后端是完整的，但 v1 的 `reader.html` 从
`cards` 表读数据。v2 的产出（CardDraft / 图形方案 / 题目集）不写那张表，
所以部署上去用户在手机上什么也看不到。桥接把两者接起来，
**前端一行都不用改**——这是让 v2 最快可见的路径。

**设计原则**：
1. 忠实映射，不为过门禁而凑内容。v1 的 `rule_check` 要求 quiz≥3、
   review_quiz≥3，但这两条已被范围决策废除（题量由目标定、题库分层）。
   所以这里如实映射，冲突交给 `check_v1_compat` 报出来，而不是编题凑数。
2. 确定性字段由程序算（难度、时效、权威度），不问模型。
3. 标注来源：`_bridge` 字段记下这张卡的 v2 出处（草稿 id / 学习包 id /
   题目分层），出问题时能追回去。

用法::

    import bridge_v1
    card = bridge_v1.to_v1_card(draft, items, material, supplement=sup)
    conflicts = bridge_v1.check_v1_compat(card)     # 旧门禁与范围决策的冲突
    bridge_v1.save(card)                            # 落 v1 的 cards 表
"""
import hashlib

from datetime import datetime

import db
import news
import providers
import schema_v2

TASK = "card_supplement"
TEMPLATE = "t2_reading"

# 权威度映射：v2 按 tier 分来源，v1 前端认这五档文案
CREDIBILITY = {
    "official": "官方", "paper": "官方", "primary": "官方",
    "media": "权威媒体", "analyst": "专业机构",
    "blog": "专业博客", "social": "自媒体",
}

# 难度按正文字数推。不用模型——这是确定性的量纲换算，不是判断
DIFFICULTY_BANDS = ((900, "入门"), (1500, "中级"))


def _difficulty(body):
    n = len(body or "")
    for limit, name in DIFFICULTY_BANDS:
        if n < limit:
            return name
    return "进阶"


def _facts_to_quiz(item):
    """v2 题目 → v1 quiz 形状。

    v2 多出的 `layer` 与 `error_reason` 一并带过去：前端暂时不显示，
    但它们是新题库分层的依据，丢了就退不回来。
    """
    return {
        "question": item.get("question"),
        "options": list(item.get("options") or []),
        "answer": item.get("answer"),
        "explanation": item.get("explanation"),
        "layer": item.get("layer"),
        "error_reason": item.get("error_reason"),
        "objective": item.get("objective"),
    }


def split_items(items):
    """按时间层把题库拆成 v1 的两套：学完立即 / 后续回忆。

    范围决策 D2 把「随堂题 + 复习题」合并为分层题库，这里是把合并后的
    题库还原成旧前端的两个字段——**方向与决策相反，属于兼容垫片**。
    等前端改成直接读分层题库，这个函数就可以删掉。
    """
    immediate, later = [], []
    for it in items or []:
        (immediate if it.get("layer") == "immediate" else later).append(_facts_to_quiz(it))
    return immediate, later


def to_v1_card(draft, items, material=None, supplement=None, figures=None,
               pack_id=None, draft_id=None, date=None):
    """把 v2 产出映射成 v1 卡片字典。

    `supplement` 缺省时 think_answer / open_question 为空——调用方应先跑
    `supplement()` 补全。缺了这两个字段 v1 门禁会拦，所以不静默放过。
    """
    material = material or {}
    supplement = supplement or {}

    # 正文 = 解释 + 例子 + 边界。边界单独成段并加小标题，
    # 因为读者需要一眼看出「这段在讲适用范围」，混在解释里会被略过。
    parts = [b.get("text", "") for b in (draft.get("explanation") or [])]
    parts += [b.get("text", "") for b in (draft.get("examples") or [])]
    bounds = [b.get("text", "") for b in (draft.get("boundaries") or [])]
    body = "\n\n".join(p for p in parts if p)
    if bounds:
        body += "\n\n【适用边界】\n" + "\n".join(bounds)

    quiz, review_quiz = split_items(items)

    open_q = supplement.get("open_question") or None
    card = {
        "source_url": material.get("url"),
        "title": draft.get("title"),
        "summary": draft.get("lead"),
        "body": body,
        "core_points": list(draft.get("key_points") or []),
        "think_question": draft.get("transfer_task") or "",
        "think_answer": supplement.get("think_answer") or "",
        "open_question": open_q,
        "quiz": quiz,
        "review_quiz": review_quiz,
        "difficulty": _difficulty(body),
        "source": material.get("site") or material.get("title") or "未标注来源",
        "category": "AI",
        "template": TEMPLATE,
        "timeliness": material.get("kind") or "evolving",
        "credibility": CREDIBILITY.get((material.get("source_tier") or "blog").lower(),
                                       "专业博客"),
        "published": material.get("published_at"),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "_meta": {
            "template": TEMPLATE,
            "category": "AI",
            "generated_at": datetime.now().isoformat(timespec="seconds"),
            # 标注来源：出问题时能追回是 v2 哪一次生成
            "origin": "v2-bridge",
        },
        "_bridge": {
            "origin": "v2",
            "draft_id": draft_id,
            "pack_id": pack_id,
            "objective": draft.get("objective"),
            "milestone": None,
            "layers": sorted({it.get("layer") for it in (items or []) if it.get("layer")}),
            "figures": len(figures or []),
            "bridged_at": datetime.now().isoformat(timespec="seconds"),
        },
    }
    if date:
        card["_date"] = date
    return card


def dedupe_key(card):
    """桥接卡的去重键。v1 的 cards 表按 source_url 去重，但同一篇文章
    可能合法地出多张卡（范围决策已废除「一文一卡」），所以这里带上前缀。"""
    raw = "%s|%s" % (card.get("source_url") or "", card.get("title") or "")
    return "v2:" + hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def check_v1_compat(card):
    """列出这张卡在 v1 门禁下会撞上的规则。

    **实现上直接调用真实门禁，不复制阈值。** 之前这里复制了一份阈值，
    结果是两处会各自漂移——而漂移的后果是「桥上看着能发、线上却被拦」，
    这种错最难查。现在只有一处真相。

    历史上这个函数还负责把「旧门禁要求固定题量」的冲突显式报出来。
    固定题量已按范围决策放宽（见 daily_check），所以那些冲突不再出现；
    保留这个入口是为了让调用方能不触发发布地先看一眼。
    """
    import daily_check
    return daily_check.rule_check(card)


def validate_supplement(data):
    errs = []
    if not isinstance(data, dict):
        return ["补全输出必须是 JSON 对象"]
    if len((data.get("think_answer") or "").strip()) < 150:
        # v1 门禁硬要求 150 字，这里提前卡住比入库后被拦便宜
        errs.append("think_answer 过短（%d 字 < 150）" % len((data.get("think_answer") or "")))
    oq = data.get("open_question")
    if not isinstance(oq, dict):
        errs.append("open_question 应为对象")
    else:
        if len((oq.get("question") or "").strip()) < 10:
            errs.append("open_question.question 过短")
        if len((oq.get("reference_answer") or "").strip()) < 80:
            errs.append("open_question.reference_answer 过短")
        gp = oq.get("grading_points")
        if not isinstance(gp, list) or len(gp) < 2:
            errs.append("open_question.grading_points 至少 2 条")
    return errs


def supplement(provider, draft):
    """补 think_answer 与 open_question。一次调用，不碰正文。"""
    inputs = {
        "objective": draft.get("objective") or "",
        "body_block": __import__("card_writer").render_body(draft),
        "transfer_task": draft.get("transfer_task") or "（未给迁移任务）",
        "key_points": "；".join(draft.get("key_points") or []) or "（无）",
    }
    return provider.generate_json(TASK, validate_supplement, inputs)


def from_draft(draft_id, items=None, material=None, provider=None, pack_id=None,
               figures=None, date=None):
    """从库里的一张 v2 草稿桥接成 v1 卡片（含补全调用）。"""
    row = db.get_v2_card_draft_by_id(draft_id)
    if not row:
        raise ValueError("草稿不存在: %s" % draft_id)
    draft = row["payload"] or {}
    items = items if items is not None else (row.get("assessment") or [])
    figures = figures if figures is not None else (row.get("figures") or [])

    if material is None and row.get("source_id"):
        src = db.get_v2_source_by_id(row["source_id"])
        material = {"title": (src or {}).get("title"), "url": (src or {}).get("url"),
                    "site": (src or {}).get("title")}

    sup = supplement(provider, draft) if provider else {}
    card = to_v1_card(draft, items, material, sup, figures,
                      pack_id=pack_id, draft_id=draft_id, date=date)
    if provider:
        card["_bridge"]["milestone"] = row.get("concept")
        card["_bridge"]["capability_gap"] = row.get("capability_gap")
    return card


def save(card, date=None):
    """落 v1 的 cards 表。返回是否写入（同一来源+标题已存在则忽略）。"""
    if card.get("source_url"):
        card = dict(card)
        card["source_url"] = "%s#%s" % (card["source_url"], dedupe_key(card))
    return db.save_card(card, date=date)


def publish_gate(card):
    """桥接卡能不能进推送。

    范围决策明确：候选内容必须过质量门禁，不能让模型生成的东西直接发布。
    这里复用的是 v1 的 rule_check——它的阈值偏旧（见 check_v1_compat），
    但「先拦后放」的方向是对的，不能因为阈值待调整就跳过门禁。
    """
    import daily_check
    issues = daily_check.rule_check(card)
    return (not issues), issues
