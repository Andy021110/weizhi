"""CP13 测试：资讯适配。跨源去重、来源优先级、冲突并列、时效与不生产。"""
from datetime import datetime, timedelta

from weizhi.produce import news

NOW = datetime(2026, 9, 11, 12, 0, 0)


def _ev(**over):
    base = {
        "title": "Granite 时间序列基础模型发布",
        "url": "https://example.com/a",
        "source": "官方博客",
        "source_tier": "official",
        "published_at": "2026-09-08",
        "summary": "参数量约 385M，上下文长度 8192。",
        "kind": "fast",
    }
    base.update(over)
    return base


# ---------- 规范键 ----------

def test_same_event_from_different_urls_shares_key():
    """用 URL 做键等于没去重——每条来源的 URL 天然不同。"""
    a = _ev(url="https://x.com/1")
    b = _ev(url="https://y.com/2", source="量子位", source_tier="media")
    assert news.canonical_key(a) == news.canonical_key(b)


def test_title_noise_does_not_split_events():
    """「| 选自 …」「编译」这类转载标记不该把同一事件算成两条。"""
    a = _ev(title="高效电商 Agent 解剖")
    b = _ev(title="高效电商 Agent 解剖 | 选自 Anthropic 博客", url="https://z.com/3")
    assert news.canonical_key(a) == news.canonical_key(b)


def test_different_events_have_different_keys():
    assert news.canonical_key(_ev()) != news.canonical_key(_ev(title="另一件完全不同的事情"))


# ---------- 跨源去重与来源优先级 ----------

def test_merge_dedupes_and_ranks_sources():
    events = [
        _ev(source="某自媒体", source_tier="social", url="https://s.com/1"),
        _ev(source="官方博客", source_tier="official", url="https://o.com/1"),
    ]
    out = news.merge(events)
    assert len(out) == 1, "同一事件应合并成一条"
    assert out[0]["source_count"] == 2
    assert out[0]["primary_source"]["name"] == "官方博客", "一手来源优先"
    assert out[0]["traceable_to_primary"] is True


def test_secondhand_without_primary_is_flagged():
    out = news.merge([_ev(source="二手翻译", source_tier="blog")])
    assert out[0]["traceable_to_primary"] is False


def test_merge_keeps_all_sources():
    """冲突要并列展示，前提是来源都还在。"""
    out = news.merge([_ev(), _ev(source="量子位", source_tier="media", url="https://q.com/1")])
    assert len(out[0]["sources"]) == 2


# ---------- 冲突检测 ----------

def test_conflict_detected_when_numbers_differ():
    """方案 M5 / T5：两个来源对同一版本信息表述冲突时必须并列展示。"""
    a = _ev(summary="该模型参数量 385M，上下文 8192。")
    b = _ev(source="二手渠道", source_tier="blog", url="https://b.com/1",
            summary="该模型参数量 400M，上下文 8192。")
    out = news.merge([a, b])
    assert out[0]["conflicts"], "数字不一致必须被检出"
    assert any("400m" in f.lower() or "385m" in f.lower()
               for f in out[0]["conflicts"][0]["differing_facts"])


def test_no_conflict_when_facts_match():
    a = _ev(summary="参数量 385M。")
    b = _ev(source="量子位", source_tier="media", url="https://q.com/1", summary="参数量 385M。")
    assert news.merge([a, b])[0]["conflicts"] == []


def test_conflict_not_reported_for_single_source():
    assert news.merge([_ev()])[0]["conflicts"] == []


# ---------- 时效 ----------

def test_fast_content_expires():
    info = news.validity(_ev(kind="fast", published_at="2026-09-08"), now=NOW)
    assert info["valid_days"] == news.VALIDITY_DAYS["fast"]
    assert info["reverify_at"]
    assert info["expired"] is False

    old = news.validity(_ev(kind="fast", published_at="2026-08-01"), now=NOW)
    assert old["expired"] is True


def test_stable_content_has_no_expiry():
    """原理类内容不会因为时间失真，给它安失效期反而是噪音。"""
    info = news.validity(_ev(kind="stable", published_at="2020-01-01"), now=NOW)
    assert info["valid_days"] is None and info["expires_at"] is None and not info["expired"]


def test_needs_reverify_after_reverify_window():
    fresh = _ev(kind="fast", published_at="2026-09-10")
    stale = _ev(kind="fast", published_at="2026-09-01")
    assert news.needs_reverify(fresh, now=NOW) is False
    assert news.needs_reverify(stale, now=NOW) is True


def test_unknown_kind_falls_back_to_evolving():
    assert news.normalize(_ev(kind="胡说"))["kind"] == "evolving"


# ---------- 生产门禁 ----------

def test_expired_events_are_skipped():
    r = news.select_for_production([_ev(kind="fast", published_at="2026-01-01")], now=NOW)
    assert r["status"] == "skip_day"
    assert any("有效期" in s["why"] for s in r["skipped"])


def test_skip_day_is_a_valid_outcome():
    """方案 M5：没有高价值事件时允许当天不生产，不为不断更硬产。"""
    r = news.select_for_production([], now=NOW)
    assert r["status"] == "skip_day"
    assert "不生产" in r["reason"]


def test_relevance_gate_uses_goal():
    spec = {"key": "g", "capability": "能说清电商智能体的分层结构",
            "scene": "给团队定方案", "success_evidence": "能画出分层图",
            "milestones": [{"id": "m1", "name": "说清电商智能体分层", "evidence": "能画图"}]}
    claims_index = {
        "https://example.com/a": [
            {"claim_idx": 0, "kind": "definition",
             "text": "电商智能体在架构上分为接入层、编排层与工具层三个层次。"}],
    }
    r = news.select_for_production([_ev()], spec=spec, claims_index=claims_index, now=NOW)
    assert r["status"] == "ok"
    assert r["events"][0]["relevance"] > 0


def test_production_respects_limit():
    events = [_ev(title="事件 A"), _ev(title="事件 B", url="https://b"),
              _ev(title="事件 C", url="https://c")]
    r = news.select_for_production(events, limit=2, now=NOW)
    assert len(r["events"]) == 2


# ---------- 渲染 ----------

def test_brief_surfaces_conflicts_and_provenance():
    a = _ev(summary="参数量 385M。")
    b = _ev(source="二手渠道", source_tier="blog", url="https://b.com/1", summary="参数量 400M。")
    text = news.render_brief(news.merge([a, b]))
    assert "冲突" in text and "不许合并" in text
    assert "时效" in text


def test_brief_flags_secondhand():
    text = news.render_brief(news.merge([_ev(source="二手翻译", source_tier="blog")]))
    assert "二手内容" in text
