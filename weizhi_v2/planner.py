"""Build reviewable learning-pack candidates from already verified evidence claims."""
import hashlib
import json

from . import storage


VERSION = "verified-claim-planner-v2"
FIGURES = [
    [("/assets/ai-product/42-evidence-panel.svg", "主张—证据核验面板"),
     ("/assets/ai-product/03-golden-set.svg", "从事实到可复测学习目标")],
    [("/assets/ai-product/08-failure-tree.svg", "区分事实、解释与推断的边界"),
     ("/assets/ai-product/04-eval-loop.svg", "用问题和复述检验理解")],
    [("/assets/ai-product/36-minimal-change.svg", "把结论迁移到最小实例"),
     ("/assets/ai-product/38-baseline-compare.svg", "用对照条件检查结论边界")],
]


def _load_inputs(source_ids):
    source_ids = list(dict.fromkeys(source_ids or []))
    if not source_ids:
        return [], []
    conn = storage._conn()
    try:
        marks = ",".join("?" for _ in source_ids)
        sources = [dict(row) for row in conn.execute(
            "SELECT * FROM v2_sources WHERE id IN (%s) ORDER BY created_at" % marks,
            tuple(source_ids),
        ).fetchall()]
        claims = [dict(row) for row in conn.execute(
            "SELECT * FROM v2_evidence_claims WHERE source_id IN (%s) "
            "AND verification_status='supported' ORDER BY source_id,created_at,id" % marks,
            tuple(source_ids),
        ).fetchall()]
    finally:
        conn.close()
    for claim in claims:
        raw = claim.pop("verification_json", "{}")
        try:
            claim["verification"] = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            claim["verification"] = {}
    return sources, claims


def _short(text, limit=54):
    text = " ".join((text or "").split())
    return text if len(text) <= limit else text[:limit].rstrip("，。；：,. ") + "…"


TOPICS = {
    "decoding": ("decoding", "greedy", "sampling", "beam", "temperature", "generationconfig", "generate()", "生成", "采样", "解码"),
    "input": ("prompt", "tokenizer", "padding", "batch", "input", "format", "throughput", "提示", "输入", "批处理"),
    "memory": ("quantization", "bitsandbytes", "4-bit", "4-bits", "memory", "device", "显存", "量化", "内存"),
    "evaluation": ("evaluation", "benchmark", "accuracy", "failure", "latency", "cost", "评测", "基准", "错误", "延迟", "成本"),
    "security": ("privacy", "safeguard", "misuse", "retention", "permission", "安全", "隐私", "滥用", "权限", "保留"),
}


def _topic(item):
    text = (item.get("claim_text") or "").casefold()
    best_name, best_score = "other", 0
    for name, words in TOPICS.items():
        score = sum(1 for word in words if word in text)
        if score > best_score:
            best_name, best_score = name, score
    return best_name


def _split_bucket(items):
    result = []
    offset = 0
    while len(items) - offset >= 2:
        remaining = len(items) - offset
        size = 2 if remaining == 4 else min(3, remaining)
        result.append(items[offset:offset + size])
        offset += size
    return result


def _chunks(items):
    """Group two or three semantically related claims; discard isolated filler."""
    buckets = {}
    for item in items:
        buckets.setdefault(_topic(item), []).append(item)
    groups = []
    for name in TOPICS:
        groups.extend(_split_bucket(buckets.get(name, [])))
    groups.extend(_split_bucket(buckets.get("other", [])))
    if not groups and len(items) >= 2:
        groups = [items[:2]]
    return groups[:3]


def _card(claims, sources, position, fingerprint):
    source_map = {item["id"]: item for item in sources}
    primary = claims[0]
    claim_ids = [item["id"] for item in claims]
    claim_lines = ["证据直接支持的主张：%s" % item["claim_text"] for item in claims]
    evidence_lines = ["原文证据：%s" % item["evidence_text"] for item in claims]
    concept_id = "verified_%s_%d" % (fingerprint[:12], position)
    title = "核验证据学习：%s" % _short(primary["claim_text"], 34)
    figures = FIGURES[(position - 1) % len(FIGURES)]
    content = {
        "title": title,
        "lead": "这张卡从已经通过快照逐字核验的主张出发。学习任务是准确复述信源说了什么、证据怎样支持它，并识别哪些解释仍需要额外材料，而不是把摘要语气当成事实强度。",
        "sections": [
            {"heading": "1. 先固定原始主张", "paragraphs": claim_lines + ["阅读时保留主语、条件、时间和数量限定。省略限定词可能让一句局部结论被误读为普遍规律；复述应先求准确，再追求简洁。"]},
            {"heading": "2. 回到支持它的证据", "paragraphs": evidence_lines + ["逐句比较主张和证据：证据明确出现的内容可以作为事实复述；证据没有表达的因果关系、适用范围和价值判断，都不能自动补进结论。"]},
            {"heading": "3. 分开事实与解释", "paragraphs": ["事实层回答信源明确报告了什么；解释层回答它可能意味着什么；决策层回答在你的任务中是否值得采用。三层可以相连，但每次跨层都需要新的论据或实验。", "如果多个来源对同一问题表述不同，应保留各自条件和发布时间。系统需要显示冲突与未知，不能通过生成一段顺滑文字把差异静默抹平。"]},
            {"heading": "4. 检查适用边界", "paragraphs": ["检查这条结论来自什么对象、样本、版本或场景。即使证据摘录完全匹配，外推到不同用户、模型、数据或时间仍可能失败；快照核验解决真实性，不替代外部有效性判断。", "把最容易误用这条主张的场景写出来，再说明需要补充哪一种来源或对照实验。边界越具体，未来遇到相似问题时越容易正确调用这条知识。"]},
            {"heading": "5. 做一次迁移复述", "paragraphs": ["先关闭原文，用一句话复述主张，再分别写出一个证据支持的结论和一个证据尚未支持的推断。重新打开信源，检查是否遗漏限制条件或加入了原文没有的数字。", "最后把结论放进你的实际任务：指出它会改变哪个判断、需要观察什么指标、什么结果会推翻当前理解。这样卡片保存的是可使用且可修正的知识。"]},
        ],
        "illustrations": [
            {"src": src, "after_section": index * 2, "alt": label, "caption": label,
             "takeaway": "用结构化步骤把原始事实、解释边界和迁移判断分开。"}
            for index, (src, label) in enumerate(figures)
        ],
        "key_points": ["复述必须保留原始限定条件。", "证据匹配不自动证明外推成立。", "迁移前写清可能推翻结论的观察。"],
        "factual_sentences": [{"text": item["claim_text"], "claim_ids": [item["id"]]} for item in claims],
        "evidence_quotes": [{
            "claim": item["claim_text"], "quote": item["evidence_text"],
            "source": source_map[item["source_id"]]["title"],
            "source_url": source_map[item["source_id"]]["canonical_uri"],
        } for item in claims],
        "quiz": [{
            "question": "以下哪一种做法符合这张卡的证据使用规则？",
            "options": ["只复述证据明确支持的内容，并单独标记推断", "把合理猜测写成来源已经证实的事实", "忽略条件并把局部结果推广到所有场景"],
            "answer": 0, "objective_id": concept_id,
            "explanation": "核验证据只支持它明确表达的内容；解释、外推和产品决策需要额外依据。",
        }],
    }
    return {
        "concept_id": concept_id,
        "learning_objective": "准确复述并判断证据边界：%s" % _short(primary["claim_text"]),
        "minimum_recall": "能说出原始主张、直接证据和一个尚未被证实的外推",
        "transfer_task": "把结论应用到一个真实决策，并写出可推翻它的观察",
        "estimated_minutes": 8,
        "claim_ids": claim_ids,
        "content": content,
    }


def plan_pack(data):
    goal = storage.get_goal(data.get("goal_id"))
    if not goal or goal["status"] in ("archived", "completed"):
        return {"error": "请选择有效的学习目标"}
    source_ids = data.get("source_ids") or []
    sources, claims = _load_inputs(source_ids)
    if len(sources) != len(set(source_ids)) or any(item["status"] != "ready" for item in sources):
        return {"error": "只能使用已经完成快照核验的来源"}
    if not claims:
        return {"error": "所选来源没有通过核验的证据主张"}
    if len(claims) < 2:
        return {"error": "核验证据不足以形成高价值学习卡；至少需要两条相关主张"}
    milestone_id = data.get("milestone_id")
    valid_milestones = {item["id"] for item in goal.get("milestones") or []}
    if milestone_id and milestone_id not in valid_milestones:
        return {"error": "里程碑不属于当前学习目标"}
    if not milestone_id:
        milestone = next((item for item in goal.get("milestones") or [] if item["status"] == "active"), None)
        milestone_id = milestone["id"] if milestone else None
    groups = _chunks(claims[:12])
    selected = [claim for group in groups for claim in group]
    if len(selected) < 2:
        return {"error": "核验证据彼此过于分散，尚不能组成一个学习命题"}
    fingerprint = hashlib.sha256((goal["id"] + "|" + "|".join(item["id"] for item in selected)).encode()).hexdigest()
    generation_version = "%s:%s" % (VERSION, fingerprint[:16])
    existing = next((item for item in storage.list_packs() if item["generation_version"] == generation_version), None)
    if existing:
        return {"success": True, "pack": existing, "reused": True}
    cards = [_card(group, sources, index, fingerprint) for index, group in enumerate(groups, 1)]
    title = data.get("title") or "证据学习包｜%s" % _short(sources[0]["title"], 30)
    result = storage.create_pack({
        "goal_id": goal["id"], "milestone_id": milestone_id, "title": title,
        "pack_objective": "从核验过的原始证据建立可复述、可追溯、可迁移的知识",
        "sources": [{"source_id": item["id"], "role": "primary" if index == 0 else "corroborating"} for index, item in enumerate(sources)],
        "claims": selected, "cards": [dict(item, position=index) for index, item in enumerate(cards, 1)],
        "provenance": "verified", "generation_version": generation_version,
    })
    if result.get("success"):
        result["claim_count"] = len(selected)
    return result
