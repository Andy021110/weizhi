# -*- coding: utf-8 -*-
"""CP20 测试：M4 复习流程。密封题库、服务端判卷、掌握度回流、单卡调度。

这一层的核心不是"能判对错"，而是**答案不出现在下发给浏览器的数据里**——
所以第一条用例就在钉这件事。
"""
import json

import pytest

import db
import mastery
import review_flow
from conftest import make_card

# 一张 v2 风格的卡：题目带 layer 与 error_reason，_bridge 指向 v2 目标
V2_CARD = {
    "source_url": "https://x/h#v2:abc",
    "title": "循环的四个阶段",
    "body": "正文" * 300,
    "template": "t2_reading",
    "think_question": "迁移任务",
    "think_answer": "答案" * 80,
    "quiz": [{"question": "即时题", "options": ["A", "B"], "answer": 1,
              "explanation": "即时解析", "error_reason": "即时误解",
              "layer": "immediate", "objective": "能说出四阶段"}],
    "review_quiz": [{"question": "回忆题", "options": ["C", "D"], "answer": 0,
                     "explanation": "回忆解析", "error_reason": "回忆误解",
                     "layer": "day1", "objective": "能复述四阶段"},
                    {"question": "迁移题", "options": ["E", "F"], "answer": 1,
                     "explanation": "迁移解析", "error_reason": "迁移误解",
                     "layer": "day7", "objective": "能迁移到新场景"}],
    "open_question": {"question": "简答题", "reference_answer": "参考答案"},
    "_bridge": {"origin": "v2", "goal_key": "g-1", "milestone": "m1", "draft_id": 7},
}


# ---------- 密封：答案不能下发 ----------

def test_merge_orders_by_layer_deterministically():
    """判卷靠下标找答案，顺序一变就对错题，所以顺序必须是确定的。"""
    qs = review_flow.merge_questions(V2_CARD)
    assert [q["layer"] for q in qs] == ["immediate", "day1", "day7"]
    assert [q["index"] for q in qs] == [0, 1, 2]
    # 同层内保持原顺序
    card = dict(V2_CARD)
    card["review_quiz"] = V2_CARD["review_quiz"] + [
        {"question": "同层第二题", "options": ["G", "H"], "answer": 0, "layer": "day1"}]
    again = review_flow.merge_questions(card)
    assert [q["question"] for q in again if q["layer"] == "day1"] == ["回忆题", "同层第二题"]
    # 反复调用结果一致
    assert review_flow.merge_questions(card) == again


def test_v1_card_without_layer_is_inferred_by_field():
    """v1 老卡没有 layer：随堂题→immediate，复习题→day1。"""
    qs = review_flow.merge_questions(make_card())
    layers = [q["layer"] for q in qs]
    assert layers == ["immediate"] * 3 + ["day1"] * 3


def test_seal_removes_every_answer_field():
    sealed = review_flow.seal(review_flow.merge_questions(V2_CARD))
    for q in sealed:
        for field in review_flow.SEALED_DROP:
            assert field not in q, "密封后仍带 %s" % field
        assert q["question"] and q["options"] is not None


def test_queue_card_carries_no_answer_anywhere():
    """整份下发数据里不能出现任何答案字段——这是这一层的存在理由。"""
    qc = review_flow.queue_card(V2_CARD)
    blob = json.dumps(qc, ensure_ascii=False)
    assert "即时解析" not in blob and "回忆解析" not in blob
    assert "即时误解" not in blob
    assert "参考答案" not in blob, "open_question 的参考答案也是答案"
    assert "quiz" not in qc and "review_quiz" not in qc
    # open_question 允许留下**题干**（前端要显示问题），但不许带答案
    assert qc["open_question"] == {"question": "简答题"}
    assert "reference_answer" not in blob and "grading_points" not in blob
    # 但题面必须在
    assert [q["question"] for q in qc["questions"]] == ["回忆题", "迁移题"]
    assert qc["layers"] == {"day1": 1, "day7": 1}
    assert qc["question_count"] == 2


def test_queue_uses_review_layers_not_immediate():
    """immediate 是「学完当场测」，不该在后续每次复习里反复出现。"""
    assert "即时题" not in [q["question"] for q in review_flow.queue_card(V2_CARD)["questions"]]


def test_queue_falls_back_when_card_has_no_review_layer():
    """老卡可能只有随堂题，这时要回退到全部，否则它永远进不了复习。"""
    old = make_card(review_quiz=[])
    qc = review_flow.queue_card(old)
    assert qc["question_count"] == 3
    assert all(q["layer"] == "immediate" for q in qc["questions"])


def test_study_questions_take_immediate_only():
    assert [q["question"] for q in review_flow.study_questions(V2_CARD)] == ["即时题"]


# ---------- 判卷 ----------

def _save(card):
    assert db.save_card(card, date="2026-09-12") is True
    return db.get_card(card["source_url"])


def test_correct_answer_is_graded_by_server(tmp_db):
    _save(V2_CARD)
    fb, err = review_flow.answer_question(V2_CARD["source_url"], 1, 0)
    assert err is None and fb["correct"] is True
    assert fb["correct_index"] == 0
    assert fb["correct_option"] == "C"
    assert fb["explanation"] == "回忆解析"
    assert fb["error_reason"] is None, "答对了不该显示错误原因"
    assert fb["layer"] == "day1"


def test_wrong_answer_returns_error_reason(tmp_db):
    """方案 M4：用户要能看到错在哪，不只看到正确答案。"""
    _save(V2_CARD)
    fb, _ = review_flow.answer_question(V2_CARD["source_url"], 1, 1)
    assert fb["correct"] is False
    assert fb["error_reason"] == "回忆误解"
    assert fb["correct_option"] == "C"


def test_v2_card_updates_mastery(tmp_db):
    _save(V2_CARD)
    fb, _ = review_flow.answer_question(V2_CARD["source_url"], 1, 1, elapsed_ms=12000)
    assert fb["mastery"], "v2 卡的作答要回流到掌握度"
    assert fb["mastery"]["goal_key"] == "g-1" and fb["mastery"]["concept"] == "m1"
    assert db.get_v2_mastery("g-1", "m1")["attempts"] == 1
    assert len(db.list_v2_reviews(goal_key="g-1")) == 1


def test_v1_card_does_not_touch_v2_mastery(tmp_db):
    """v1 老卡没有里程碑与目标的概念，塞进 v2 掌握度只会污染它。"""
    card = make_card(source_url="custom:old:1")
    _save(card)
    fb, _ = review_flow.answer_question("custom:old:1", 0, 0)
    assert fb["correct"] is True
    assert fb["mastery"] is None
    assert db.list_v2_reviews(goal_key="g-1") == []


def test_mastery_is_per_question(tmp_db):
    """逐题记录，卡片的调度才按整卡走——两者不能混。"""
    _save(V2_CARD)
    review_flow.answer_question(V2_CARD["source_url"], 0, 0)
    review_flow.answer_question(V2_CARD["source_url"], 1, 1)
    rows = db.list_v2_reviews(goal_key="g-1")
    assert len(rows) == 2
    assert sorted(r["question_idx"] for r in rows) == [0, 1]


def test_fast_wrong_is_penalized_more_than_slow_wrong(tmp_db):
    """很快答错 = 高置信地答错，最危险，扣得更多。"""
    _save(V2_CARD)
    fast, _ = review_flow.answer_question(V2_CARD["source_url"], 1, 1, elapsed_ms=2000)
    # 换一个里程碑：同一 (goal, concept) 是累进的，先后作答没法比较
    slow_card = dict(V2_CARD, source_url="https://x/h#v2:slow",
                     _bridge=dict(V2_CARD["_bridge"], milestone="m2"))
    _save(slow_card)
    slow, _ = review_flow.answer_question("https://x/h#v2:slow", 1, 1, elapsed_ms=60000)
    assert fast["mastery"]["score"] < slow["mastery"]["score"]


@pytest.mark.parametrize("elapsed,expect", [
    (1500, 0.9), (6000, 0.9), (6001, 0.6), (20000, 0.6), (20001, 0.3), (None, None),
])
def test_confidence_proxy(elapsed, expect):
    assert review_flow.confidence_proxy(elapsed) == expect


def test_refuses_unknown_card(tmp_db):
    fb, err = review_flow.answer_question("不存在", 0, 0)
    assert fb is None and "不存在" in err


@pytest.mark.parametrize("index,chosen,expect", [
    (9, 0, "题号越界"), (0, 9, "选项越界"), (-1, 0, "题号越界"),
])
def test_refuses_out_of_range(tmp_db, index, chosen, expect):
    _save(V2_CARD)
    fb, err = review_flow.answer_question(V2_CARD["source_url"], index, chosen)
    assert fb is None and expect in err


def test_refuses_question_without_answer(tmp_db):
    """题目没有可判的答案属于数据问题，不能假装用户答错了——那会污染掌握度。"""
    card = make_card(source_url="custom:noans:1",
                     quiz=[{"question": "没有答案的题", "options": ["A", "B"]}],
                     review_quiz=[])
    _save(card)
    fb, err = review_flow.answer_question("custom:noans:1", 0, 0)
    assert fb is None and "没有可用答案" in err
    assert db.list_v2_reviews(goal_key="g-1") == []


# ---------- 单卡调度 ----------

def test_finish_advances_sm2(tmp_db):
    _save(V2_CARD)
    res, err = review_flow.finish_card(V2_CARD["source_url"], 2, 3)
    assert err is None and res["passed"] is True
    assert res["state"]["next_review_at"]
    after = db.get_card(V2_CARD["source_url"])
    assert after["review_count"] == 1
    assert after["memory_state"] == "learning"


def test_finish_below_threshold_resets_interval(tmp_db):
    _save(V2_CARD)
    res, _ = review_flow.finish_card(V2_CARD["source_url"], 1, 3)
    assert res["passed"] is False
    assert res["state"]["interval_days"] == 1


def test_single_question_must_be_right_to_pass(tmp_db):
    """1 道题时向上取整 → 答对 1 道才算过。"""
    _save(V2_CARD)
    res, _ = review_flow.finish_card(V2_CARD["source_url"], 1, 1)
    assert res["passed"] is True
    _save(dict(V2_CARD, source_url="https://x/h#v2:one"))
    res2, _ = review_flow.finish_card("https://x/h#v2:one", 0, 1)
    assert res2["passed"] is False


@pytest.mark.parametrize("correct,total,expect", [
    (0, 0, "total 必须大于 0"), (4, 3, "0..total"), (-1, 3, "0..total"),
    ("x", 3, "必须是整数"),
])
def test_finish_rejects_bad_numbers(tmp_db, correct, total, expect):
    _save(V2_CARD)
    res, err = review_flow.finish_card(V2_CARD["source_url"], correct, total)
    assert res is None and expect in err


def test_finish_refuses_unknown_card(tmp_db):
    res, err = review_flow.finish_card("不存在", 1, 1)
    assert res is None and "不存在" in err


# ---------- 桥接：v2 卡要能进复习队列 ----------

def test_bridged_card_enters_review_queue(tmp_db):
    """`db.save_card` 不写 next_review_at，而队列只取已到期的卡——
    桥接不显式给这一列，v2 的卡就永远不会出现在复习里。"""
    import bridge_v1
    from datetime import datetime, timedelta

    draft = {"title": "t", "lead": "l", "objective": "o",
             "explanation": [{"text": "解释", "cites": [0]}],
             "examples": [{"text": "例子", "cites": [0]}],
             "boundaries": [], "key_points": ["k"], "transfer_task": "任务"}
    items = [{"question": "q", "options": ["A", "B"], "answer": 0,
              "layer": "immediate", "explanation": "e", "error_reason": "r"}]
    card = bridge_v1.to_v1_card(draft, items, {"url": "https://y/1"})
    assert card["next_review_at"], "桥接必须给初始复习时间"
    assert card["memory_state"] == "learning"
    assert bridge_v1.save(card, date="2026-09-11") is True

    # 到期当天才出现，前一天不出现（用卡片自己报的日期，不写死基准日）
    due_date = card["next_review_at"]
    assert db.get_due_reviews("2026-01-01") == []
    due = db.get_due_reviews(due_date)
    assert len(due) == 1
    assert (due[0].get("_bridge") or {}).get("origin") == "v2"


def test_next_review_at_lands_in_column_not_extra(tmp_db):
    """两个调度列必须写进真正的列：若落进 extra，读回时会被 extra 覆盖，
    于是"按 next_review_at 查到期卡"在写入之后就查不到了。"""
    card = dict(V2_CARD, next_review_at="2026-09-12", memory_state="learning")
    _save(card)
    got = db.get_card(V2_CARD["source_url"])
    assert got["next_review_at"] == "2026-09-12"
    assert got["memory_state"] == "learning"
    assert len(db.get_due_reviews("2026-09-12")) == 1


def test_bridge_keeps_goal_key_for_mastery_attribution():
    import bridge_v1
    draft = {"title": "t", "lead": "l", "objective": "o",
             "explanation": [{"text": "x", "cites": [0]}],
             "examples": [], "boundaries": [], "key_points": [], "transfer_task": "t"}
    card = bridge_v1.to_v1_card(draft, [], {"url": "https://y/1"},
                                goal_key="g-9", concept="m3", capability_gap="画不出循环图")
    b = card["_bridge"]
    assert b["goal_key"] == "g-9" and b["milestone"] == "m3"
    assert b["capability_gap"] == "画不出循环图"


def test_set_card_review_schedule_only_fills_blanks(tmp_db):
    """补调度会被重复触发（每次升级桥接规则），所以绝不能覆盖已有进度——
    否则用户复习出来的间隔会被打回原点。"""
    card = dict(V2_CARD, next_review_at="2026-09-12", memory_state="learning")
    _save(card)
    assert db.set_card_review_schedule(V2_CARD["source_url"], "2026-10-01") is False
    assert db.get_card(V2_CARD["source_url"])["next_review_at"] == "2026-09-12"


def test_set_card_review_schedule_fills_empty(tmp_db):
    card = dict(V2_CARD)
    card.pop("next_review_at", None)
    _save(card)
    assert db.get_card(V2_CARD["source_url"])["next_review_at"] is None
    assert db.set_card_review_schedule(V2_CARD["source_url"], "2026-09-13") is True
    got = db.get_card(V2_CARD["source_url"])
    assert got["next_review_at"] == "2026-09-13" and got["memory_state"] == "learning"
    assert db.set_card_review_schedule("不存在", "2026-09-13") is False


def test_refresh_backfills_review_schedule(tmp_db):
    """刷新要把早期入库、没有调度信息的 v2 卡补上，否则它们进不了复习。"""
    import bridge_v1
    import evidence
    sid, _ = evidence.ingest_source("https://x/rf", "循环四阶段与状态回写。" * 8,
                                    title="刷新用材料")
    did = db.save_v2_card_draft(input_hash="rf-h", schema_version="1.0",
                                payload={"title": "无调度的卡"}, source_id=sid,
                                status="draft")
    draft = {"title": "无调度的卡", "lead": "l", "objective": "o",
             "explanation": [{"text": "解释", "cites": [0]}], "examples": [],
             "boundaries": [], "key_points": [], "transfer_task": "t"}
    card = bridge_v1.to_v1_card(draft, [], {"url": "https://x/rf"})
    card.pop("next_review_at", None)      # 模拟旧代码入库的卡
    card.pop("memory_state", None)
    assert bridge_v1.save(card, date="2026-09-12") is True
    # save 会把 source_url 加上 v2 前缀去重，取回来要用同一个键
    key = bridge_v1.stored_key("https://x/rf", draft["title"])
    assert db.get_card(key)["next_review_at"] is None

    result = bridge_v1.refresh()
    entry = [u for u in result["updated"] if u["draft_id"] == did]
    assert entry and entry[0]["scheduled"] is True
    assert db.get_card(key)["next_review_at"]
    assert db.get_card(key)["memory_state"] == "learning"


# ---------- CP21：学习/自测侧也要密封 ----------

def test_study_card_keeps_only_immediate_layer():
    sc = review_flow.study_card(V2_CARD)
    assert [q["question"] for q in sc["questions"]] == ["即时题"]
    assert sc["layers"] == {"immediate": 1}
    assert "quiz" not in sc and "review_quiz" not in sc
    blob = json.dumps(sc, ensure_ascii=False)
    assert "即时解析" not in blob and "即时误解" not in blob


def test_study_card_keeps_question_but_not_answer():
    """简答题要能显示题干，参考答案必须留服务端。"""
    sc = review_flow.study_card(V2_CARD)
    assert sc["open_question"] == {"question": "简答题"}
    assert sc["has_open_question"] is True
    assert "reference_answer" not in json.dumps(sc, ensure_ascii=False)


def test_open_question_of_reads_from_server_side_card():
    """参考答案只在服务端可读——判分时由服务端自己取。"""
    oq = review_flow.open_question_of(V2_CARD)
    assert oq["reference_answer"] == "参考答案"
    assert review_flow.open_question_of({"open_question": {"question": "x"}}) is None
    assert review_flow.open_question_of({}) is None


def test_seal_cards_is_batch_and_safe_on_empty():
    assert review_flow.seal_cards([]) == []
    assert review_flow.seal_cards(None) == []
    out = review_flow.seal_cards([V2_CARD, make_card(source_url="custom:x:1")])
    assert len(out) == 2
    for c in out:
        assert "quiz" not in c and "review_quiz" not in c
        assert c["questions"], "老卡也要有可做的题"


def test_vocab_style_card_without_questions_still_works():
    """词汇卡可能没有选择题（只有 words）。密封不能让它变成空卡。"""
    card = make_card(template="t1_vocab", quiz=[], review_quiz=[])
    sc = review_flow.study_card(card)
    assert sc["question_count"] == 0
    assert sc["layers"] == {}
    assert sc["title"] == card["title"]
