"""Contracts for the one-task-per-path starter curriculum."""
from scripts.seed_seven_path_starters import EXTRAS, STARTERS, make_card
from scripts.generate_ai_product_card_diagrams import visual
from weizhi_v2.content_quality import learning_character_count
from weizhi_v2.validators import validate_pack


def test_seven_paths_have_six_new_starters_plus_product_pack():
    assert len(STARTERS) == 6
    assert len({item["goal_key"] for item in STARTERS}) == 6
    assert len(EXTRAS) == 12
    assert all(sum(item["goal_key"] == starter["goal_key"] for item in EXTRAS) == 2 for starter in STARTERS)


def test_starter_cards_are_short_visual_and_publishable():
    for spec in STARTERS + EXTRAS:
        card = make_card(spec)
        assert len(card["content"]["illustrations"]) == 2
        assert len(card["content"]["sections"]) == 5
        assert learning_character_count(card) >= card["estimated_minutes"] * 60
        assert validate_pack([dict(card, position=1)], [], provenance="user_authored") == []


def test_each_path_forms_a_three_card_pack():
    for starter in STARTERS:
        specs = [starter] + [item for item in EXTRAS if item["goal_key"] == starter["goal_key"]]
        cards = [dict(make_card(item), position=index) for index, item in enumerate(specs, 1)]
        assert validate_pack(cards, [], provenance="user_authored") == []


def test_diagrams_use_six_distinct_visual_grammars():
    nodes = ["一", "二", "三", "四"]
    variants = [visual(nodes, index) for index in range(6)]
    assert len(set(variants)) == 6
    assert any("ellipse" in item for item in variants)
    assert any("对照矩阵" in item for item in variants)
