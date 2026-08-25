"""管家 LLM 输出白名单校验测试（daily_agent.validate_decision）。

核心 trade-off：LLM 输出不可信 → 白名单约束（只修待拍板内的卡、通知类型/长度白名单、
荐食只能从今日新卡里挑），保证「半自主」不出界。
"""
import db
import daily_agent as da
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
    monkeypatch.setattr(da, "today_picks", lambda: [])
    decision = {
        "notifications": [
            {"type": "daily_summary", "title": "今日状态", "body": "一切正常", "level": "info"},
            {"type": "evil_type", "title": "非法类型", "body": "x", "level": "action"},
            {"title": "缺类型", "body": "x", "level": "info"},
            {"type": "daily_summary", "title": "", "body": "空标题", "level": "info"},
        ],
        "summary": "s",
    }
    out = da.validate_decision(decision, _signals(tmp_db, []))
    types = [n["type"] for n in out["notifications"]]
    assert types == ["daily_summary"]  # 非法类型/缺类型/空标题全被滤


def test_level_fallback_to_info(tmp_db, monkeypatch):
    monkeypatch.setattr(da, "today_picks", lambda: [])
    decision = {"notifications": [{"type": "daily_summary", "title": "t", "body": "b", "level": "critical"}],
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
