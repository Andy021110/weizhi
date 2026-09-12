import json

from weizhi_v2 import storage
from weizhi_v2.content_quality import learning_character_count
from weizhi_v2.planner import plan_pack


def _fixture(tmp_db, claim_count=4):
    goal = storage.create_goal({
        "outcome": "学会依据原始证据判断 AI 产品技术变化",
        "current_level": "有计算机基础，但缺少持续评测经验",
        "use_context": "微知产品设计与技术选型",
        "daily_minutes": 90,
        "milestones": ["建立证据判断方法", "完成一次迁移实验"],
    })["goal"]
    source = storage.create_source({
        "source_type": "article", "canonical_uri": "https://example.com/report",
        "title": "AI system evaluation report", "snapshot_path": "/tmp/report.txt",
        "snapshot_sha256": "planner-fixture", "status": "ready",
    })["source"]
    conn = storage._conn()
    try:
        for index in range(claim_count):
            text = "The report records supported finding %d under the stated test condition." % (index + 1)
            conn.execute(
                "INSERT INTO v2_evidence_claims VALUES (?,?,?,?,?,?,?,?,?)",
                ("claim-%d" % index, source["id"], text, text, "section %d" % index,
                 "fact", "supported", json.dumps({"method": "exact excerpt"}), storage._now()),
            )
        conn.commit()
    finally:
        conn.close()
    return goal, source


def test_planner_turns_verified_claims_into_reviewable_candidate(tmp_db):
    goal, source = _fixture(tmp_db)
    result = plan_pack({"goal_id": goal["id"], "source_ids": [source["id"]]})
    assert result["success"] is True
    assert result["claim_count"] == 4
    pack = result["pack"]
    assert pack["status"] == "candidate"
    assert pack["milestone_id"] == goal["milestones"][0]["id"]
    assert len(pack["cards"]) == 2
    for card in pack["cards"]:
        content = card["version"]["content"]
        assert len(content["sections"]) == 5
        assert len(content["illustrations"]) == 2
        assert learning_character_count({"content": content}) >= card["estimated_minutes"] * 60
        assert content["factual_sentences"]
        assert all(item["claim_ids"] for item in content["factual_sentences"])


def test_planner_is_idempotent_for_same_goal_and_claims(tmp_db):
    goal, source = _fixture(tmp_db, claim_count=2)
    first = plan_pack({"goal_id": goal["id"], "source_ids": [source["id"]]})
    second = plan_pack({"goal_id": goal["id"], "source_ids": [source["id"]]})
    assert second["reused"] is True
    assert first["pack"]["id"] == second["pack"]["id"]
    assert len(storage.list_packs()) == 1


def test_planner_rejects_single_release_fact_as_too_thin(tmp_db):
    goal, source = _fixture(tmp_db, claim_count=1)
    result = plan_pack({"goal_id": goal["id"], "source_ids": [source["id"]]})
    assert result["error"] == "核验证据不足以形成高价值学习卡；至少需要两条相关主张"
    assert storage.list_packs() == []


def test_planner_groups_related_claims_and_drops_isolated_topic(tmp_db):
    goal, source = _fixture(tmp_db, claim_count=0)
    claims = [
        "The default decoding strategy uses greedy search unless a generation configuration changes it.",
        "Sampling can make open-ended generation more diverse than greedy decoding.",
        "Beam search is commonly selected for input-grounded generation tasks.",
        "Batch inputs improve throughput with a small cost to latency and memory.",
        "Incorrect prompt format can produce a suboptimal answer for a chat model.",
        "Quantization can reduce model memory requirements.",
    ]
    conn = storage._conn()
    try:
        for index, text in enumerate(claims):
            conn.execute(
                "INSERT INTO v2_evidence_claims VALUES (?,?,?,?,?,?,?,?,?)",
                ("topic-%d" % index, source["id"], text, text, "line", "fact", "supported", "{}", storage._now()),
            )
        conn.commit()
    finally:
        conn.close()
    pack = plan_pack({"goal_id": goal["id"], "source_ids": [source["id"]]})["pack"]
    grouped = [[item["text"] for item in c["version"]["content"]["factual_sentences"]] for c in pack["cards"]]
    assert len(grouped) == 2
    assert len(grouped[0]) == 3 and all("generat" in item.casefold() or "search" in item.casefold() for item in grouped[0])
    assert len(grouped[1]) == 2 and any("prompt" in item.casefold() for item in grouped[1])
    assert all("Quantization" not in item for group in grouped for item in group)


def test_planner_rejects_pending_source_or_unverified_claims(tmp_db):
    goal = storage.create_goal({
        "outcome": "学会验证来源", "current_level": "初学", "use_context": "学习",
        "daily_minutes": 60, "milestones": ["完成一次验证"],
    })["goal"]
    source = storage.create_source({
        "source_type": "article", "canonical_uri": "https://example.com/pending",
        "title": "Pending", "status": "pending",
    })["source"]
    result = plan_pack({"goal_id": goal["id"], "source_ids": [source["id"]]})
    assert result["error"] == "只能使用已经完成快照核验的来源"


def test_candidate_can_be_rejected_only_once(tmp_db):
    goal, source = _fixture(tmp_db, claim_count=2)
    candidate = plan_pack({"goal_id": goal["id"], "source_ids": [source["id"]]})["pack"]
    rejected = storage.reject_pack(candidate["id"])
    assert rejected["success"] is True
    assert rejected["pack"]["status"] == "rejected"
    assert rejected["pack"]["cards"][0]["version"]["status"] == "rejected"
    assert storage.reject_pack(candidate["id"])["error"] == "只有候选学习包可以拒绝"
    assert storage.publish_pack(candidate["id"])["error"] == "只有候选学习包可以发布"
