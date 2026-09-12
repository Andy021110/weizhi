# -*- coding: utf-8 -*-
"""发现层测试：被「不推送」拿掉的能力，必须有地方能看到。

范围决策把两样东西从推送里拿了下来——Top 3 荐读、以及 v2 的原始产出。
拿掉推送是对的（内容发现该由用户主动去逛），但如果只写进数据库而
不给出入口，能力就是事实上消失了。发现层是这两样的唯一出口。
"""
import json
import sys
import types

import db
import reader
from conftest import make_card


def _shadow_card(url="v2:shadow:1", **over):
    """一张影子卡：v2 产出、可见，但不参与推送/质检/自动修复。"""
    card = make_card(
        source_url=url,
        title="v2 实验卡：Agent Harness 的四个阶段",
        summary="把循环拆成四段，才能判断一次失败卡在哪一步。",
        _date="2026-09-11",
        favorite=0,
        _meta={"template": "t2_reading", "origin": "v2-bridge", "shadow": True},
        _bridge={"origin": "v2", "shadow": True, "draft_id": 1},
        figures=[{"kind": "flow", "svg": "<svg viewBox='0 0 10 10'></svg>"}],
    )
    card.update(over)
    return card


# ---------- 影子卡的取数 ----------

def test_shadow_cards_returns_only_shadow(tmp_db):
    db.save_card(make_card(source_url="custom:normal:1", title="正常卡"),
                 date="2026-09-12")
    db.save_card(_shadow_card(), date="2026-09-11")
    titles = [c["title"] for c in db.shadow_cards(limit=5)]
    assert "v2 实验卡：Agent Harness 的四个阶段" in titles
    assert "正常卡" not in titles


def test_shadow_cards_is_latest_first(tmp_db):
    db.save_card(_shadow_card(url="v2:a", title="早"), date="2026-09-10")
    db.save_card(_shadow_card(url="v2:b", title="晚"), date="2026-09-11")
    got = db.shadow_cards(limit=5)
    assert [c["title"] for c in got] == ["晚", "早"]


def test_shadow_cards_respects_limit(tmp_db):
    for i in range(4):
        db.save_card(_shadow_card(url="v2:%d" % i, title="卡%d" % i), date="2026-09-11")
    assert len(db.shadow_cards(limit=2)) == 2
    assert db.shadow_cards(limit=0) == []


def test_shadow_cards_not_fooled_by_false_flag(tmp_db):
    """`"shadow": false` 不能被当命中。

    所以这里按 id 扫描后用 is_shadow_card 过滤，而不是在 SQL 里对 extra
    做 LIKE——字符串匹配分不清布尔值和它的字符串形式。
    """
    db.save_card(make_card(
        source_url="custom:false:1", title="只是普通卡",
        _bridge={"origin": "v2", "shadow": False}), date="2026-09-12")
    assert db.shadow_cards(limit=5) == []


# ---------- 发现层聚合 ----------

def test_build_discover_empty(tmp_db):
    assert reader.build_discover() == {"picks": [], "lab": []}


def test_build_discover_reads_daily_picks(tmp_db):
    picks = [{"source_url": "https://x/1", "title": "第一篇", "why": "值得一读"}]
    db.set_user_state("daily_picks", json.dumps(picks, ensure_ascii=False))
    out = reader.build_discover()
    assert out["picks"] == [{"source_url": "https://x/1", "title": "第一篇",
                             "why": "值得一读", "from": "agent"}]


def test_build_discover_falls_back_to_today_cards(tmp_db):
    """Agent 什么都没挑时，发现层不能空着一块——退化成如实列出今日新卡。

    这不是硬凑推荐：推荐语换成卡片摘要，`from` 标出这是「今天有什么」
    而不是「Agent 挑的」。
    """
    from datetime import datetime
    db.save_card(make_card(source_url="custom:t:1", title="今天的新卡",
                           summary="讲了什么"), date=datetime.now().strftime("%Y-%m-%d"))
    picks = reader.build_discover()["picks"]
    assert len(picks) == 1
    assert picks[0]["title"] == "今天的新卡"
    assert picks[0]["from"] == "today"


def test_build_discover_fallback_excludes_shadow(tmp_db):
    """兜底也不能把 v2 影子卡混进来——影子卡的隔离是全局的，不因入口而变。"""
    from datetime import datetime
    today = datetime.now().strftime("%Y-%m-%d")
    db.save_card(_shadow_card(), date=today)
    assert reader.build_discover()["picks"] == []
    assert len(reader.build_discover()["lab"]) == 1


def test_build_discover_prefers_agent_picks_over_fallback(tmp_db):
    """Agent 挑过就用 Agent 的，不能又叠一层今日新卡。"""
    from datetime import datetime
    db.save_card(make_card(source_url="custom:t:2", title="今天的新卡"),
                 date=datetime.now().strftime("%Y-%m-%d"))
    db.set_user_state("daily_picks", json.dumps(
        [{"source_url": "https://x/9", "title": "挑出来的", "why": "w"}], ensure_ascii=False))
    picks = reader.build_discover()["picks"]
    assert [p["title"] for p in picks] == ["挑出来的"]
    assert picks[0]["from"] == "agent"


def test_build_discover_tolerates_broken_picks(tmp_db):
    """坏数据不能让整个发现层打不开——它只是少一块。"""
    db.set_user_state("daily_picks", "{不是 JSON")
    assert reader.build_discover()["picks"] == []

    db.set_user_state("daily_picks", json.dumps([
        "字符串不是对象", {"title": "没有 url"}, {"source_url": "https://x/2", "why": "ok"}]))
    got = reader.build_discover()["picks"]
    assert len(got) == 1 and got[0]["source_url"] == "https://x/2"


def test_build_discover_limits_picks(tmp_db):
    picks = [{"source_url": "https://x/%d" % i, "title": "t%d" % i, "why": "w"}
             for i in range(8)]
    db.set_user_state("daily_picks", json.dumps(picks, ensure_ascii=False))
    assert len(reader.build_discover(limit=3)["picks"]) == 3


def test_build_discover_lab_exposes_v2_cards(tmp_db):
    db.save_card(_shadow_card(), date="2026-09-11")
    lab = reader.build_discover()["lab"]
    assert len(lab) == 1
    assert lab[0]["date"] == "2026-09-11"
    assert lab[0]["figures"] == 1        # 配图数要露出来，评审才知道有没有图
    assert lab[0]["source_url"] == "v2:shadow:1"


# ---------- 单卡详情：少给一个字段不会报错，只会少一块界面 ----------

def test_card_detail_includes_figures():
    """回归：配图在列表里点开就消失，就是因为白名单漏了 figures。"""
    card = _shadow_card()
    out = reader.card_detail(card)
    assert out["figures"] == card["figures"]
    assert out["favorite"] == card["favorite"]
    assert out["_date"] == "2026-09-11"


def test_card_detail_drops_internal_fields():
    card = _shadow_card(_gen_input={"prompt": "内部提示词"})
    blob = json.dumps(reader.card_detail(card), ensure_ascii=False)
    assert "_gen_input" not in blob and "内部提示词" not in blob


def test_card_detail_on_missing_card():
    assert reader.card_detail(None) is None


# ---------- 影子生产的选材轮转 ----------

def test_already_made_detects_bridged_card(tmp_db):
    """判据必须按 URL 前缀查。

    入库键是 `<url>#v2:<sha1(url|卡片标题)>`，哈希里混的是**生成之后的**
    标题；而选材时手上只有 RSS 的原始标题。用标题算键看着更精确，
    实际永远对不上——这个 bug 真的发生过，表现是每天照撞「重复卡」。
    """
    import v2_shadow
    db.save_card(make_card(source_url="https://x/a#v2:abcdef123456",
                           title="生成出来的标题"), date="2026-09-11")
    assert v2_shadow.already_made("RSS 上的原标题", "https://x/a") is True
    assert v2_shadow.already_made("RSS 上的原标题", "https://x/b") is False
    assert v2_shadow.already_made("x", "") is False


def test_already_made_ignores_plain_v1_card(tmp_db):
    """v1 自己产的卡用裸 URL，不能因为「同一个来源」就判成已桥接。"""
    import v2_shadow
    db.save_card(make_card(source_url="https://x/a", title="v1 的卡"), date="2026-09-11")
    assert v2_shadow.already_made("x", "https://x/a") is False


def test_already_made_escapes_like_wildcards(tmp_db):
    """URL 里的 % 必须转义，否则一篇带 % 的 url 会把邻近的都判成已出卡。"""
    import v2_shadow
    db.save_card(make_card(source_url="https://x/100%off#v2:aa", title="t"),
                 date="2026-09-11")
    assert v2_shadow.already_made("t", "https://x/100%off") is True
    assert v2_shadow.already_made("t", "https://x/1000off") is False



def _fake_sources(monkeypatch, entries):
    """把 feedparser / trafilatura 换成假实现，避免测试联网。"""
    class _Entry:
        def __init__(self, title, link):
            self.title, self.link = title, link

    class _Feed:
        def __init__(self, es):
            self.entries = es

    monkeypatch.setitem(sys.modules, "feedparser", types.SimpleNamespace(
        parse=lambda rss: _Feed([_Entry(t, u) for t, u in entries])))
    monkeypatch.setitem(sys.modules, "trafilatura", types.SimpleNamespace(
        fetch_url=lambda u: b"<html></html>",
        extract=lambda raw, include_comments=False: "正文内容" * 400))


def test_fetch_materials_skips_already_made(tmp_db, monkeypatch):
    """回归：RSS 置顶几天不变，影子生产不能第二天就撞「重复卡」停摆。

    真实现状：每天取 RSS 第一篇 → 第二天下架不了 → v1 门禁判「重复卡」→
    影子链路只成功过一次，之后再没产出过可评审的 v2 卡。
    """
    import v2_shadow
    db.save_card(make_card(source_url="https://x/old#v2:deadbeef1234",
                           title="生成后的标题"), date="2026-09-11")

    _fake_sources(monkeypatch, [("老文章", "https://x/old"), ("新文章", "https://x/new")])
    out = v2_shadow.fetch_materials({"sources": [{"name": "测试源", "rss": "https://x/feed"}]},
                                    want=1)
    assert [m["url"] for m in out] == ["https://x/new"]


def test_fetch_materials_takes_first_when_all_new(tmp_db, monkeypatch):
    """没出过卡时仍然取最新一篇——跳过逻辑不能把正常路径也改了。"""
    import v2_shadow
    _fake_sources(monkeypatch, [("第一篇", "https://x/1"), ("第二篇", "https://x/2")])
    out = v2_shadow.fetch_materials({"sources": [{"name": "测试源", "rss": "https://x/feed"}]},
                                    want=1)
    assert [m["url"] for m in out] == ["https://x/1"]
