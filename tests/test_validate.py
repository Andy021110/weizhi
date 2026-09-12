"""管家 LLM 输出白名单校验测试（daily_agent.validate_decision）。

核心 trade-off：LLM 输出不可信 → 白名单约束（只修待拍板内的卡、通知类型/长度白名单、
荐食只能从今日新卡里挑），保证「半自主」不出界。
"""
from weizhi.core import db
from weizhi.ops import daily_agent as da
from conftest import make_card


def _signals(tmp_db, pending_urls):
    """构造信号：pending 列表 + 在库里造对应卡（regen 校验需要 db.get_card 命中）。"""
    for i, u in enumerate(pending_urls):
        db.save_card(make_card(source_url=u, title="待拍板卡%d" % i))
    return {"pending": [{"source_url": u} for u in pending_urls]}


def test_non_dict_returns_none(tmp_db):
    assert da.validate_decision("not-a-dict", _signals(tmp_db, [])) is None
    assert da.validate_decision(None, _signals(tmp_db, [])) is None


def test_regen_only_from_pending(tmp_db, monkeypatch):
    monkeypatch.setattr(da, "today_picks", lambda: [])
    signals = _signals(tmp_db, ["custom:pending:1"])
    decision = {"regen_candidates": ["custom:pending:1", "custom:evil:99", "not-a-url"],
                "notifications": [], "summary": "s"}
    out = da.validate_decision(decision, signals)
    assert out["regen_candidates"] == ["custom:pending:1"]  # 非待拍板/不存在全被滤掉


def test_regen_capped_at_three(tmp_db, monkeypatch):
    monkeypatch.setattr(da, "today_picks", lambda: [])
    urls = ["custom:pending:%d" % i for i in range(5)]
    signals = _signals(tmp_db, urls)
    decision = {"regen_candidates": urls, "notifications": [], "summary": "s"}
    out = da.validate_decision(decision, signals)
    assert len(out["regen_candidates"]) == 3


def test_notification_type_whitelist(tmp_db, monkeypatch):
    """白名单只放行 review_due（范围决策：通知精简到三类，
    其中 badcase_pending / system_failure 由系统生成，模型不许自己发）。"""
    monkeypatch.setattr(da, "today_picks", lambda: [])
    decision = {
        "notifications": [
            {"type": "review_due", "title": "该复习了", "body": "有 5 张到期", "level": "warn"},
            {"type": "evil_type", "title": "非法类型", "body": "x", "level": "action"},
            {"title": "缺类型", "body": "x", "level": "info"},
            {"type": "review_due", "title": "", "body": "空标题", "level": "warn"},
        ],
        "summary": "s",
    }
    out = da.validate_decision(decision, _signals(tmp_db, []))
    types = [n["type"] for n in out["notifications"]]
    assert types == ["review_due"]  # 非法类型/缺类型/空标题全被滤


def test_retired_notification_types_are_filtered(tmp_db, monkeypatch):
    """**被停发的类型必须被拦下**——哪怕模型照着旧提示词编出来。

    这条防的是回潮：范围决策取消了每日摘要、荐读推送、断签提醒、
    过时卡提醒、周报，模型不该能把它们塞回来。"""
    monkeypatch.setattr(da, "today_picks", lambda: [])
    retired = ["daily_summary", "daily_picks", "streak_warn", "stale_warn",
               "weak_review", "action_log", "weekly_report"]
    decision = {
        "notifications": [{"type": t, "title": "t", "body": "b", "level": "info"}
                          for t in retired],
        "summary": "s",
    }
    out = da.validate_decision(decision, _signals(tmp_db, []))
    assert out["notifications"] == [], "被停发的类型不该通过校验"


def test_level_fallback_to_info(tmp_db, monkeypatch):
    monkeypatch.setattr(da, "today_picks", lambda: [])
    decision = {"notifications": [{"type": "review_due", "title": "t", "body": "b", "level": "critical"}],
                "summary": "s"}
    out = da.validate_decision(decision, _signals(tmp_db, []))
    assert out["notifications"][0]["level"] == "info"  # 非法 level 兜底 info


def test_recommendations_only_from_today_picks(tmp_db, monkeypatch):
    monkeypatch.setattr(da, "today_picks",
                        lambda: [{"source_url": "custom:pick:1"}, {"source_url": "custom:pick:2"}])
    decision = {
        "recommendations": [
            {"source_url": "custom:pick:1", "title": "好卡", "why": "值得读"},
            {"source_url": "custom:pick:2", "title": "好卡2", "why": "也值得读"},
            {"source_url": "custom:old:9", "title": "旧卡", "why": "不在今日候选"},
            {"source_url": "custom:pick:1", "title": "缺 why"},
        ],
        "summary": "s",
    }
    out = da.validate_decision(decision, _signals(tmp_db, []))
    assert len(out["recommendations"]) == 2
    assert all(r["source_url"] in ("custom:pick:1", "custom:pick:2") for r in out["recommendations"])


def test_summary_truncated(tmp_db, monkeypatch):
    monkeypatch.setattr(da, "today_picks", lambda: [])
    decision = {"notifications": [], "summary": "长" * 100}
    out = da.validate_decision(decision, _signals(tmp_db, []))
    assert len(out["summary"]) == 40
