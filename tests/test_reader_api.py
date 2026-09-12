# -*- coding: utf-8 -*-
"""CP21 测试：HTTP 层的密封与参数解析。

这些是最容易被忽略的一层——业务逻辑都对，接口却把答案发出去了，
或者把合法的 `0` 当成缺失值。
"""
import pytest

from weizhi.core import db
from weizhi.serve import reader
from weizhi.serve import review_flow
from conftest import make_card


# ---------- 参数解析：0 是合法值 ----------

@pytest.mark.parametrize("value,expect", [
    (0, 0), ("0", 0), (1, 1), ("2", 2),          # 0 必须原样保留
    (None, -1), ("", -1), ("abc", -1), (-1, -1),
])
def test_int_arg_keeps_zero(value, expect):
    """`int(x or -1)` 会把 0 换成 -1——第 1 题与选项 A 永远判越界。真踩过。"""
    assert reader._int_arg(value) == expect


def test_int_arg_custom_default():
    assert reader._int_arg(None, default=0) == 0
    assert reader._int_arg("x", default=7) == 7


# ---------- 演示模式（只读口令） ----------
#
# 这个实例要放出去给人看（作品集/面试），所以「能看」和「能改」必须分开。
# 这组测试守的就是那条线：演示口令一条写都做不了，而且
# 演示口令留空时不能变成「空字符串就能进」的后门。

OWNER_TOKEN = "OWNER-abc"
DEMO_TOKEN = "DEMO-xyz"


def _tokens(demo=True, owner=True):
    t = {}
    if owner:
        t[OWNER_TOKEN] = reader.ROLE_OWNER
    if demo:
        t[DEMO_TOKEN] = reader.ROLE_DEMO
    return t


def test_demo_token_maps_to_demo_role():
    roles = _tokens()
    assert reader.resolve_role(DEMO_TOKEN, roles) == reader.ROLE_DEMO
    assert reader.resolve_role(OWNER_TOKEN, roles) == reader.ROLE_OWNER
    assert reader.resolve_role("乱写的口令", roles) is None


def test_blank_token_is_not_a_backdoor():
    """demo_token 留空/只有空格时，不能让空口令通过。"""
    assert reader.resolve_role("", _tokens(demo=False)) is None
    assert reader.resolve_role("   ", _tokens(demo=False)) is None


def test_no_token_configured_means_owner():
    """老部署一个 token 都没配：必须继续可用，不能把自己锁在门外。"""
    assert reader.resolve_role("", {}) == reader.ROLE_OWNER
    assert reader.resolve_role("随便什么", {}) == reader.ROLE_OWNER


def test_load_tokens_skips_empty_values(monkeypatch):
    monkeypatch.setattr(reader, "load_config", lambda: {
        "access_token": OWNER_TOKEN, "demo_token": "  "})
    assert reader.load_tokens() == {OWNER_TOKEN: reader.ROLE_OWNER}


def test_demo_role_cannot_write_anything():
    """演示 = 零写入。唯一例外是 /api/verify——它是拿口令换角色的入口本身。"""
    assert reader.demo_can_write("/api/verify")
    for p in ("/api/create", "/api/done", "/api/delete", "/api/edit",
              "/api/review", "/api/review/master", "/api/review/skip",
              "/api/favorite", "/api/state", "/api/event", "/api/grade",
              "/api/plan/create", "/api/plan/outline", "/api/plan/classify",
              "/api/plan/disambiguate", "/api/plan/update", "/api/plan/delete",
              "/api/ledger/encounter", "/api/ledger/make-card",
              "/api/regen", "/api/rollback", "/api/notifications/read"):
        assert not reader.demo_can_write(p), "%s 对演示角色必须是只读" % p


def test_new_write_endpoints_are_locked_by_default():
    """写锁是「默认拒绝」而不是「逐个列举允许」。
    以后新增写接口忘了改这里，demo 也进不去——这正是要的方向。"""
    assert not reader.demo_can_write("/api/以后新加的写接口")


def test_demo_access_log_separates_me_from_visitors(tmp_db):
    """能分清「我自己用」和「别人用」——这是要演示口令的另一个原因。"""
    db.record_demo_access("203.0.113.9", "GET", "/api/cards", False)
    db.record_demo_access("203.0.113.9", "POST", "/api/create", True)
    db.record_demo_access("198.51.100.4", "GET", "/api/discover", False)
    s = db.demo_access_summary()
    assert s["requests"] == 3
    assert s["visitors"] == 2          # 两个访客，不是一个
    assert s["blocked"] == 1           # 想生成卡被拦了一次
    assert s["recent"][0]["path"] == "/api/discover"


def test_demo_access_log_is_capped(tmp_db):
    """流水要封顶：留着的意义是观察，不是归档。"""
    for i in range(30):
        db.record_demo_access("10.0.0.%d" % (i % 3), "GET", "/api/cards", False,
                             keep=10)
    assert db.demo_access_summary()["requests"] == 10


# ---------- 密封辅助 ----------

def test_sealed_batch_strips_all_answers():
    import json
    card = make_card(
        quiz=[{"question": "q1", "options": ["A", "B"], "answer": 0,
               "explanation": "解析一", "error_reason": "误解一", "layer": "immediate"}],
        review_quiz=[{"question": "q2", "options": ["C", "D"], "answer": 1,
                      "explanation": "解析二", "layer": "day1"}],
        open_question={"question": "简答", "reference_answer": "参考",
                       "grading_points": ["a"]})
    out = reader._sealed([card])
    blob = json.dumps(out, ensure_ascii=False)
    assert len(out) == 1
    for leaked in ('"answer"', "解析一", "解析二", "误解一", "参考", "grading_points"):
        assert leaked not in blob, "接口把 %s 发出去了" % leaked
    assert out[0]["questions"]


def test_sealed_one_returns_only_sealed_fields():
    card = make_card()
    merged = reader._sealed_one(card)
    assert set(merged).issubset({"questions", "layers", "question_count",
                                 "open_question", "has_open_question"})
    assert merged["questions"] and merged["question_count"] == len(merged["questions"])


def test_sealed_one_on_empty_card():
    assert reader._sealed_one(None) == {}


def test_sealed_is_passthrough_when_review_flow_missing(monkeypatch):
    """review_flow 缺失时 v1 老路径不能断——原样返回比抛异常好。"""
    monkeypatch.setattr(reader, "review_flow", None)
    cards = [make_card()]
    assert reader._sealed(cards) is cards
    assert reader._sealed_one(cards[0]) == {}


# ---------- 判卷接口的输入契约 ----------

def test_answer_endpoint_accepts_question_zero(tmp_db):
    """回归：第 1 题（index=0）必须能判，不能被当成缺失值。"""
    from weizhi.core import db
    card = make_card(
        source_url="custom:z:1",
        quiz=[{"question": "第一题", "options": ["A", "B"], "answer": 0,
               "explanation": "e", "layer": "immediate"}],
        review_quiz=[])
    assert db.save_card(card, date="2026-09-12") is True
    fb, err = review_flow.answer_question("custom:z:1", reader._int_arg("0"),
                                          reader._int_arg("0"))
    assert err is None and fb["correct"] is True
    assert fb["correct_index"] == 0


def test_answer_endpoint_rejects_missing_index(tmp_db):
    from weizhi.core import db
    card = make_card(source_url="custom:z:2")
    assert db.save_card(card, date="2026-09-12") is True
    fb, err = review_flow.answer_question("custom:z:2", reader._int_arg(None),
                                          reader._int_arg(None))
    assert fb is None and "题号越界" in err
