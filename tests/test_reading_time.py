# -*- coding: utf-8 -*-
"""阅读时长（碎片场景的关键信息）测试。

为什么要有这个字段：顶栏写着「碎片时间 · 精读卡片」，但每张卡不告诉用户
要花多久——而「现在读还是等会儿」正是靠它决定的。
"""
from weizhi.core import db


def test_reading_minutes_scales_with_body():
    assert db._reading_minutes("字" * 300) == 1
    assert db._reading_minutes("字" * 600) == 2
    assert db._reading_minutes("字" * 3000) == 10


def test_reading_minutes_floor_is_one():
    """只要有正文就至少 1 分钟——显示「约 0 分钟」比不显示更糟。"""
    assert db._reading_minutes("字" * 70) == 1


def test_reading_minutes_none_when_no_body():
    """没有正文时不能编一个数字出来，前端据此不显示该徽标。"""
    assert db._reading_minutes(None) is None
    assert db._reading_minutes("") is None
    assert db._reading_minutes("太短") is None


def test_whitespace_not_counted():
    """换行与空格不计入字数——否则同一篇内容因排版差异给出不同时长。"""
    assert db._reading_minutes("字" * 300 + "\n" * 200) == 1


def test_row_to_card_exposes_reading_minutes(tmp_db):
    """字段要真的出现在列表接口的卡片上（前端读 card.reading_minutes）。"""
    db.save_card({"source_url": "custom:t:1", "title": "测试卡",
                  "body": "字" * 900, "summary": "摘要",
                  "quiz": [], "review_quiz": []}, date="2026-09-12")
    card = db.load_cards("2026-09-12")[0]
    assert card["reading_minutes"] == 3
