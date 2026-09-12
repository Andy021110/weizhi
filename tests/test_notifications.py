# -*- coding: utf-8 -*-
"""CP23 测试：通知策略（只允许三类）+ CP27 通知范围（默认只看今天）。

范围决策要求通知精简到：到期复习 / 需要用户审核 / 系统任务失败且无法自动恢复，
其余七类取消。这一层是唯一的口子，所以测试也集中在这里。

CP27 补的是「看哪一段」：历史通知攒着不清会把今天该做的事淹掉，
所以默认只看今天，历史留一个入口。
"""
from datetime import datetime

import pytest

import daily_agent
import db
import notifications as N
import reader


def _types_stored():
    return [n["type"] for n in db.list_notifications(limit=200)]


# ---------- 白名单本身 ----------

def test_exactly_three_allowed():
    assert set(N.KEEP) == {"review_due", "badcase_pending", "system_failure"}


def test_retired_types_are_recorded_with_reasons():
    """停发的类型必须留下名字和理由——它们不是"忘了"，是决定不发。"""
    for t in ("daily_summary", "daily_picks", "streak_warn", "stale_warn",
              "weak_review", "action_log", "weekly_report"):
        assert t in N.STOPPED, "%s 没有记录停发理由" % t
        assert N.STOPPED[t].strip(), "%s 的理由是空的" % t


def test_allowed_and_stopped_do_not_overlap():
    assert not (set(N.KEEP) & set(N.STOPPED))


def test_whitelist_is_the_same_object_everywhere():
    """daily_agent 与 reader 必须用同一份白名单，否则两处各写一份迟早漂移。"""
    assert daily_agent.notifications.KEEP is N.KEEP
    assert reader.notifications.KEEP is N.KEEP


# ---------- emit ----------

def test_emit_stores_allowed_type(tmp_db):
    assert N.emit("review_due", "该复习了", "有 3 张到期") is True
    assert _types_stored() == ["review_due"]


@pytest.mark.parametrize("ntype", ["daily_summary", "daily_picks", "streak_warn",
                                   "stale_warn", "weak_review", "action_log",
                                   "weekly_report"])
def test_emit_drops_retired_types(tmp_db, ntype):
    N.reset_suppressed()
    assert N.emit(ntype, "t", "b") is False
    assert _types_stored() == [], "%s 不该被写进库" % ntype
    assert N.suppressed().get(ntype) == 1


def test_emit_drops_unknown_types_without_raising(tmp_db):
    """模型有时会自己编一个 type 出来。拦下它，但别让整个任务挂掉。"""
    assert N.emit("完全没听说过的类型", "t", "b") is False
    assert _types_stored() == []


def test_emit_normalizes_bad_level(tmp_db):
    N.emit("review_due", "t", "b", level="critical")
    assert db.list_notifications()[0]["level"] == "info"


def test_reason_stopped_explains_both_cases():
    assert "打卡" in N.reason_stopped("streak_warn")
    assert "不在允许的三类里" in N.reason_stopped("something_new")


# ---------- 保留三类的语义 ----------

def test_failure_only_carries_the_last_error_line(tmp_db):
    """报错要具体到能下手，但不能把整个堆栈塞进通知。"""
    N.failure("每日质检巡检", RuntimeError("第一行\n第二行\n最后一行：连接失败"))
    note = db.list_notifications()[0]
    assert note["type"] == "system_failure"
    assert "每日质检巡检" in note["title"]
    assert "最后一行：连接失败" in note["body"]
    assert "第一行" not in note["body"]


def test_failure_handles_empty_error(tmp_db):
    assert N.failure("某阶段", None) is True
    assert "无错误信息" in db.list_notifications()[0]["body"]


def test_review_due_and_pending_helpers(tmp_db):
    assert N.review_due(5) is True
    assert N.pending_review(2, "甲、乙") is True
    notes = {n["type"]: n for n in db.list_notifications()}
    assert set(notes) == {"review_due", "badcase_pending"}
    assert "5" in notes["review_due"]["title"]
    assert notes["badcase_pending"]["level"] == "action"


# ---------- 读取侧同口径 ----------

def test_read_side_filters_old_rows(tmp_db):
    """库里已经躺着的历史通知也要按新规则过滤——否则旧噪音会一直显示。"""
    db.add_notification("daily_summary", "老的每日摘要", "噪音")
    db.add_notification("review_due", "该复习了", "3 张")
    kinds = sorted(N.KEEP)
    shown = db.list_notifications(types=kinds)
    assert [n["type"] for n in shown] == ["review_due"]
    assert db.count_unread_notifications(types=kinds) == 1
    # 但原始数据还在（没有删用户的历史）
    assert len(db.list_notifications()) == 2


def test_unread_count_matches_list(tmp_db):
    """徽标与列表必须同口径，否则点进去看到的是空的。"""
    db.add_notification("streak_warn", "断签了", "回来吧")
    db.add_notification("review_due", "该复习了", "3 张")
    kinds = sorted(N.KEEP)
    listed = db.list_notifications(unread_only=True, types=kinds)
    assert db.count_unread_notifications(types=kinds) == len(listed) == 1


# ---------- 累计学习天数取代连续打卡 ----------

def test_study_days_counts_distinct_dates(tmp_db):
    for i, d in enumerate(("2026-09-01", "2026-09-01", "2026-09-03", "2026-09-09")):
        db.save_card({"source_url": "u%d" % i, "title": "c%d" % i}, date=d)
        db.mark_done(d, "u%d" % i)
    assert db.study_days() == 3, "同一天多张只算一天"


def test_streak_is_gone_from_signals():
    """断签信号已移除：它唯一的用途是被取消的 streak_warn。

    只看**代码**不看注释——注释里提到 streak_warn 是为了说明为什么删掉它。
    """
    import inspect
    src = inspect.getsource(daily_agent.rule_signals)
    code = "\n".join(l for l in src.split("\n") if not l.strip().startswith("#"))
    assert "streak" not in code, "signal 里还有 streak"
    assert "decline" not in code, "学习量下滑信号还在"


def test_metrics_report_study_days_not_streak(tmp_db):
    import daily_check
    m = daily_check.usage_metrics()
    assert "study_days" in m
    assert "streak" not in m


# ---------- CP27 通知范围：默认只看今天 ----------

def _today():
    return datetime.now().strftime("%Y-%m-%d")


def test_scope_defaults_to_today():
    scope, since = reader.notification_scope({})
    assert scope == "today" and since == _today()


def test_scope_all_has_no_date_floor():
    assert reader.notification_scope({"scope": ["all"]}) == ("all", None)


@pytest.mark.parametrize("bad", ["al", "ALL", "", "yesterday"])
def test_scope_treats_unknown_value_as_today(bad):
    """拼错/大小写不对的参数不能静默变成「全部」——那又把历史糊回脸上。"""
    assert reader.notification_scope({"scope": [bad]})[0] == "today"


def test_list_notifications_since_date_excludes_history(tmp_db):
    N.emit("review_due", "历史一", "正文", level="warn", date="2026-01-01")
    N.emit("review_due", "历史二", "正文", level="warn", date="2026-01-02")
    N.emit("review_due", "今天的", "正文", level="warn")

    assert [n["title"] for n in db.list_notifications(since_date=_today())] == ["今天的"]
    # 不传就是全部 —— 历史没被删，只是不再默认展示
    assert len(db.list_notifications()) == 3
    assert len(db.list_notifications(since_date=None)) == 3


def test_unread_count_shares_scope_with_list(tmp_db):
    """口径必须一致，否则徽标显示 3 条未读、点进去一条都没有。"""
    for i in range(3):
        N.emit("review_due", "历史%d" % i, "正文", level="warn", date="2026-01-0%d" % (i + 1))
    N.emit("review_due", "今天的", "正文", level="warn")

    assert db.count_unread_notifications(since_date=_today()) == 1
    assert db.count_unread_notifications() == 4
    listed = db.list_notifications(unread_only=True, since_date=_today())
    assert len(listed) == db.count_unread_notifications(since_date=_today())


def test_today_scope_is_empty_when_only_history(tmp_db):
    """只有历史时，今天这一栏要如实为空，而不是把旧的顶上来。"""
    N.emit("review_due", "历史", "正文", level="warn", date="2026-01-01")
    assert db.list_notifications(since_date=_today()) == []
    assert db.count_unread_notifications(since_date=_today()) == 0
    assert len(db.list_notifications(since_date=None)) == 1


def test_scope_does_not_bypass_type_whitelist(tmp_db):
    """范围过滤和类型白名单是两道独立的门，不能互相绕过。"""
    N.emit("review_due", "允许的", "正文", level="warn")
    N.emit("streak_warn", "已停发的类型", "正文", level="warn")   # 被白名单拦下
    rows = db.list_notifications(types=sorted(N.KEEP), since_date=_today())
    assert [n["type"] for n in rows] == ["review_due"]


def test_read_all_scoped_to_visible_ids(tmp_db):
    """「全部标为已读」只标当前看到的那一段，否则回头翻历史全是已读。"""
    N.emit("review_due", "历史", "正文", level="warn", date="2026-01-01")
    N.emit("review_due", "今天的", "正文", level="warn")

    visible = db.list_notifications(since_date=_today())
    db.mark_notifications_read([n["id"] for n in visible])

    assert db.count_unread_notifications(since_date=_today()) == 0
    assert db.count_unread_notifications(since_date=None) == 1   # 历史仍是未读
