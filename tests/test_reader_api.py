# -*- coding: utf-8 -*-
"""CP21 测试：HTTP 层的密封与参数解析。

这些是最容易被忽略的一层——业务逻辑都对，接口却把答案发出去了，
或者把合法的 `0` 当成缺失值。
"""
import pytest

import reader
import review_flow
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
    import db
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
    import db
    card = make_card(source_url="custom:z:2")
    assert db.save_card(card, date="2026-09-12") is True
    fb, err = review_flow.answer_question("custom:z:2", reader._int_arg(None),
                                          reader._int_arg(None))
    assert fb is None and "题号越界" in err
