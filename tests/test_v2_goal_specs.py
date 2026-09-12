"""Quality contract for personalized GoalSpec candidates."""
from weizhi_v2 import storage
from weizhi_v2.goal_specs import PROFILE, candidate_goal_specs, evaluate_goal_spec


def test_candidate_paths_cover_distinct_llm_capabilities():
    specs = candidate_goal_specs()
    assert len(specs) == 7
    assert len({spec["outcome"] for spec in specs}) == 7
    joined = " ".join(spec["outcome"] for spec in specs)
    for topic in ("AI 产品", "大模型", "数学", "RAG", "Agent", "LoRA", "源码"):
        assert topic in joined


def test_candidate_paths_are_personalized_and_pass_quality_gate():
    for spec in candidate_goal_specs():
        assert PROFILE["education"] in spec["current_level"]
        assert spec["status"] == "draft"
        assert evaluate_goal_spec(spec) == {"score": 100, "issues": []}
        assert len(spec["milestones"]) == 5
        assert len(spec["success_evidence"]) == 4


def test_draft_goal_is_persisted_without_becoming_active(tmp_db):
    result = storage.create_goal(candidate_goal_specs()[0])
    assert result["success"]
    assert result["goal"]["status"] == "draft"
    assert len(result["goal"]["milestones"]) == 5
    assert all(item["status"] == "planned" for item in result["goal"]["milestones"])
    assert len(result["goal"]["success_evidence"]) == 4
