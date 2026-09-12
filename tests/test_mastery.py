"""CP10 测试：掌握度、遗忘风险、复习调度。全部确定性，不调模型。"""
from datetime import datetime, timedelta

from weizhi.core import db
from weizhi.serve import mastery


def test_correct_moves_toward_one():
    s = mastery.update_score(0.3, correct=True)
    assert 0.3 < s < 1.0
    for _ in range(10):
        s = mastery.update_score(s, correct=True)
    assert s > 0.9


def test_wrong_costs_more_than_correct_gains():
    """答错扣得比答对加得狠——错一次推翻的是既有印象。"""
    up = mastery.update_score(0.5, correct=True) - 0.5
    down = 0.5 - mastery.update_score(0.5, correct=False)
    assert down > up


def test_confident_and_wrong_is_penalized_extra():
    """自信地答错是最危险的信号：他建立了错误的信心。"""
    plain = mastery.update_score(0.6, correct=False)
    confident = mastery.update_score(0.6, correct=False, confidence=0.9)
    assert confident < plain


def test_score_is_bounded():
    assert mastery.update_score(0.0, correct=False) == 0.0
    assert mastery.update_score(1.0, correct=True) <= 1.0


def test_record_writes_log_and_state(tmp_db):
    st = mastery.record("g-1", "m1", correct=True, confidence=0.8, elapsed_ms=4200)
    assert st["attempts"] == 1 and st["correct"] == 1
    assert st["next_review_at"] > st["last_review_at"]
    logs = db.list_v2_reviews(goal_key="g-1")
    assert len(logs) == 1
    assert logs[0]["elapsed_ms"] == 4200 and logs[0]["confidence"] == 0.8


def test_record_keeps_raw_log_even_if_summary_recomputed(tmp_db):
    """汇总可重算，原始记录不可再生——两者分开存。"""
    mastery.record("g-1", "m1", correct=False, error_type="概念混淆")
    mastery.record("g-1", "m1", correct=True)
    assert len(db.list_v2_reviews(goal_key="g-1")) == 2
    assert db.get_v2_mastery("g-1", "m1")["attempts"] == 2


def test_hint_used_shortens_interval(tmp_db):
    """用了提示的答对不算真会：掌握度照算，但间隔不放大。"""
    a = mastery.record("g-1", "m1", correct=True, hint_used=False)
    b = mastery.record("g-2", "m1", correct=True, hint_used=True)
    assert b["interval_days"] < a["interval_days"]


def test_risk_high_when_never_reviewed(tmp_db):
    assert mastery.risk(None) == 1.0
    assert mastery.risk({"score": 0.5, "last_review_at": None}) == 1.0


def test_risk_grows_with_time():
    now = datetime(2026, 9, 11, 12, 0, 0)
    fresh = {"score": 0.8, "last_review_at": (now - timedelta(hours=1)).isoformat()}
    stale = {"score": 0.8, "last_review_at": (now - timedelta(days=20)).isoformat()}
    assert mastery.risk(fresh, now) < mastery.risk(stale, now)
    assert mastery.risk(stale, now) > 0.8


def test_higher_mastery_decays_slower():
    now = datetime(2026, 9, 11, 12, 0, 0)
    when = (now - timedelta(days=5)).isoformat()
    low = {"score": 0.2, "last_review_at": when}
    high = {"score": 0.9, "last_review_at": when}
    assert mastery.risk(high, now) < mastery.risk(low, now)


def test_interval_grows_with_mastery_and_is_capped():
    assert mastery.next_interval(0.9) > mastery.next_interval(0.2)
    assert mastery.next_interval(1.0) <= mastery.MAX_INTERVAL


def test_due_orders_by_risk(tmp_db):
    now = datetime(2026, 9, 11, 12, 0, 0)
    db.upsert_v2_mastery("g-1", "m1", score=0.5,
                         next_review_at=(now - timedelta(days=1)).isoformat(),
                         last_review_at=(now - timedelta(days=3)).isoformat())
    db.upsert_v2_mastery("g-1", "m2", score=0.5,
                         next_review_at=(now - timedelta(days=9)).isoformat(),
                         last_review_at=(now - timedelta(days=11)).isoformat())
    out = mastery.due("g-1", now=now)
    assert [s["concept"] for s in out] == ["m2", "m1"], "风险高的排前面"


def test_due_excludes_not_yet_due(tmp_db):
    now = datetime(2026, 9, 11, 12, 0, 0)
    db.upsert_v2_mastery("g-1", "m1", score=0.9,
                         next_review_at=(now + timedelta(days=5)).isoformat(),
                         last_review_at=now.isoformat())
    assert mastery.due("g-1", now=now) == []


def test_weakest_excludes_mastered(tmp_db):
    db.upsert_v2_mastery("g-1", "m1", score=0.9)
    db.upsert_v2_mastery("g-1", "m2", score=0.2)
    out = mastery.weakest("g-1", concepts=["m1", "m2"])
    assert [s["concept"] for s in out] == ["m2"]


def test_weakest_treats_unseen_as_gap(tmp_db):
    """没测过的里程碑也是缺口，不能因为「没有记录」就被跳过。"""
    out = mastery.weakest("g-1", concepts=["m1", "m2"])
    assert len(out) == 2


def test_explain_is_human_readable(tmp_db):
    assert "还没有复习记录" in mastery.explain(None)
    st = mastery.record("g-1", "m1", correct=False)
    text = mastery.explain(st)
    assert "掌握度" in text and "遗忘风险" in text
