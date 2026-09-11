"""CP9 测试：目标诊断。追问上限、可观察性校验、版本化、生命周期。"""
import copy

import pytest

import db
import goalspec
import providers
from conftest import make_goalspec, make_probe
from providers import FakeTextProvider

RAW = "我想学 Agent"


def _provider(probe_data=None, spec_data=None, sufficient=None):
    """sufficient 给定则用 make_probe 构造对应判定，否则用 probe_data。"""
    if sufficient is not None:
        probe_data = make_probe(sufficient=sufficient)
    p_data = probe_data if probe_data is not None else make_probe()
    s_data = spec_data if spec_data is not None else make_goalspec()
    return FakeTextProvider(responder=lambda task, inputs:
                            p_data if task == goalspec.TASK_PROBE else s_data)


# ---------- 探针校验 ----------

def test_good_probe_passes():
    assert goalspec.validate_probe(make_probe()) == []
    assert goalspec.validate_probe(make_probe(sufficient=True, questions=[])) == []


def test_probe_rejects_too_many_questions():
    """方案 M2：最多三个高信息量问题，问太多会劝退用户。"""
    qs = [{"question": "问题%d" % i, "why": "x"} for i in range(4)]
    errs = goalspec.validate_probe(make_probe(questions=qs))
    assert any("劝退" in e for e in errs)


def test_probe_rejects_inconsistent_verdict():
    # 判定不足却没给问题 → 用户无法继续
    assert goalspec.validate_probe(make_probe(questions=[]))
    # 判定足够却还在问 → 浪费用户时间
    assert goalspec.validate_probe(
        make_probe(sufficient=True, questions=[{"question": "还要问吗", "why": "x"}]))


def test_probe_rejects_string_questions():
    errs = goalspec.validate_probe(make_probe(questions=["你水平如何？"]))
    assert any("不是字符串" in e for e in errs)


# ---------- 规格校验 ----------

def test_good_spec_passes():
    assert goalspec.validate_spec(make_goalspec()) == []


@pytest.mark.parametrize("mutate,expect", [
    (lambda d: d.update(capability=""), "capability"),
    (lambda d: d.update(capability="了解 Agent 是什么"), "不可观察"),
    (lambda d: d.update(capability="能" + "很长" * 40), ">60"),
    (lambda d: d.update(success_evidence=""), "success_evidence"),
    (lambda d: d.update(success_evidence="感觉差不多已经会用了"), "主观感受"),
    (lambda d: d.update(scene=""), "scene"),
    (lambda d: d.update(milestones=[]), "milestones"),
    (lambda d: d.update(milestones=[{"name": "a", "evidence": "b"}] * 8), "milestones"),
    (lambda d: d.update(prereq=["a"] * 7), "prereq"),
])
def test_spec_rejects_bad_output(mutate, expect):
    spec = copy.deepcopy(make_goalspec())
    mutate(spec)
    errs = goalspec.validate_spec(spec)
    assert errs, "应当被拦截: %s" % expect
    assert any(expect in e for e in errs), "错误信息应指向 %s，实际: %s" % (expect, errs)


def test_milestone_needs_evidence():
    spec = make_goalspec(milestones=[{"name": "a", "evidence": ""}] * 3)
    assert any("evidence" in e for e in goalspec.validate_spec(spec))


def test_normalize_assigns_milestone_ids():
    """里程碑 id 必须由程序分配——「每张卡映射到一个里程碑」要靠它。"""
    spec = goalspec.normalize_spec(make_goalspec(), "g-1")
    assert [m["id"] for m in spec["milestones"]] == ["m1", "m2", "m3"]
    assert spec["key"] == "g-1"
    assert spec["daily_minutes"] == 60


# ---------- 编排 ----------

def test_build_returns_questions_when_vague(tmp_db):
    r = goalspec.build(_provider(), RAW)
    assert r["status"] == "need_more"
    assert 0 < len(r["questions"]) <= goalspec.MAX_QUESTIONS
    assert r["missing"]
    assert db.list_v2_goals() == [], "信息不足时不该落库"


def test_build_with_answers_creates_draft(tmp_db):
    r = goalspec.build(_provider(sufficient=True), RAW,
                       answers={"目标能力": "能自己搭一个"})
    assert r["status"] == "draft"
    assert r["version"] == 1
    row = db.get_v2_goal(r["goal_id"])
    assert row["raw_input"] == RAW
    assert row["spec"]["milestones"][0]["id"] == "m1"


def test_confirm_activates_goal(tmp_db):
    r = goalspec.build(_provider(sufficient=True), RAW, answers={"a": "b"})
    ok, _msg = goalspec.confirm(r["goal_id"])
    assert ok is True
    assert goalspec.active_spec(r["goal_key"])["capability"]


def test_confirm_refuses_unexecutable_spec(tmp_db):
    """没有可观察成功证据的目标不许 active（方案 M2 验收门槛）。"""
    bad = make_goalspec(success_evidence="")
    p = FakeTextProvider(responder=lambda task, inputs:
                         make_probe(sufficient=True) if task == goalspec.TASK_PROBE else bad)
    with pytest.raises(providers.ProviderError):
        goalspec.build(p, RAW, answers={"a": "b"})


def test_revise_keeps_old_version(tmp_db):
    """方案 M2：用户修改目标后旧学习记录保留 → 插新版本，不改旧行。"""
    r = goalspec.build(_provider(sufficient=True), RAW, answers={"a": "b"})
    goalspec.confirm(r["goal_id"])
    v1 = db.get_v2_goal(r["goal_id"])

    new_spec = make_goalspec(capability="能独立定位并修复一次 Agent 循环故障")
    ok, info = goalspec.revise(r["goal_key"], new_spec)
    assert ok is True and info["version"] == 2

    # 旧版本还在，内容没被改写
    assert db.get_v2_goal(r["goal_id"])["spec"] == v1["spec"]
    assert data_of(r["goal_id"])["version"] == 1
    assert len(db.list_v2_goals()) == 2
    # 新版本是 draft，需要重新确认
    assert db.latest_v2_goal(r["goal_key"])["status"] == "draft"


def test_pause_and_archive(tmp_db):
    r = goalspec.build(_provider(sufficient=True), RAW, answers={"a": "b"}, confirm=True)
    goalspec.pause(r["goal_id"])
    assert goalspec.active_spec(r["goal_key"]) is None
    goalspec.archive(r["goal_id"])
    assert db.get_v2_goal(r["goal_id"])["status"] == "archived"


def test_same_raw_goal_is_idempotent(tmp_db):
    """同一句原话派生出同一个 goal_key → 重复提交不会产生两个目标。"""
    a = goalspec.build(_provider(sufficient=True), RAW, answers={"a": "b"})
    b = goalspec.build(_provider(sufficient=True), RAW, answers={"a": "b"})
    assert a["goal_key"] == b["goal_key"]


def test_empty_goal_rejected(tmp_db):
    with pytest.raises(ValueError):
        goalspec.build(_provider(), "   ")


def data_of(goal_id):
    return db.get_v2_goal(goal_id)
