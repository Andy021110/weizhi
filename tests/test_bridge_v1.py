"""CP16 测试：v2 → v1 桥接层。映射正确性、分层还原、冲突可见、不凑题。"""
import pytest

import bridge_v1
import db
from conftest import make_body, make_structure

MATERIAL = {"title": "Agent Harness 执行循环", "url": "https://example.com/h",
            "site": "官方博客", "kind": "evolving", "source_tier": "official"}

GOOD_FIG = {
    "kind": "flow",
    "proposition": "一次调用经过哪四步变成答案",
    "reading": "少任何一步循环都闭合不了",
    "caption": "循环的四个阶段",
    "alt": "四个方框按顺序排列，箭头依次相连",
    "plot": {"nodes": ["组装上下文", "模型调用", "工具执行", "状态回写"]},
}


def _draft():
    d = dict(make_body())
    d.update(make_structure())
    d["estimated_minutes"] = 7
    return d


def _items(immediate=2, later=1):
    make = lambda layer, i: {  # noqa: E731
        "id": "q%d" % i, "layer": layer, "objective": "能说清四个阶段",
        "cites": [0], "question": "循环包含哪四个阶段？",
        "options": ["组装、调用、执行、回写", "其他甲", "其他乙", "其他丙"],
        "answer": 0, "explanation": "证据里明确给出。",
        "error_reason": "容易和通用流水线混淆。",
    }
    out, i = [], 1
    for _ in range(immediate):
        out.append(make("immediate", i)); i += 1
    for _ in range(later):
        out.append(make("day7", i)); i += 1
    return out


def _supplement(**over):
    data = {
        "think_answer": "判断依据是这套循环假设工具调用是幂等的。" * 8,
        "open_question": {"question": "什么情况下不能直接套用这套循环？",
                          "reference_answer": "当工具涉及写操作时，" * 10,
                          "grading_points": ["指出写操作需要确认层", "说明幂等假设"]},
    }
    data.update(over)
    return data


# ---------- 映射 ----------

def test_core_fields_map():
    card = bridge_v1.to_v1_card(_draft(), _items(), MATERIAL, _supplement())
    d = _draft()
    assert card["title"] == d["title"]
    assert card["summary"] == d["lead"]
    assert card["core_points"] == d["key_points"]
    assert card["think_question"] == d["transfer_task"]
    assert card["think_answer"] == _supplement()["think_answer"]
    assert card["open_question"] == _supplement()["open_question"]
    assert card["source_url"] == MATERIAL["url"]
    assert card["template"] == bridge_v1.TEMPLATE
    assert card["_meta"]["origin"] == "v2-bridge"


def test_body_includes_explanation_examples_and_boundaries():
    card = bridge_v1.to_v1_card(_draft(), _items(), MATERIAL, _supplement())
    for block in _draft()["explanation"]:
        assert block["text"] in card["body"]
    for block in _draft()["examples"]:
        assert block["text"] in card["body"]
    # 边界要单独成段并带小标题，混在解释里会被读者略过
    assert "【适用边界】" in card["body"]
    for block in _draft()["boundaries"]:
        assert block["text"] in card["body"]


def test_split_items_restores_v1_two_sets():
    """范围决策把两套题库合并成分层题库，这里做的是反向兼容垫片。"""
    quiz, review = bridge_v1.split_items(_items(immediate=2, later=1))
    assert len(quiz) == 2 and len(review) == 1
    assert all(q["layer"] == "immediate" for q in quiz)
    assert review[0]["layer"] == "day7"


def test_layer_and_error_reason_survive_the_bridge():
    """分层与错误原因必须带过去，丢了就退不回新题库。"""
    card = bridge_v1.to_v1_card(_draft(), _items(), MATERIAL, _supplement())
    assert card["quiz"][0]["error_reason"]
    assert card["quiz"][0]["objective"]
    assert card["_bridge"]["layers"] == ["day7", "immediate"]


def test_difficulty_is_deterministic():
    short = dict(_draft(), explanation=[{"text": "短" * 300, "cites": [0]}],
                 examples=[], boundaries=[])
    assert bridge_v1._difficulty(short["explanation"][0]["text"]) == "入门"
    assert bridge_v1._difficulty("字" * 1000) == "中级"
    assert bridge_v1._difficulty("字" * 2000) == "进阶"


def test_credibility_maps_from_tier():
    official = bridge_v1.to_v1_card(_draft(), _items(), MATERIAL, _supplement())
    assert official["credibility"] == "官方"
    social = bridge_v1.to_v1_card(
        _draft(), _items(), dict(MATERIAL, source_tier="social"), _supplement())
    assert social["credibility"] == "自媒体"


def test_bridge_records_provenance():
    card = bridge_v1.to_v1_card(_draft(), _items(), MATERIAL, _supplement(),
                                figures=[GOOD_FIG], pack_id=7, draft_id=42)
    b = card["_bridge"]
    assert (b["pack_id"], b["draft_id"], b["figures"]) == (7, 42, 1)
    assert b["objective"]
    assert b["figure_notes"] == [], "可渲染的图不该产生跳过记录"


# ---------- 配图：渲染、注入、失败降级 ----------

def test_figures_are_rendered_to_svg_on_the_card():
    import xml.etree.ElementTree as ET
    card = bridge_v1.to_v1_card(_draft(), _items(), MATERIAL, _supplement(),
                                figures=[GOOD_FIG])
    figs = card["figures"]
    assert len(figs) == 1
    f = figs[0]
    assert f["svg"].startswith("<svg")
    ET.fromstring(f["svg"])              # 不合法会抛异常
    assert f["caption"] == GOOD_FIG["caption"]
    # 读图结论必须带过去：没有它，图就只是装饰
    assert f["reading"] == GOOD_FIG["reading"]
    assert f["alt"] == GOOD_FIG["alt"]


def test_figures_survive_db_round_trip(tmp_db):
    """图要能在 v1 前端读到——落库走 extra JSON，这条得实测。"""
    card = bridge_v1.to_v1_card(_draft(), _items(), MATERIAL, _supplement(),
                                figures=[GOOD_FIG])
    assert bridge_v1.save(card, date="2026-09-11") is True
    got = db.load_cards("2026-09-11")[0]
    assert len(got["figures"]) == 1
    assert got["figures"][0]["svg"].startswith("<svg")


def test_scene_figure_is_skipped_with_a_note():
    """scene 需要图片服务，不能本地编——跳过但要说清为什么。"""
    card = bridge_v1.to_v1_card(_draft(), _items(), MATERIAL, _supplement(),
                                figures=[dict(GOOD_FIG, kind="scene")])
    assert card["figures"] == []
    assert card["_bridge"]["figures"] == 0
    assert any("图片服务" in n["why"] for n in card["_bridge"]["figure_notes"])


def test_unrenderable_figure_does_not_break_the_card():
    """一张图渲染失败不能让整张卡作废——正文比图重要得多。"""
    broken = dict(GOOD_FIG, plot={"nodes": []})
    card = bridge_v1.to_v1_card(_draft(), _items(), MATERIAL, _supplement(),
                                figures=[broken, GOOD_FIG])
    assert card["title"] and card["body"], "卡片主体不受影响"
    assert len(card["figures"]) == 1, "好的那张要留下"
    assert card["_bridge"]["figure_notes"], "失败的那张要留下原因"


def test_safe_svg_rejects_dangerous_constructs():
    """纵深防御：渲染结果里混进脚本就别注入前端。"""
    assert bridge_v1._safe_svg("<svg><circle/></svg>")
    assert bridge_v1._safe_svg("<svg><script>alert(1)</script></svg>") is None
    assert bridge_v1._safe_svg("<svg onload=alert(1)></svg>") is None
    assert bridge_v1._safe_svg("<svg><foreignObject></foreignObject></svg>") is None


def test_figures_absent_is_fine():
    card = bridge_v1.to_v1_card(_draft(), _items(), MATERIAL, _supplement())
    assert card["figures"] == []
    assert card["_bridge"]["figure_notes"] == []


# ---------- 不凑题 + 冲突可见 ----------

def test_does_not_pad_questions():
    """范围决策废除了固定题量——桥接只做映射，绝不编题凑数。"""
    card = bridge_v1.to_v1_card(_draft(), _items(immediate=1, later=0), MATERIAL, _supplement())
    assert len(card["quiz"]) == 1, "不该为了过旧门禁编出多余题目"
    assert card["review_quiz"] == []


def test_compat_delegates_to_the_real_gate(tmp_db):
    """不许在桥接里复制一份阈值：两份阈值必然漂移，
    漂移的后果是「桥上看着能发、线上却被拦」。"""
    import daily_check
    card = bridge_v1.to_v1_card(_draft(), _items(immediate=1, later=0), MATERIAL, _supplement())
    assert bridge_v1.check_v1_compat(card) == daily_check.rule_check(card)


def test_scope_compliant_card_now_passes_the_gate(tmp_db):
    """固定题量放宽后，一张 1 道即时题的卡不再被拦。"""
    card = bridge_v1.to_v1_card(_draft(), _items(immediate=1, later=1), MATERIAL, _supplement())
    assert bridge_v1.check_v1_compat(card) == []


def test_dedupe_key_differs_by_title():
    a = bridge_v1.to_v1_card(_draft(), _items(), MATERIAL, _supplement())
    b = bridge_v1.to_v1_card(dict(_draft(), title="另一个标题"), _items(), MATERIAL, _supplement())
    assert bridge_v1.dedupe_key(a) != bridge_v1.dedupe_key(b)


# ---------- 补全校验 ----------

def test_good_supplement_passes():
    assert bridge_v1.validate_supplement(_supplement()) == []


@pytest.mark.parametrize("mutate,expect", [
    (lambda d: d.update(think_answer="太短"), "think_answer"),
    (lambda d: d.update(open_question="不是对象"), "open_question"),
    (lambda d: d.update(open_question={"question": "短"}), "question"),
    (lambda d: d.update(open_question={"question": "这是一个合格长度的简答题题干吗？",
                                       "reference_answer": "短",
                                       "grading_points": ["a", "b"]}), "reference_answer"),
    (lambda d: d.update(open_question={"question": "这是一个合格长度的简答题题干吗？",
                                       "reference_answer": "答" * 90,
                                       "grading_points": ["只有一条"]}), "grading_points"),
])
def test_supplement_validation(mutate, expect):
    sup = _supplement()
    mutate(sup)
    errs = bridge_v1.validate_supplement(sup)
    assert errs and any(expect in e for e in errs), errs


# ---------- 落库与发布门禁 ----------

def test_save_writes_readable_v1_card(tmp_db):
    card = bridge_v1.to_v1_card(_draft(), _items(), MATERIAL, _supplement())
    assert bridge_v1.save(card) is True
    loaded = db.load_cards()
    assert len(loaded) == 1
    got = loaded[0]
    assert got["title"] == card["title"]
    assert got["quiz"][0]["layer"] == "immediate", "前端读回的题目也要带分层"
    assert got["think_answer"]


def test_save_dedupes_same_source_and_title(tmp_db):
    card = bridge_v1.to_v1_card(_draft(), _items(), MATERIAL, _supplement())
    assert bridge_v1.save(card) is True
    again = bridge_v1.to_v1_card(_draft(), _items(), MATERIAL, _supplement())
    assert bridge_v1.save(again) is False, "同一来源+标题不该重复入库"


def test_publish_gate_blocks_and_explains(tmp_db):
    """范围决策：模型生成的内容不能直接发布，必须过门禁。"""
    card = bridge_v1.to_v1_card(_draft(), _items(immediate=1, later=0), MATERIAL,
                                _supplement(think_answer=""))
    ok, issues = bridge_v1.publish_gate(card)
    assert ok is False
    assert issues, "被拦下要给出原因，不能只返回 False"


def test_publish_gate_passes_a_healthy_card(tmp_db):
    card = bridge_v1.to_v1_card(_draft(), _items(immediate=1, later=1), MATERIAL, _supplement())
    ok, issues = bridge_v1.publish_gate(card)
    assert ok is True, issues


# ---------- 刷新已入库的卡 ----------

def test_stored_key_matches_what_save_wrote(tmp_db):
    """刷新靠 stored_key 找回行，它必须与 save 用的算法完全一致——
    不一致的后果是刷新静默什么都不做。"""
    card = bridge_v1.to_v1_card(_draft(), _items(), MATERIAL, _supplement(),
                                figures=[GOOD_FIG])
    bridge_v1.save(card, date="2026-09-11")
    saved_url = db.load_cards("2026-09-11")[0]["source_url"]
    assert bridge_v1.stored_key(MATERIAL["url"], card["title"]) == saved_url


def test_refresh_adds_figures_to_an_already_saved_card(tmp_db):
    """桥接规则改了以后，旧卡必须能被补齐——否则部署完看不到任何变化。"""
    import evidence
    sid, claims = evidence.ingest_source(
        MATERIAL["url"], "Agent Harness 的循环由四个阶段组成，状态回写让循环闭合。" * 6,
        title=MATERIAL["title"])
    did = db.save_v2_card_draft(
        input_hash="refresh-h", schema_version="1.0",
        payload={"title": _draft()["title"]}, source_id=sid, status="draft")

    # 先按「没有配图」入库一张卡
    card = bridge_v1.to_v1_card(_draft(), _items(), MATERIAL, _supplement())
    bridge_v1.save(card, date="2026-09-11")
    assert db.load_cards("2026-09-11")[0]["figures"] == []

    # 图上来了（草稿侧记录了配图）
    db.save_v2_draft_figures(did, [GOOD_FIG])

    result = bridge_v1.refresh()
    assert result["checked"] >= 1
    assert any(u["draft_id"] == did for u in result["updated"])

    got = db.load_cards("2026-09-11")[0]
    assert len(got["figures"]) == 1
    assert got["figures"][0]["svg"].startswith("<svg")
    assert got["_bridge"]["figures"] == 1


def test_refresh_preserves_untouched_fields(tmp_db):
    """刷新只动派生字段，不能顺手把正文或别的 extra 字段覆盖掉。"""
    import evidence
    sid, _ = evidence.ingest_source(MATERIAL["url"], "循环四阶段与状态回写。" * 8,
                                    title=MATERIAL["title"])
    did = db.save_v2_card_draft(
        input_hash="refresh-h2", schema_version="1.0",
        payload={"title": _draft()["title"]}, source_id=sid, status="draft")
    card = bridge_v1.to_v1_card(_draft(), _items(), MATERIAL, _supplement())
    bridge_v1.save(card, date="2026-09-11")
    before = db.load_cards("2026-09-11")[0]

    db.save_v2_draft_figures(did, [GOOD_FIG])
    bridge_v1.refresh()
    after = db.load_cards("2026-09-11")[0]

    assert after["body"] == before["body"]
    assert after["core_points"] == before["core_points"]
    assert after["think_answer"] == before["think_answer"]
    assert after["_bridge"]["layers"] == before["_bridge"]["layers"], \
        "一层深合并要保住 _bridge 里的其它键"


def test_refresh_reports_missing_card_instead_of_silence(tmp_db):
    """草稿在但卡不在时要报出来，不能装作刷过了。"""
    import evidence
    sid, _ = evidence.ingest_source("https://example.com/orphan", "内容。" * 30,
                                    title="孤儿草稿")
    db.save_v2_card_draft(input_hash="orphan-h", schema_version="1.0",
                          payload={"title": "没有入库的卡"}, source_id=sid, status="draft")
    result = bridge_v1.refresh()
    assert any(m["draft_id"] for m in result["missing"])
    assert any("没有对应的卡片" in m["why"] for m in result["missing"])


def test_refresh_dry_run_writes_nothing(tmp_db):
    """dry-run 要能预览「会刷几张」，但不真的落库。"""
    import evidence
    sid, _ = evidence.ingest_source(MATERIAL["url"], "循环四阶段与状态回写。" * 8,
                                    title=MATERIAL["title"])
    did = db.save_v2_card_draft(
        input_hash="dry-h", schema_version="1.0",
        payload={"title": _draft()["title"]}, source_id=sid, status="draft")
    db.save_v2_draft_figures(did, [GOOD_FIG])
    card = bridge_v1.to_v1_card(_draft(), _items(), MATERIAL, _supplement())
    bridge_v1.save(card, date="2026-09-11")

    result = bridge_v1.refresh(dry_run=True)
    assert result["checked"] >= 1
    assert any(u["figures"] == 1 for u in result["updated"]), "dry-run 要报出会补几张图"
    assert db.load_cards("2026-09-11")[0]["figures"] == [], "dry-run 不该落库"


def test_merge_card_extra_merges_dict_one_level(tmp_db):
    card = bridge_v1.to_v1_card(_draft(), _items(), MATERIAL, _supplement())
    bridge_v1.save(card, date="2026-09-11")
    key = db.load_cards("2026-09-11")[0]["source_url"]

    assert db.merge_card_extra(key, {"_bridge": {"figures": 9}}) is True
    merged = db.load_cards("2026-09-11")[0]["_bridge"]
    assert merged["figures"] == 9
    assert merged["origin"] == "v2", "同层的其它键要保留"

    assert db.merge_card_extra("不存在的来源", {"a": 1}) is False
