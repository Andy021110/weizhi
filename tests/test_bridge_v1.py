"""CP16 测试：v2 → v1 桥接层。映射正确性、分层还原、冲突可见、不凑题。"""
import pytest

import bridge_v1
import db
from conftest import make_body, make_structure

MATERIAL = {"title": "Agent Harness 执行循环", "url": "https://example.com/h",
            "site": "官方博客", "kind": "evolving", "source_tier": "official"}


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


def _supplement():
    return {
        "think_answer": "判断依据是这套循环假设工具调用是幂等的。" * 8,
        "open_question": {"question": "什么情况下不能直接套用这套循环？",
                          "reference_answer": "当工具涉及写操作时，" * 10,
                          "grading_points": ["指出写操作需要确认层", "说明幂等假设"]},
    }


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
                                figures=[{"kind": "flow"}], pack_id=7, draft_id=42)
    b = card["_bridge"]
    assert (b["pack_id"], b["draft_id"], b["figures"]) == (7, 42, 1)
    assert b["objective"]


# ---------- 不凑题 + 冲突可见 ----------

def test_does_not_pad_questions_to_satisfy_old_gate():
    """v1 门禁要 quiz≥3，但范围决策已废除固定题量——不许编题凑数。"""
    card = bridge_v1.to_v1_card(_draft(), _items(immediate=1, later=0), MATERIAL, _supplement())
    assert len(card["quiz"]) == 1, "不该为了过旧门禁编出多余题目"
    assert card["review_quiz"] == []


def test_compat_reports_conflicts_instead_of_hiding_them():
    card = bridge_v1.to_v1_card(_draft(), _items(immediate=1, later=0), MATERIAL, _supplement())
    issues = bridge_v1.check_v1_compat(card)
    tags = [t for t, _ in issues]
    assert any("quiz" in t for t in tags)
    assert any("review_quiz" in t for t in tags)


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
    card = bridge_v1.to_v1_card(_draft(), _items(immediate=1, later=0), MATERIAL, _supplement())
    ok, issues = bridge_v1.publish_gate(card)
    assert ok is False
    assert issues, "被拦下要给出原因，不能只返回 False"
