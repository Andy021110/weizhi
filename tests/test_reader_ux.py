# -*- coding: utf-8 -*-
"""首屏信息层级的回归测试。

这一批改动全是「文案与位置」，没有断言就只能靠肉眼——而它们恰恰最容易
被后续改动悄悄推翻（比如有人顺手又把「今日 0 张」加回顶栏）。

所以用源码断言是刻意的：接线/文案错了不会让任何功能测试变红，
用户却每天都会看到。
"""
import os
import re

READER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "weizhi", "serve", "web", "reader.html")


def _src():
    return open(READER, encoding="utf-8").read()


def _topbar(src):
    return src.split('<div class="stat-bar">')[1].split("</div>")[0]


def test_no_daily_negative_counter_anywhere():
    """「今日 0 张」是第一屏最大的负反馈：每次打开都要看一遍「你什么都没做」，
    而且它与下方列表的「今天有几张卡」不同义（完成数 vs 内容数）。"""
    assert "todayDone" not in _src()


def test_topbar_shows_cumulative_only():
    bar = _topbar(_src())
    assert "studyDays" in bar
    assert "doneTotal" in bar


def test_quality_button_moved_out_of_topbar():
    """质检是维护动作，不该常驻顶栏与「今天复习」并列。"""
    src = _src()
    assert "qcBtn" not in src
    assert "qcEntryBtn" in src, "它应该出现在统计页里"
    assert "openQcReport" in src


def test_review_entry_shows_a_batch_not_the_debt():
    """「待复习 29」是不可完成的量；「今天复习 N 条」才是能做完的。"""
    src = _src()
    m = re.search(r'id="reviewBtn"[^>]*>([^<]*)', src)
    assert m, "复习入口不该被删掉"
    assert "今天复习" in m.group(1)

    fn = src.split("function loadReviewQueue()")[1].split("function startReview")[0]
    assert "Math.min" in fn, "入口上的数字必须是批次，不是到期总数"
    assert "REVIEW_BATCH" in src


def test_start_review_uses_the_same_batch():
    """入口显示 5 条、点进去却复习 29 条，是不一致。"""
    fn = _src().split("function startReview()")[1].split("function closeReview")[0]
    assert "REVIEW_BATCH" in fn


def test_learned_term_is_unified():
    """「已读」和「已学完」判断的是同一个 isDone，两个词会让人以为是两级。"""
    src = _src()
    assert "✓ 已学完" not in src
    assert "✓ 已读" not in src
    assert "✓ 已学" in src
    assert "✓ 标记已学" in src


def test_list_card_shows_reading_minutes():
    """碎片场景里「这张要花多久」比「讲了什么」更决定现在读还是等会儿。"""
    fn = _src().split("function renderList()")[1].split("function todayReport")[0]
    assert "c.reading_minutes" in fn
    assert "分钟" in fn


def test_countline_answers_what_to_read_today():
    fn = _src().split("function renderList()")[1].split("function todayReport")[0]
    assert "今天可以读" in fn


def test_empty_day_explains_why_instead_of_just_nothing():
    """严格出卡之后「今天没有新卡」是常态而不是故障。
    只说「这里没有」，用户会读成「坏了」——两者该给的动作完全不同。"""
    src = _src()
    assert "function emptyDayHtml" in src
    assert "empty-why" in src
    assert "出卡标准" in src
    assert "pipeline_report" in src, "要能读到产线报告才能解释原因"


def test_stats_uses_distribution_not_just_four_numbers():
    """四格数字答的正是「库存与掌握」，但 509 和 49 并排时，
    「90% 从没进过复习」这件事是看不见的。"""
    src = _src()
    assert "function masteryHtml" in src
    assert "function knowledgeMapHtml" in src
    assert "d.mastery_dist" in src
    assert "d.knowledge_map" in src
