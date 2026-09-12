"""Quality contract for the personalized AI-product trial cards."""
from scripts.seed_ai_product_trial_packs import card_definitions
from weizhi_v2.content_quality import learning_character_count
from weizhi_v2.validators import validate_pack


def test_trial_cards_have_bounded_learning_scope_and_useful_visuals():
    cards = card_definitions()
    assert len(cards) == 6
    assert len({item["concept_id"] for item in cards}) == 6
    for item in cards:
        assert 5 <= item["estimated_minutes"] <= 10
        assert len(item["content"]["sections"]) == 5
        assert len(item["content"]["illustrations"]) >= 2
        assert item["content"]["key_points"]
        assert learning_character_count(item) >= item["estimated_minutes"] * 60


def test_each_trial_pack_passes_publication_gate():
    cards = card_definitions()
    for group in (cards[:3], cards[3:]):
        payload = [dict(item, position=index) for index, item in enumerate(group, 1)]
        assert validate_pack(payload, [], provenance="user_authored") == []


def test_quizzes_map_to_each_cards_learning_objective():
    for item in card_definitions():
        question = item["content"]["quiz"][0]
        assert question["objective_id"] == item["concept_id"]
        assert question["explanation"]
