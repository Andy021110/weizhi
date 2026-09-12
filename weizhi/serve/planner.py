# -*- coding: utf-8 -*-
"""微知 v2 · 最小学习包规划（CP10，方案 M2「动态学习路径」）。

方案 M2 对规划器有三条硬要求：

1. 「每次只生成下一个最小学习包，**不一次性生成整条路径全部正文**」——
   所以 `plan_next` 只决定「接下来做哪个里程碑、用哪份材料」，
   真正写卡由 `materialize` 按计划逐张做，做完再规划下一包。
2. 「每张卡能映射到一个**里程碑**和一个**能力缺口**」——
   计划里带 concept（里程碑 id）与 capability_gap，随卡片一起落库。
3. 「系统可以解释**为什么**下一步推荐这张卡」——
   所以 `plan_next` 必须返回人话理由，而不是一个分数。

规划逻辑全部是确定性的：目标是能力缺口大小 → 依赖顺序 → 证据相关度。
模型不参与决定「学什么」，它只在拿到计划之后负责「怎么讲清楚」。
这是方案 4.1 的边界——路线判断会影响用户长期方向，不能每天给出不一样的答案。

用法::

    from weizhi.serve import planner
    plan = planner.plan_next("g-1", sources)
    if plan["status"] == "ok":
        results = planner.materialize(provider, "g-1", plan)
"""
from weizhi.produce import evidence
from weizhi.serve import mastery
from datetime import datetime

# 一份材料至少要能支撑这么多条相关证据，才值得为它做一张卡（方案 3.1）
MIN_RELEVANT_CLAIMS = evidence.MIN_CLAIMS_FOR_PACK
DEFAULT_PACK_SIZE = 3


def _terms(text):
    """从一段描述里抽关键词，复用 evidence 的滑动窗口取词（不引入分词依赖）。"""
    return evidence._goal_terms({"capability": text, "scene": "", "success_evidence": ""})


def _claim_relevance(claim, terms):
    text = claim.get("text") or ""
    if not terms or not text:
        return 0
    hit = sum(1 for t in terms if t in text)
    # 带数字/定义的证据信息密度更高，同等相关度时优先
    density = {"number": 1, "definition": 1}.get(claim.get("kind"), 0)
    return hit * 2 + density


def relevance(claim, milestone, spec):
    """单条证据对当前里程碑的相关度。

    方案 M5 要求「先计算与当前目标的能力相关性，再进入 Pack Planner」，
    这个函数就是那一步的确定性实现——它不判断内容好不好，
    只判断「跟眼下这个能力缺口有没有关系」。

    **已知脆弱性（集成测试实测暴露）**：匹配是关键词级的（字符 n-gram 重合），
    而里程碑名是模型生成的自由文本。同一个能力缺口，模型写成
    「说出循环的四个阶段」能命中材料，写成「理解循环的本质」就可能命中 0 条，
    于是整份材料被判为不相关。缓解手段有三条，按代价从小到大：
    1. 让 GoalSpec Builder 的里程碑措辞更锚定具体动作（提示词已在做）；
    2. 相关性也用上 `capability` 与 `scene`（已实现，但权重仍偏低）；
    3. M6 实验后如仍不稳，换成 embedding 相似度——那需要引入依赖，
       不能为了一个匹配问题就把「标准库 + 3 个三方包」的约束丢掉。
    """
    terms = _terms(" ".join([
        milestone.get("name") or "",
        milestone.get("evidence") or "",
        spec.get("capability") or "",
        spec.get("scene") or "",
    ]))
    return _claim_relevance(claim, terms)


def _score_source(source, milestone, spec):
    claims = source.get("claims") or []
    scored = [(relevance(c, milestone, spec), c) for c in claims]
    scored = [(r, c) for r, c in scored if r > 0]
    if not scored:
        return None
    scored.sort(key=lambda x: -x[0])
    picked = scored[:8]                       # 一张卡最多喂 8 条证据，跟 select_claims 上限一致
    avg = sum(r for r, _ in picked) / float(len(picked))
    return {
        "source_id": source.get("source_id"),
        "title": source.get("title"),
        "relevance": round(avg, 2),
        "claim_count": len(picked),
        "claim_idxs": [c.get("claim_idx") for _r, c in picked],
    }


def next_milestone(spec, now=None):
    """按依赖顺序找第一个未达标的里程碑。

    为什么按顺序而不是直接挑最弱的：规格里里程碑本身是有依赖的
    （不会画循环图就谈不上定位故障）。跳过前置会生成用户接不住的卡。
    """
    from weizhi.core import db
    states = db.get_v2_mastery(spec.get("key")) or {}
    for m in spec.get("milestones") or []:
        state = states.get(m["id"])
        score = state.get("score") if state else None
        if score is None or score < mastery.MASTERED:
            return m, state
    return None, None


def plan_next(goal_key, sources, spec=None, limit=DEFAULT_PACK_SIZE, now=None):
    """规划下一个最小学习包。

    `sources` 形如 `[{"source_id":1,"title":"…","claims":[...]}]`。
    返回 `{"status": "ok"|"goal_complete"|"no_material", "reason": "...", ...}`。
    """
    from weizhi.core import db as _db
    spec = spec or (_db.latest_v2_goal(goal_key, status="active") or {}).get("spec")
    if not spec:
        return {"status": "no_goal", "reason": "目标还没确认（确认后才会规划学习包）"}

    milestone, state = next_milestone(spec, now)
    if milestone is None:
        return {"status": "goal_complete",
                "reason": "所有里程碑都已达标（掌握度 ≥ %.2f）" % mastery.MASTERED}

    gap = mastery.explain(state, now)
    candidates, skipped = [], []
    for src in sources or []:
        scored = _score_source(src, milestone, spec)
        if not scored:
            skipped.append({"title": src.get("title"), "why": "没有与该里程碑相关的证据"})
            continue
        if scored["claim_count"] < MIN_RELEVANT_CLAIMS:
            skipped.append({"title": src.get("title"),
                            "why": "相关证据只有 %d 条，不足以成卡" % scored["claim_count"]})
            continue
        candidates.append(scored)

    if not candidates:
        return {"status": "no_material", "milestone": milestone, "gap": gap,
                "skipped": skipped,
                "reason": "里程碑「%s」当前没有足够相关的材料，先补信源再规划" % milestone["name"]}

    candidates.sort(key=lambda c: -c["relevance"])
    picked = candidates[:limit]

    order = [m["id"] for m in spec.get("milestones") or []].index(milestone["id"]) + 1
    reason = (
        "按依赖顺序，先补第 %d 个里程碑「%s」：%s。"
        "从 %d 份材料里选了相关度最高的 %d 份（最高 %.2f），"
        "排在后面的 %d 份本轮不生成，等这一包复习完再评估。"
        % (order, milestone["name"], gap, len(candidates), len(picked),
           picked[0]["relevance"], max(0, len(candidates) - len(picked)))
    )
    return {
        "status": "ok",
        "goal_key": goal_key,
        "milestone": milestone,
        "milestone_index": order,
        "gap": gap,
        "mastery": state,
        "cards": picked,
        "skipped": skipped,
        "reason": reason,
    }


def materialize(provider, plan, spec=None, max_cards=None):
    """按计划逐张写卡。这是「一次只做最小学习包」的落点。

    单张失败不中断整包——但失败会记进结果里，不静默吞掉。
    """
    from weizhi.produce import card_writer
    from weizhi.core import db as _db

    if plan.get("status") != "ok":
        return {"written": [], "failed": [], "reason": plan.get("reason")}

    goal_key = plan["goal_key"]
    spec = spec or (_db.latest_v2_goal(goal_key, status="active") or {}).get("spec")
    goal = _goal_from_spec(spec, goal_key)
    concept = plan["milestone"]["id"]
    gap = plan["gap"]

    # 先把包落库再逐张生成：范围为「一次 20-30 分钟的完整学习」，
    # 它是八个核心实体之一，不能只活在内存里（范围决策 D3）。
    pack_id = _db.save_v2_learning_pack(
        goal_key, concept, plan.get("reason") or "", status="planned")

    written, failed = [], []
    for card in (plan["cards"] if max_cards is None else plan["cards"][:max_cards]):
        src = _db.get_v2_source_by_id(card["source_id"])
        claims = [c for c in _db.load_v2_claims(card["source_id"])
                  if c.get("claim_idx") in set(card["claim_idxs"])]
        if len(claims) < MIN_RELEVANT_CLAIMS:
            failed.append({"title": card["title"], "error": "相关证据在库中不足"})
            continue
        try:
            draft, draft_id, report = card_writer.write_card_gated(
                provider, goal, claims,
                source={"title": src.get("title"), "url": src.get("url")} if src else None,
                source_id=card["source_id"],
            )
        except Exception as exc:  # noqa: BLE001 - 单张失败不该拖垮整包
            failed.append({"title": card["title"], "error": "%s: %s" % (type(exc).__name__, exc)})
            continue
        # 补上「这张卡映射到哪个里程碑、补哪个能力缺口」
        _db.save_v2_card_draft(
            input_hash=_db.get_v2_card_draft_by_id(draft_id)["input_hash"],
            schema_version=draft.get("schema_version"),
            payload=draft, source_id=card["source_id"], goal_key=goal_key,
            objective=draft.get("objective"),
            status="draft" if report["passed"] else "rejected",
            gate_report=report, concept=concept, capability_gap=gap,
        )
        written.append({"draft_id": draft_id, "title": draft.get("title"),
                        "source_id": card["source_id"],
                        "passed": report["passed"], "concept": concept})

    # 全部失败时包状态回退为 failed，不留一个「看起来有内容」的空包
    status = "ready" if written else "failed"
    _db.set_v2_learning_pack_status(
        pack_id, status, card_ids=[w["draft_id"] for w in written])
    return {"pack_id": pack_id, "status": status,
            "written": written, "failed": failed, "concept": concept,
            "gap": gap, "reason": plan.get("reason")}


def _goal_from_spec(spec, goal_key):
    """把 GoalSpec 转成 card_writer 需要的 goal 结构。

    规格里已经有可观察的成功证据与里程碑，卡片生成直接吃它，
    不再单独造一份——两份目标描述迟早会漂移。
    """
    milestones = spec.get("milestones") or []
    return {
        "key": goal_key,
        "capability": spec.get("capability") or "",
        "level": spec.get("level") or "",
        "scene": spec.get("scene") or "",
        "success_evidence": spec.get("success_evidence") or "",
        "prereq": spec.get("prereq") or [],
        "milestones": [m["name"] for m in milestones],
        "daily_minutes": spec.get("daily_minutes") or 60,
    }
