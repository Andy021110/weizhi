"""CP3 测试：CardDraft Schema 校验 + 端到端生成（Fake Provider）+ 幂等不重复建版本。"""
import copy

import pytest

import db
import schema_v2
from card_writer import build_inputs, render_evidence, write_card
from conftest import make_claims, make_draft, make_v2_goal
from providers import FakeTextProvider, ProviderError

GOAL = make_v2_goal()
CLAIMS = make_claims()


def good_draft():
    return make_draft()


def responder(task, inputs):
    """假模型：返回一份合规草稿。"""
    return make_draft()


# ---------- Schema 校验 ----------

def test_good_draft_passes():
    assert schema_v2.validate_card_draft(good_draft()) == []


@pytest.mark.parametrize("mutate,expect", [
    (lambda d: d.pop("objective"), "objective"),
    (lambda d: d.pop("transfer_task"), "transfer_task"),
    (lambda d: d.pop("title"), "title"),
    (lambda d: d.update(schema_version="0.9"), "schema_version"),
    (lambda d: d.update(estimated_minutes=40), "estimated_minutes"),
    (lambda d: d.update(key_points=["只有一条"]), "key_points"),
    (lambda d: d["explanation"][0].update(cites=[]), "cites"),
    (lambda d: d["explanation"][0].update(cites=["0"]), "cites"),
    (lambda d: d["explanation"][0].update(text="太短"), "过短"),
    (lambda d: d.update(transfer_task="短"), "transfer_task"),
])
def test_schema_rejects_bad_drafts(mutate, expect):
    draft = good_draft()
    mutate(draft)
    errs = schema_v2.validate_card_draft(draft)
    assert errs, "应当被拦截: %s" % expect
    assert any(expect in e for e in errs), "错误信息应指向 %s，实际: %s" % (expect, errs)


def test_unknown_cites_detected():
    draft = good_draft()
    draft["explanation"][0]["cites"] = [0, 99]
    unknown = schema_v2.unknown_cites(draft, CLAIMS)
    assert ("explanation", 99) in unknown
    assert ("explanation", 0) not in unknown


def test_goal_spec_requires_success_evidence():
    goal = copy.deepcopy(GOAL)
    goal["success_evidence"] = ""
    assert any("success_evidence" in e for e in schema_v2.validate_goal_spec(goal))


# ---------- 端到端 ----------

def test_render_evidence_contains_indices():
    block = render_evidence(CLAIMS)
    assert "[#0]" in block and "[#2]" in block
    assert "87.5%" in block


def test_build_inputs_is_flat_and_hashable():
    inputs = build_inputs(GOAL, CLAIMS, {"title": "T", "url": "http://x"})
    assert isinstance(inputs["evidence_block"], str)
    assert inputs["source_title"] == "T"
    assert inputs["schema_version"] == "1.0"
    assert "值得注意" in inputs["banned"]


def test_write_card_end_to_end(tmp_db):
    p = FakeTextProvider(responder=responder)
    draft, draft_id = write_card(p, GOAL, CLAIMS, source={"title": "T", "url": "u"}, source_id=1)
    assert draft["title"] == "Agent Harness 的四个阶段"
    row = db.get_v2_card_draft(db.recent_v2_model_calls(task="card_writer")[0]["input_hash"])
    assert row["status"] == "draft"
    assert row["goal_key"] == "agent-harness"
    assert row["payload"]["objective"] == draft["objective"]


def test_same_input_does_not_create_duplicate_version(tmp_db):
    """方案 M1 验收：同一输入重复运行不重复收费、不生成重复版本。"""
    p = FakeTextProvider(responder=responder)
    write_card(p, GOAL, CLAIMS)
    write_card(p, GOAL, CLAIMS)
    assert p.call_count == 1, "重复调用了模型"
    assert len(db.list_v2_card_drafts()) == 1, "生成了重复版本"


def test_bad_cite_is_retried_then_fails_without_draft(tmp_db):
    """引用不存在的证据编号：重试后仍失败，且不留草稿。"""
    def bad(task, inputs):
        d = good_draft()
        d["explanation"][0]["cites"] = [42]
        return d
    p = FakeTextProvider(responder=bad, max_retries=1)
    with pytest.raises(ProviderError):
        write_card(p, GOAL, CLAIMS)
    assert len(db.list_v2_card_drafts()) == 0, "失败时不该留下草稿"


def test_too_few_claims_is_rejected(tmp_db):
    p = FakeTextProvider(responder=responder)
    with pytest.raises(ValueError, match="不足以规划学习包"):
        write_card(p, GOAL, CLAIMS[:1])
    assert p.call_count == 0, "输入问题不该调用模型"


def test_invalid_goal_is_rejected(tmp_db):
    p = FakeTextProvider(responder=responder)
    bad_goal = dict(GOAL, success_evidence="")
    with pytest.raises(ValueError, match="GoalSpec 不合法"):
        write_card(p, bad_goal, CLAIMS)
    assert p.call_count == 0
