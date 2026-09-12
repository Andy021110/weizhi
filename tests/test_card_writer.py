"""CP3 测试：两次调用的 CardWriter（正文 + 结构）+ Schema 校验 + 幂等不重复建版本。"""
import copy

import pytest

from weizhi.core import db
from weizhi.core import schema_v2
from weizhi.produce.card_writer import (
    build_body_inputs,
    build_structure_inputs,
    render_body,
    render_evidence,
    write_card,
)
from conftest import make_body, make_claims, make_structure, make_v2_goal
from weizhi.core.providers import FakeTextProvider, ProviderError

GOAL = make_v2_goal()
CLAIMS = make_claims()


def responder(task, inputs):
    """假模型：按 task 分派。这也顺带验证了两次调用确实用了不同的提示词。"""
    if task == "card_body":
        return make_body()
    if task == "card_structure":
        return make_structure()
    raise AssertionError("意外 task: %s" % task)


def p_fake(**kw):
    return FakeTextProvider(responder=responder, **kw)


# ---------- 正文校验 ----------

def test_good_body_passes():
    assert schema_v2.validate_card_body(make_body()) == []


@pytest.mark.parametrize("mutate,expect", [
    (lambda d: d.pop("objective"), "objective"),
    (lambda d: d.pop("lead"), "lead"),
    (lambda d: d.pop("title"), "title"),
    (lambda d: d.update(schema_version="0.9"), "schema_version"),
    (lambda d: d["explanation"][0].update(cites=[]), "cites"),
    (lambda d: d["explanation"][0].update(cites=["0"]), "cites"),
    (lambda d: d["explanation"][0].update(text="两句话就收尾。"), "过短"),
    (lambda d: d.update(explanation=d["explanation"][:2]), "< 3"),
    (lambda d: d.update(examples=[]), "examples"),
])
def test_body_rejects_bad_output(mutate, expect):
    body = copy.deepcopy(make_body())
    mutate(body)
    errs = schema_v2.validate_card_body(body)
    assert errs, "应当被拦截: %s" % expect
    assert any(expect in e for e in errs), "错误信息应指向 %s，实际: %s" % (expect, errs)


def test_body_rejects_too_many_blocks():
    body = make_body()
    body["explanation"] = [
        {"text": "第 %d 段解释，长度足够通过最短限制的内容说明。" % i, "cites": [0]}
        for i in range(6)
    ]
    assert any("一张卡不是一篇文章" in e for e in schema_v2.validate_card_body(body))


# ---------- 结构校验 ----------

def test_good_structure_passes():
    assert schema_v2.validate_card_structure(make_structure()) == []


@pytest.mark.parametrize("mutate,expect", [
    (lambda d: d.pop("key_points"), "key_points"),
    (lambda d: d.pop("transfer_task"), "transfer_task"),
    (lambda d: d.update(key_points=["只有一条"]), "key_points"),
    (lambda d: d.update(transfer_task="太短"), "transfer_task"),
    (lambda d: d["boundaries"][0].update(cites=[]), "cites"),
    (lambda d: d["boundaries"][0].update(text="太短"), "过短"),
])
def test_structure_rejects_bad_output(mutate, expect):
    struct = copy.deepcopy(make_structure())
    mutate(struct)
    errs = schema_v2.validate_card_structure(struct)
    assert errs, "应当被拦截: %s" % expect
    assert any(expect in e for e in errs), "错误信息应指向 %s，实际: %s" % (expect, errs)


def test_empty_boundaries_is_allowed():
    """讲不出真实边界时宁可不写——这是结构提示词里明确允许的。"""
    assert schema_v2.validate_card_structure(make_structure(boundaries=[])) == []


def test_merge_card_fills_minutes_deterministically():
    draft = schema_v2.merge_card(make_body(), make_structure())
    assert draft["estimated_minutes"] == schema_v2.estimate_minutes(draft)
    assert 4 <= draft["estimated_minutes"] <= 15
    assert schema_v2.validate_card_draft(draft) == []


def test_unknown_cites_detected():
    draft = make_body()
    draft["explanation"][0]["cites"] = [0, 99]
    unknown = schema_v2.unknown_cites(draft, CLAIMS)
    assert ("explanation", 99) in unknown
    assert ("explanation", 0) not in unknown


def test_goal_spec_requires_success_evidence():
    goal = copy.deepcopy(GOAL)
    goal["success_evidence"] = ""
    assert any("success_evidence" in e for e in schema_v2.validate_goal_spec(goal))


# ---------- 输入组装 ----------

def test_render_evidence_contains_indices():
    block = render_evidence(CLAIMS)
    assert "[#0]" in block and "[#2]" in block
    assert "87.5%" in block


def test_body_inputs_are_flat_and_hashable():
    inputs = build_body_inputs(GOAL, CLAIMS, {"title": "T", "url": "http://x"})
    assert isinstance(inputs["evidence_block"], str)
    assert inputs["source_title"] == "T"
    assert inputs["schema_version"] == "1.0"
    assert "值得注意" in inputs["banned"]


def test_structure_inputs_carry_the_finished_body():
    """结构调用的输入必须带已定稿正文——否则它无法「基于正文派生」。"""
    body = make_body()
    inputs = build_structure_inputs(GOAL, CLAIMS, body=body)
    assert body["title"] in inputs["body_block"]
    assert body["explanation"][0]["text"][:20] in inputs["body_block"]
    # 正文一变，结构调用的输入就变，缓存自然失效
    other = make_body(title="换了标题")
    assert build_structure_inputs(GOAL, CLAIMS, body=other)["body_block"] != inputs["body_block"]


def test_render_body_lists_all_blocks():
    text = render_body(make_body())
    assert "解释 1" in text and "解释 3" in text and "例子 1" in text


# ---------- 端到端 ----------

def test_write_card_makes_two_calls(tmp_db):
    """两次调用：一次写正文，一次派生结构。"""
    p = p_fake()
    draft, _draft_id = write_card(p, GOAL, CLAIMS, source={"title": "T", "url": "u"}, source_id=1)
    assert p.call_count == 2
    tasks = {c["task"] for c in db.recent_v2_model_calls(task="card_body")} | \
            {c["task"] for c in db.recent_v2_model_calls(task="card_structure")}
    assert tasks == {"card_body", "card_structure"}
    assert draft["title"] and draft["key_points"] and draft["transfer_task"]


def test_same_input_does_not_create_duplicate_version(tmp_db):
    """方案 M1 验收：同一输入重复运行不重复收费、不生成重复版本。"""
    p = p_fake()
    write_card(p, GOAL, CLAIMS)
    write_card(p, GOAL, CLAIMS)
    assert p.call_count == 2, "第二次应当全部命中缓存"
    assert len(db.list_v2_card_drafts()) == 1, "生成了重复版本"


def test_each_call_is_cached_independently(tmp_db):
    """正文没变、只重跑结构时，不该重新生成正文。"""
    p = p_fake()
    write_card(p, GOAL, CLAIMS)
    assert p.call_count == 2
    write_card(p, GOAL, CLAIMS, prompt_version="v2")  # 版本变 → 两个都失效
    assert p.call_count == 4
    write_card(p, GOAL, CLAIMS, prompt_version="v2")
    assert p.call_count == 4


def test_bad_cite_is_retried_then_fails_without_draft(tmp_db):
    """引用不存在的证据编号：重试后仍失败，且不留半成品。"""
    def bad(task, inputs):
        d = make_body() if task == "card_body" else make_structure()
        if task == "card_body":
            d["explanation"][0]["cites"] = [42]
        return d
    p = FakeTextProvider(responder=bad, max_retries=1)
    with pytest.raises(ProviderError):
        write_card(p, GOAL, CLAIMS)
    assert len(db.list_v2_card_drafts()) == 0, "失败时不该留下草稿"


def test_too_few_claims_is_rejected(tmp_db):
    p = p_fake()
    with pytest.raises(ValueError, match="不足以规划学习包"):
        write_card(p, GOAL, CLAIMS[:1])
    assert p.call_count == 0, "输入问题不该调用模型"


def test_invalid_goal_is_rejected(tmp_db):
    p = p_fake()
    bad_goal = dict(GOAL, success_evidence="")
    with pytest.raises(ValueError, match="GoalSpec 不合法"):
        write_card(p, bad_goal, CLAIMS)
    assert p.call_count == 0
