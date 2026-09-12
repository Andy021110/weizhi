"""CP12 测试：题目质量门禁 + 延迟复习闭环。"""
from datetime import datetime, timedelta

import pytest

from weizhi.produce import assessment
from weizhi.core import db
from weizhi.serve import mastery
from weizhi.core.providers import FakeTextProvider

GOOD_ITEM = {
    "objective": "能说清循环由哪四个阶段组成",
    "layer": "immediate",
    "cites": [0],
    "question": "Agent Harness 的核心循环包含以下哪一组阶段？",
    "options": ["上下文组装、模型调用、工具执行、状态回写",
                "提示编写、模型选择、数据清洗、结果导出",
                "意图识别、槽位填充、对话管理、语音合成",
                "数据采集、特征工程、模型训练、上线部署"],
    "answer": 0,
    "explanation": "证据里明确给出这四个阶段。",
    "error_reason": "容易和通用机器学习流水线混淆。",
}


def _provider(items):
    return FakeTextProvider(responder=lambda task, inputs: {"items": items})


def _draft_row(goal_key="g-1", concept="m1", items=None, status="draft"):
    did = db.save_v2_card_draft(
        input_hash="h-%s-%s-%s" % (goal_key, concept, status),
        schema_version="1.0", payload={"title": "循环的四个阶段"},
        goal_key=goal_key, concept=concept, objective="能说清四个阶段", status=status)
    assessment.attach(did, items or [dict(GOOD_ITEM)], concept=concept)
    return did


# ---------- 结构校验 ----------

def test_good_item_passes():
    assert assessment.validate_quiz({"items": [GOOD_ITEM]}) == []


@pytest.mark.parametrize("mutate,expect", [
    (lambda d: d.pop("objective"), "objective"),
    (lambda d: d.update(cites=[]), "cites"),
    (lambda d: d.update(question="短"), "question"),
    (lambda d: d.update(options=["A", "B"]), "少于"),
    (lambda d: d.update(answer=9), "越界"),
    (lambda d: d.pop("explanation"), "explanation"),
    (lambda d: d.pop("error_reason"), "error_reason"),
])
def test_rejects_bad_item(mutate, expect):
    item = dict(GOOD_ITEM)
    mutate(item)
    errs = assessment.validate_quiz({"items": [item]})
    assert errs and any(expect in e for e in errs), errs


def test_layer_is_required_and_validated():
    """范围决策 D2：随堂题与复习题合并为分层题库。"""
    bad = dict(GOOD_ITEM, layer="第三层")
    errs = assessment.validate_quiz({"items": [bad]})
    assert any("layer" in e for e in errs)
    # 每种合法 layer 都要能通过（day1/day7 需与 immediate 搭配，见下一条用例）
    for layer in assessment.LAYERS:
        items = [dict(GOOD_ITEM, layer="immediate")]
        if layer != "immediate":
            items.append(dict(GOOD_ITEM, layer=layer))
        assert assessment.validate_quiz({"items": items}) == [], layer


def test_needs_at_least_one_immediate():
    """学完当场必须能测——分层不能全堆到 7 天后。"""
    only_day7 = [dict(GOOD_ITEM, layer="day7")]
    errs = assessment.validate_quiz({"items": only_day7})
    assert any("immediate" in e for e in errs)


def test_single_question_is_enough():
    """范围决策 D1：「每卡三道题」已废除，单一判断目标一道好题就够。"""
    assert assessment.validate_quiz({"items": [dict(GOOD_ITEM, layer="immediate")]}) == []


def test_count_is_optional_and_hint_is_adaptive():
    """不传 count 时由目标决定题量，不是写死 3。"""
    inputs = assessment.build_inputs({"objective": "x", "title": "t"}, [], None)
    assert "由学习目标决定" in inputs["count_hint"]
    forced = assessment.build_inputs({"objective": "x", "title": "t"}, [], 2)
    assert "固定出 2 道" in forced["count_hint"]


def test_by_layer():
    items = [dict(GOOD_ITEM, layer="immediate"), dict(GOOD_ITEM, layer="day7")]
    assert len(assessment.by_layer(items, "day7")) == 1
    assert assessment.by_layer(items, "day1") == []


def test_error_reason_is_required():
    """方案 M4：用户要能看到错误原因，不能只给正确答案。"""
    item = dict(GOOD_ITEM, error_reason="")
    assert any("error_reason" in e for e in assessment.validate_quiz({"items": [item]}))


# ---------- 质量门禁 ----------

def test_answer_uniqueness_catches_duplicate_options():
    item = dict(GOOD_ITEM, options=["一样", "一样", "不一样", "也不同"])
    assert any(t == "选项重复" for t, _ in assessment.check_answer_uniqueness(item))


def test_answer_uniqueness_catches_containment():
    """一个选项被另一个包含 → 两者都说得通 → 坏题。"""
    item = dict(GOOD_ITEM, options=["四个阶段", "四个阶段加一个调度层", "三阶段", "两阶段"])
    assert any(t == "选项包含" for t, _ in assessment.check_answer_uniqueness(item))


def test_ambiguity_catches_catch_all_options():
    item = dict(GOOD_ITEM, options=["都对", "以上都对", "只有A", "只有B"])
    assert any(t == "语面歧义" for t, _ in assessment.check_option_ambiguity(item))


def test_ambiguity_catches_length_skew():
    """正确答案被写得特别长会成为破题线索。"""
    item = dict(GOOD_ITEM, options=["很长很长的正确选项描述文字内容这么多",
                                    "短", "也短", "还是很短"])
    assert any(t == "选项参差" for t, _ in assessment.check_option_ambiguity(item))


def test_leak_catches_answer_in_question():
    item = dict(GOOD_ITEM, question="上下文组装、模型调用、工具执行、状态回写属于什么？",
                options=["上下文组装、模型调用、工具执行、状态回写", "其他", "另外", "再其他"])
    assert any(t == "答案泄露" for t, _ in assessment.check_answer_leak(item))


def test_quality_report_reports_coverage():
    report = assessment.quality_report([dict(GOOD_ITEM), dict(GOOD_ITEM, objective="")])
    assert report["objective_coverage"] == 0.5
    assert report["passed"] is False


def test_quality_report_passes_for_good_set():
    report = assessment.quality_report([dict(GOOD_ITEM)])
    assert report["passed"] is True
    assert report["objective_coverage"] == 1.0


# ---------- 生成 ----------

def test_generate_attaches_ids_and_persists(tmp_db):
    did = _draft_row()
    items, report = assessment.generate(
        _provider([dict(GOOD_ITEM)]), {"objective": "x", "title": "t"},
        [{"claim_idx": 0, "text": "证据"}], concept="m1", draft_id=did)
    assert items[0]["id"] == "q1" and items[0]["concept"] == "m1"
    assert report["passed"] is True
    assert db.get_v2_draft_assessment(did)[0]["id"] == "q1"


def test_generate_rejects_unknown_cites(tmp_db):
    from weizhi.core.providers import ProviderError
    bad = dict(GOOD_ITEM, cites=[99])
    with pytest.raises(ProviderError):
        assessment.generate(_provider([bad]), {"objective": "x", "title": "t"},
                            [{"claim_idx": 0, "text": "证据"}])


# ---------- 复习闭环 ----------

def test_due_reviews_returns_cards_with_items(tmp_db):
    now = datetime(2026, 9, 11, 12, 0, 0)
    did = _draft_row()
    db.upsert_v2_mastery("g-1", "m1", score=0.4,
                         next_review_at=(now - timedelta(days=1)).isoformat(),
                         last_review_at=(now - timedelta(days=3)).isoformat())
    out = assessment.due_reviews("g-1", now=now)
    assert len(out) == 1
    assert out[0]["draft_id"] == did
    assert out[0]["items"][0]["id"] == "q1"


def test_rejected_cards_do_not_enter_review(tmp_db):
    """被门禁拒掉的卡不该占用复习时间。"""
    now = datetime(2026, 9, 11, 12, 0, 0)
    _draft_row(status="rejected")
    db.upsert_v2_mastery("g-1", "m1", score=0.4,
                         next_review_at=(now - timedelta(days=1)).isoformat(),
                         last_review_at=(now - timedelta(days=3)).isoformat())
    assert assessment.due_reviews("g-1", now=now) == []


def test_submit_correct_updates_mastery(tmp_db):
    did = _draft_row()
    fb, status = assessment.submit("g-1", "m1", did, 0, chosen=0, confidence=0.9)
    assert status == "ok" and fb["correct"] is True
    assert fb["error_reason"] is None
    assert fb["mastery_after"] > mastery.DEFAULT_SCORE
    assert isinstance(fb["next_review_at"], str) and fb["next_review_at"]
    assert fb["answer"] == 0 and fb["correct_option"]


def test_submit_wrong_gives_error_reason(tmp_db):
    """方案 M4：答错时要告诉用户错在哪，不只给正确答案。"""
    did = _draft_row()
    fb, _ = assessment.submit("g-1", "m1", did, 0, chosen=3, confidence=0.8)
    assert fb["correct"] is False
    assert fb["error_reason"]
    assert fb["correct_option"]
    assert fb["mastery_after"] < mastery.DEFAULT_SCORE


def test_submit_records_error_type_and_hint(tmp_db):
    did = _draft_row()
    assessment.submit("g-1", "m1", did, 0, chosen=3, hint_used=True, elapsed_ms=9000)
    log = db.list_v2_reviews(goal_key="g-1")[0]
    assert log["hint_used"] == 1
    assert log["elapsed_ms"] == 9000
    assert log["error_type"]


def test_submit_rejects_bad_index(tmp_db):
    did = _draft_row()
    fb, msg = assessment.submit("g-1", "m1", did, 9, chosen=0)
    assert fb is None and "越界" in msg


def test_submit_rejects_unknown_card(tmp_db):
    fb, msg = assessment.submit("g-1", "m1", 999, 0, chosen=0)
    assert fb is None and "不存在" in msg


def test_daily_plan_prioritizes_reviews(tmp_db):
    """到期复习优先于新内容——不复习的话前面投入的时间就白花了。"""
    now = datetime(2026, 9, 11, 12, 0, 0)
    _draft_row()
    db.upsert_v2_mastery("g-1", "m1", score=0.4,
                         next_review_at=(now - timedelta(days=1)).isoformat(),
                         last_review_at=(now - timedelta(days=3)).isoformat())
    plan = assessment.daily_plan("g-1", now=now)
    assert plan["review_count"] == 1
    assert "先复习" in plan["reason"]

    empty = assessment.daily_plan("g-none", now=now)
    assert empty["review_count"] == 0
    assert "新的学习包" in empty["reason"]
