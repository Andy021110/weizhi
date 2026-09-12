"""影子模式测试：影子卡必须可见但对推送、质检、自动修复全部不可见。"""

import db
import daily_check


def _shadow_card(**over):
    card = {
        "title": "影子卡", "source_url": "https://x/1", "body": "正文" * 400,
        "summary": "摘要", "core_points": ["a"],
        "quiz": [{"question": "q", "options": ["A", "B"], "answer": 0}],
        "review_quiz": [], "think_question": "t",
        "think_answer": "答" * 200, "open_question": {"question": "q"},
        "template": "t2_reading",
        "_meta": {"template": "t2_reading", "origin": "v2-bridge", "shadow": True},
        "_bridge": {"origin": "v2", "shadow": True, "draft_id": 1},
    }
    card.update(over)
    return card


def test_normal_card_is_not_shadow():
    assert db.is_shadow_card({"title": "x", "_meta": {"template": "t2_reading"}}) is False
    assert db.is_shadow_card(None) is False
    assert db.is_shadow_card("不是字典") is False


def test_shadow_detected_from_both_places():
    """判据只实现一次，但两个位置任一为真都要认出来——漏一处就会漏进推送。"""
    assert db.is_shadow_card({"_meta": {"shadow": True}}) is True
    assert db.is_shadow_card({"_bridge": {"shadow": True}}) is True
    assert db.is_shadow_card({"_meta": {}, "_bridge": {}}) is False


def test_shadow_card_is_saved_and_readable(tmp_db):
    """可见是影子模式的前提——否则用户没法评审。"""
    assert db.save_card(_shadow_card(), date="2026-09-11") is True
    cards = db.load_cards("2026-09-11")
    assert len(cards) == 1
    assert db.is_shadow_card(cards[0]) is True


def test_shadow_card_excluded_from_quality_check(tmp_db):
    """质检与自动修复必须跳过影子卡：要评估的就是 v2 的原始产出，
    让 v1 的修复流水线改它，等于把评估对象先改了一遍。"""
    db.save_card(_shadow_card(), date="2026-09-11")
    db.save_card({"title": "正常卡", "source_url": "https://x/2", "template": "t2_reading",
                  "_meta": {"template": "t2_reading"}}, date="2026-09-11")
    picked = daily_check.pick_candidates(limit=20, check_all=True)
    titles = [c.get("title") for c in picked]
    assert "正常卡" in titles
    assert "影子卡" not in titles


def test_shadow_card_excluded_from_today_picks(tmp_db):
    """荐读是用户每天真正看到的东西，影子卡不能挤掉正片。"""
    import daily_agent
    from datetime import datetime
    db.save_card(_shadow_card(), date=datetime.now().strftime("%Y-%m-%d"))
    db.save_card({"title": "正常卡", "source_url": "https://x/3", "template": "t2_reading",
                  "_meta": {"template": "t2_reading"}},
                 date=datetime.now().strftime("%Y-%m-%d"))
    urls = {p["source_url"] for p in daily_agent.today_picks()}
    assert "https://x/3" in urls
    assert "https://x/1" not in urls


def test_bridge_marks_shadow_flag():
    import bridge_v1
    draft = {"title": "t", "lead": "l", "objective": "o",
             "explanation": [{"text": "解释", "cites": [0]}],
             "examples": [{"text": "例子", "cites": [0]}],
             "boundaries": [], "key_points": ["k"], "transfer_task": "任务"}
    items = [{"question": "q", "options": ["A", "B"], "answer": 0,
              "layer": "immediate", "explanation": "e", "error_reason": "r"}]
    shadow = bridge_v1.to_v1_card(draft, items, {"url": "https://y/1"}, shadow=True)
    normal = bridge_v1.to_v1_card(draft, items, {"url": "https://y/1"}, shadow=False)
    assert db.is_shadow_card(shadow) is True
    assert db.is_shadow_card(normal) is False


# ---------- 隔离标记必须真的落进库 ----------

def test_meta_markers_survive_roundtrip(tmp_db):
    """回归：`_meta` 曾被列在 save_card 的 fixed 集合里，落库时整块丢弃。

    `is_shadow_card` 查 `_meta.shadow` 与 `_bridge.shadow` 两处是纵深防御，
    但前者从来没写进库——等于一条防线从未通电。这里用一张**只有 _meta
    标记**的卡来测，否则 _bridge 会把问题遮住。
    """
    db.save_card({
        "title": "只有 _meta 标记的卡", "source_url": "https://x/meta-only",
        "template": "t2_reading",
        "_meta": {"template": "t2_reading", "shadow": True, "origin": "v2-bridge"},
    }, date="2026-09-11")
    card = db.get_card("https://x/meta-only")
    assert card["_meta"].get("shadow") is True
    assert card["_meta"].get("origin") == "v2-bridge"
    assert db.is_shadow_card(card) is True


def test_row_to_card_keeps_display_meta_without_extra(tmp_db):
    """补列值的那一步不能把展示字段也搞丢（没有 extra 的老卡最容易踩）。"""
    db.save_card({"title": "T", "source_url": "https://x/plain", "category": "AI",
                  "template": "t2_reading"}, date="2026-09-11")
    card = db.get_card("https://x/plain")
    assert card["_meta"]["category"] == "AI"
    assert card["_meta"]["template"] == "t2_reading"
