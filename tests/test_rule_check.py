"""质量巡检客观规则测试（daily_check.rule_check，零成本不烧 AI token）。

规则覆盖：思考题/回答长度/quiz 题数/review_quiz/简答题/精读正文长度/重复卡/时效字段。
"""
from weizhi.core import db
from conftest import make_card
from weizhi.ops.daily_check import rule_check


def _labels(card):
    return [i[0] for i in rule_check(card)]


def test_good_card_passes(tmp_db):
    assert rule_check(make_card()) == []


def test_missing_think_question(tmp_db):
    c = make_card(think_question="")
    assert "缺思考题" in _labels(c)


def test_missing_think_answer(tmp_db):
    c = make_card(think_answer="")
    assert "缺思考题答案" in _labels(c)


def test_short_think_answer(tmp_db):
    c = make_card(think_answer="太短了")
    assert "回答过短" in _labels(c)


def test_empty_quiz_is_blocked(tmp_db):
    """范围决策废除了固定题量，但「学完当场要能测」仍是硬要求。"""
    c = make_card(quiz=[], review_quiz=[{"question": "回忆题", "options": ["A", "B"], "answer": 0}])
    assert "缺即时题" in _labels(c)


def test_one_immediate_question_is_enough(tmp_db):
    """单一判断目标一道好题就够——这是范围决策 D1 的直接后果。"""
    c = make_card(quiz=make_card()["quiz"][:1], review_quiz=[])
    labels = _labels(c)
    assert "缺即时题" not in labels
    assert "无题目" not in labels


def test_review_quiz_count_is_not_enforced(tmp_db):
    """随堂题与复习题已合并为分层题库，不再要求复习题满三道。"""
    c = make_card(review_quiz=make_card()["review_quiz"][:1])
    assert "复习题不足" not in _labels(c)


def test_missing_open_question_for_reading(tmp_db):
    c = make_card(open_question=None)
    assert "缺简答题" in _labels(c)


def test_short_reading_body(tmp_db):
    c = make_card(body="太短了")
    assert "精读正文过短" in _labels(c)


def test_no_template_specific_quiz_minimum(tmp_db):
    """模板级题量下限已随范围决策一起废除（六类卡片也要从界面下线）。"""
    for tpl in ("t1_vocab", "t2_reading", "t4_trivia", "t5_skill"):
        c = make_card(template=tpl, quiz=make_card()["quiz"][:1])
        assert "缺即时题" not in _labels(c), tpl


def test_fast_missing_published_with_real_source(tmp_db):
    """fast/event 有真实 http 来源必须带 published。"""
    c = make_card(timeliness="fast", credibility="B",
                  source_url="https://example.com/a", published=None)
    assert "缺发布时间" in _labels(c)


def test_topic_mode_not_forced_published(tmp_db):
    """topic 模式（无真实来源）不强制 published——防误报的修正。"""
    c = make_card(timeliness="fast", credibility="B",
                  source_url="custom:test:1", published=None)
    assert "缺发布时间" not in _labels(c)


def test_missing_credibility_for_evolving(tmp_db):
    c = make_card(timeliness="evolving", credibility=None)
    assert "缺权威度" in _labels(c)


def test_duplicate_card(tmp_db):
    db.save_card(make_card(title="同名卡"))
    c = make_card(title="同名卡", source_url="custom:test:2")
    assert "重复卡" in _labels(c)


def test_wrong_field_type(tmp_db):
    c = make_card(quiz="not-a-list")
    assert "字段类型错误" in _labels(c)
