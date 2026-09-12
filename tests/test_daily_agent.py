# -*- coding: utf-8 -*-
"""管家（daily_agent）的荐读链路测试。

「为你挑的 Top 3」是策展能力的唯一出口。它长期是空的，而**没有任何信号**：
过滤是静默的，日志里也不打印。所以这里的用例既测正确性，也测「出事时看得见」。
"""
import json
from datetime import datetime

import db
import daily_agent
from conftest import make_card


def _today():
    return datetime.now().strftime("%Y-%m-%d")


def _save(source_url, title, summary="摘要", date=None):
    db.save_card(make_card(source_url=source_url, title=title, summary=summary),
                 date=date or _today())


def _signals(**over):
    s = {"pending": [], "review_due": {"due": 0, "compliance": None}}
    s.update(over)
    return s


# ---------- URL 归一化 ----------

def test_norm_url_strips_scheme_www_and_trailing_slash():
    base = "example.com/a/b"
    for u in ("https://example.com/a/b", "http://example.com/a/b",
              "https://www.example.com/a/b", "https://example.com/a/b/",
              "HTTPS://Example.com/a/b", "  https://example.com/a/b  "):
        assert daily_agent.norm_url(u) == base, u


def test_norm_url_keeps_distinct_paths_distinct():
    assert daily_agent.norm_url("https://x.com/a") != daily_agent.norm_url("https://x.com/b")
    assert daily_agent.norm_url("") == ""
    assert daily_agent.norm_url(None) == ""


# ---------- 荐读：模型抄错 URL 不该让推荐消失 ----------

def test_validate_decision_accepts_url_without_scheme(tmp_db):
    """回归：模型少写协议，逐字符比对会判「不在候选里」并静默丢掉。"""
    _save("https://x.com/article-1", "文章一")
    d = daily_agent.validate_decision(
        {"recommendations": [{"source_url": "x.com/article-1",
                              "title": "文章一", "why": "讲清了为什么"}]},
        _signals())
    assert [r["source_url"] for r in d["recommendations"]] == ["https://x.com/article-1"]


def test_validate_decision_stores_candidate_original_url(tmp_db):
    """落库必须是候选里的原值——卡片是按原值建索引的，归一化值查不到。"""
    _save("https://www.x.com/a/", "文章A")
    d = daily_agent.validate_decision(
        {"recommendations": [{"source_url": "www.x.com/a", "title": "抄错的", "why": "w"}]},
        _signals())
    assert d["recommendations"][0]["source_url"] == "https://www.x.com/a/"
    assert db.get_card(d["recommendations"][0]["source_url"]) is not None


def test_validate_decision_falls_back_to_card_summary_for_why(tmp_db):
    """模型漏写 why 时用卡片摘要兜底，而不是整条丢掉。"""
    _save("https://x.com/a2", "文章二", summary="卡片自己的摘要")
    d = daily_agent.validate_decision(
        {"recommendations": [{"source_url": "https://x.com/a2", "title": "文章二"}]},
        _signals())
    assert d["recommendations"][0]["why"] == "卡片自己的摘要"


def test_validate_decision_drops_url_not_in_candidates(tmp_db, capsys):
    _save("https://x.com/real", "在候选里")
    d = daily_agent.validate_decision(
        {"recommendations": [{"source_url": "https://x.com/nope", "why": "w"}]},
        _signals())
    assert d["recommendations"] == []
    # 静默是这次踩坑的根源：必须留下痕迹
    assert "荐读丢弃" in capsys.readouterr().out


def test_validate_decision_excludes_shadow_cards(tmp_db):
    """影子卡不参与荐读——隔离是全局的，不因入口而变。"""
    db.save_card(make_card(source_url="custom:shadow:1", title="影子",
                           _meta={"shadow": True}), date=_today())
    d = daily_agent.validate_decision(
        {"recommendations": [{"source_url": "custom:shadow:1", "why": "w"}]},
        _signals())
    assert d["recommendations"] == []


def test_validate_decision_caps_at_three(tmp_db):
    for i in range(5):
        _save("https://x.com/p%d" % i, "卡%d" % i)
    d = daily_agent.validate_decision(
        {"recommendations": [{"source_url": "https://x.com/p%d" % i, "why": "w"}
                             for i in range(5)]},
        _signals())
    assert len(d["recommendations"]) == 3


def test_validate_decision_tolerates_garbage(tmp_db):
    _save("https://x.com/g", "卡")
    assert daily_agent.validate_decision(
        {"recommendations": "不是数组"}, _signals())["recommendations"] == []
    assert daily_agent.validate_decision(
        {"recommendations": ["字符串", None, 42]}, _signals())["recommendations"] == []


# ---------- 落库：宁可显示「今天没有」，也不要展示过期推荐 ----------

def test_execute_overwrites_daily_picks_with_empty(tmp_db):
    """只写非空，会让「今天没挑出来」变成继续展示昨天那份，用户看不出过期。"""
    db.set_user_state("daily_picks", json.dumps(
        [{"source_url": "https://x.com/old", "title": "昨天的推荐", "why": "w"}],
        ensure_ascii=False))
    daily_agent.execute("", {"recommendations": [], "regen_candidates": [],
                             "notifications": []}, _signals(), "/tmp")
    assert json.loads(db.get_user_state("daily_picks")) == []


def test_execute_writes_picks_when_present(tmp_db):
    recs = [{"source_url": "https://x.com/a", "title": "A", "why": "值得读"}]
    daily_agent.execute("", {"recommendations": recs, "regen_candidates": [],
                             "notifications": []}, _signals(), "/tmp")
    assert json.loads(db.get_user_state("daily_picks")) == recs


def test_execute_prints_when_no_picks(tmp_db, capsys):
    daily_agent.execute("", {"recommendations": [], "regen_candidates": [],
                             "notifications": []}, _signals(), "/tmp")
    assert "今日无荐读" in capsys.readouterr().out


# ---------- 空推荐时的兜底路径仍然成立 ----------

def test_fallback_gives_today_cards_as_picks(tmp_db):
    """LLM 挂掉时的规则兜底也要给荐读——否则模型一挂发现层就空。"""
    _save("https://x.com/f1", "兜底卡")
    out = daily_agent.fallback({}, _signals())
    assert [r["source_url"] for r in out["recommendations"]] == ["https://x.com/f1"]
