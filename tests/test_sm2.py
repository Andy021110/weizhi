"""SM-2 间隔复习算法测试。

被测：db.schedule_review(source_url, quality)
- quality=1 记得：间隔按 1→3→7→15→30 递增，ease +0.1（上限 2.5）
- quality=0 忘记：间隔归 1、计数清零、ease -0.2（下限 1.3）
- 连续记得 5 次 → mastered
"""
from weizhi.core import db
from conftest import make_card


def _save(tmp_db, **over):
    card = make_card(**over)
    assert db.save_card(card)
    return card["source_url"]


def test_schedule_review_nonexistent(tmp_db):
    assert db.schedule_review("custom:nope:0", 1) is None


def test_sm2_interval_sequence(tmp_db):
    src = _save(tmp_db)
    s = db.schedule_review(src, 1)
    assert s["interval_days"] == 1  # 首次记得 → 1 天
    s = db.schedule_review(src, 1)
    assert s["interval_days"] == 3
    s = db.schedule_review(src, 1)
    assert s["interval_days"] == 7
    s = db.schedule_review(src, 1)
    assert s["interval_days"] == 15
    s = db.schedule_review(src, 1)
    assert s["interval_days"] == 30


def test_forget_resets_interval_and_count(tmp_db):
    src = _save(tmp_db)
    for _ in range(3):
        db.schedule_review(src, 1)
    s = db.schedule_review(src, 0)  # 第 4 次复习忘记
    assert s["interval_days"] == 1
    assert s["review_count"] == 0


def test_ease_upper_bound(tmp_db):
    src = _save(tmp_db)
    s = db.schedule_review(src, 1)
    assert s["ease"] == 2.5  # 已在上限，不超


def test_ease_lower_bound(tmp_db):
    src = _save(tmp_db)
    for _ in range(20):
        s = db.schedule_review(src, 0)
    assert s["ease"] == 1.3  # 下限截断


def test_mastered_after_five(tmp_db):
    src = _save(tmp_db)
    for _ in range(5):
        s = db.schedule_review(src, 1)
    assert s["memory_state"] == "mastered"
    assert s["review_count"] == 5


def test_not_mastered_after_forget(tmp_db):
    src = _save(tmp_db)
    for _ in range(4):
        db.schedule_review(src, 1)
    s = db.schedule_review(src, 0)
    assert s["memory_state"] == "learning"
