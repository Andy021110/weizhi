"""pytest 共享 fixture：临时 SQLite 库隔离 + 造卡工具。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

import db


@pytest.fixture()
def tmp_db(monkeypatch):
    """用临时库替换真实库（DB_PATH 模块级），测试不污染 weizhi.db。"""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setattr(db, "DB_PATH", path)
    db.init_db()
    yield path
    if os.path.exists(path):
        os.unlink(path)


def make_card(**over):
    """构造一张能通过全部客观规则的卡（可按需覆盖字段制造坏卡）。"""
    card = {
        "source_url": "custom:test:1",
        "title": "ReAct 范式原理测试卡",
        "summary": "ReAct 把推理与行动交替进行。",
        "body": "正文内容" * 200,  # 1200 字
        "template": "t2_reading",
        "difficulty": "入门",
        "think_question": "为什么 ReAct 能减少幻觉？",
        "think_answer": "回答内容" * 80,  # 240 字
        "quiz": [{"question": "理解题%d" % i, "options": ["A", "B", "C", "D"], "answer": 0, "explanation": "x"} for i in range(3)],
        "review_quiz": [{"question": "复习题%d" % i, "options": ["A", "B", "C", "D"], "answer": i} for i in range(3)],
        "open_question": {"question": "用自己的话解释 ReAct", "reference_answer": "参考回答", "grading_points": ["a", "b", "c"]},
        "core_points": ["点1", "点2", "点3"],
        "source": "测试来源",
        "timeliness": "stable",
        "credibility": "A",
        "published": None,
    }
    card.update(over)
    return card
