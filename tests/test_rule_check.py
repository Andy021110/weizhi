"""质量巡检客观规则测试（daily_check.rule_check，零成本不烧 AI token）。

规则覆盖：思考题/回答长度/quiz 题数/review_quiz/简答题/精读正文长度/重复卡/时效字段。
"""
import db
from conftest import make_card
from daily_check import rule_check


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


def test_insufficient_quiz(tmp_db):
    c = make_card(quiz=make_card()["quiz"][:1])
    assert "quiz不足" in _labels(c)


def test_insufficient_review_quiz(tmp_db):
    c = make_card(review_quiz=make_card()["review_quiz"][:2])
    assert "复习题不足" in _labels(c)


def test_missing_open_question_for_reading(tmp_db):
    c = make_card(open_question=None)
    assert "缺简答题" in _labels(c)


def test_short_reading_body(tmp_db):
    c = make_card(body="太短了")
    assert "精读正文过短" in _labels(c)


def test_vocab_quiz_minimum_is_two(tmp_db):
    """T1 词汇卡 quiz 门槛是 2（不是 3）。"""
    c = make_card(template="t1_vocab", quiz=make_card()["quiz"][:2])
    assert "quiz不足" not in _labels(c)


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
