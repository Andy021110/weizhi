"""pytest 共享 fixture：临时 SQLite 库隔离 + 造卡工具。"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest

from weizhi.core import db


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


def make_v2_goal(**over):
    """构造一份合法的 GoalSpec（方案 2.3 七个要素都填）。"""
    goal = {
        "key": "agent-harness",
        "capability": "能说清 Agent Harness 的循环由哪几阶段组成",
        "level": "能读源码但没系统梳理过",
        "scene": "给团队做一次 20 分钟分享",
        "success_evidence": "能不查资料画出循环图并标注每阶段职责",
        "prereq": ["用过 LLM API"],
        "milestones": ["画出循环图"],
        "daily_minutes": 60,
    }
    goal.update(over)
    return goal


def make_probe(sufficient=False, questions=None, **over):
    """「探」这一步的输出。"""
    if questions is None:
        questions = [] if sufficient else [
            {"question": "你是想能自己搭一个，还是想能评审别人搭的？",
             "why": "决定卡片的深度是「能动手」还是「能判断」"},
            {"question": "学完打算在什么场合用？",
             "why": "决定内容该聚焦哪个场景"},
        ]
    data = {
        "sufficient": sufficient,
        "missing": [] if sufficient else ["目标能力", "使用场景"],
        "questions": questions,
    }
    data.update(over)
    return data


def make_goalspec(**over):
    """「写规格」这一步的合规输出（3-7 个里程碑，capability 可观察）。"""
    spec = {
        "capability": "能说清 Agent Harness 的循环结构并定位问题出在哪一环",
        "level": "能读源码但没系统梳理过",
        "scene": "给团队做一次 20 分钟分享",
        "success_evidence": "能不查资料画出循环图并逐段标注各阶段职责",
        "prereq": ["用过 LLM API"],
        "milestones": [
            {"name": "说出循环的四个阶段", "evidence": "口头复述不卡壳"},
            {"name": "画出循环图", "evidence": "图上有四个阶段与数据流向"},
            {"name": "定位一次失败请求", "evidence": "能指出卡在哪个阶段并说明依据"},
        ],
    }
    spec.update(over)
    return spec


def make_body(**over):
    """第一次调用（写正文）的合规输出。explanation 段数/字数要够过 validate_card_body。"""
    body = {
        "schema_version": "1.0",
        "objective": "能说清 Agent Harness 循环包含的四个阶段",
        "title": "Agent Harness 的四个阶段",
        "lead": "把 Agent 拆成循环来看，才能判断一次失败的请求卡在哪一步，而不是笼统地怪模型。",
        # 每段都要有「论断 + 一层推演」的量，两句话收尾的段落会被 schema 判过短
        "explanation": [
            {"text": "Agent Harness 是为了补上单次模型调用的边界而出现的执行框架。"
                     "模型只会根据当前上下文产出下一步，既不会自己记住上一步做过什么，"
                     "也不会真的去调用外部系统；Harness 把组装上下文、驱动模型、"
                     "执行工具并把结果写回这三件事补上，多步任务才成为可能。",
             "cites": [0, 1]},
            {"text": "它的核心循环由上下文组装、模型调用、工具执行与状态回写四个阶段组成。"
                     "前两个阶段决定模型看到什么、产出什么；后两个阶段决定外部动作怎么发生、"
                     "结果怎么回到循环里。少了状态回写，模型下一轮就看不见上一轮工具做了什么，"
                     "循环会退化回一次性的单次调用。",
             "cites": [1]},
            {"text": "四个阶段的划分不是实现细节，而是排查问题的抓手。"
                     "当一次 Agent 表现异常，先看上下文里到底装了什么，再看模型产出的动作是否合法，"
                     "接着看工具是否被正确执行，最后看结果有没有写回；"
                     "按这个顺序走，比对着最终答案猜要快得多。",
             "cites": [1]},
        ],
        "examples": [
            {"text": "一次典型调用会依次走完四步：先把工具描述和系统提示组装进上下文，"
                     "再让模型决定调用哪个工具，执行后把返回结果追加回上下文，最后进入下一轮。"
                     "比如用户问「今天北京天气如何」，模型在某一轮产出调用 weather 工具的决定，"
                     "框架执行后把 JSON 结果追加进上下文，模型下一轮才能基于真实数据作答。",
             "cites": [1]},
        ],
    }
    body.update(over)
    return body


def make_structure(**over):
    """第二次调用（派生结构）的合规输出。"""
    struct = {
        "boundaries": [
            {"text": "该框架在 2025 年 3 月的评测中准确率达到 87.5%，但评测只覆盖单轮工具调用场景；"
                     "涉及多智能体协作或长时任务时还需要额外的调度层，不能直接套用这套循环。",
             "cites": [2]},
        ],
        "key_points": [
            "循环由上下文组装、模型调用、工具执行、状态回写四个阶段组成",
            "状态回写决定下一轮能看到什么",
            "每一轮循环都是可观测的调试点",
        ],
        "transfer_task": "挑一个你日常使用的 Agent 产品，指出它的状态回写发生在哪一步，并说明跳过会怎样。",
    }
    struct.update(over)
    return struct


def make_draft(**over):
    """完整的 card draft（= 正文 + 结构），用于门禁测试。"""
    draft = dict(make_body())
    draft.update(make_structure())
    draft["estimated_minutes"] = 7
    draft.update(over)
    return draft


def make_claims():
    """与 make_draft 配套的证据片段（claim_idx 0/1/2）。"""
    return [
        {"claim_idx": 0, "text": "Agent Harness 是一种把模型、工具与循环组织起来的执行框架。",
         "kind": "definition"},
        {"claim_idx": 1, "text": "它的核心循环由上下文组装、模型调用、工具执行与状态回写四个阶段组成。",
         "kind": "fact"},
        {"claim_idx": 2, "text": "在 2025 年 3 月的评测中，该框架的准确率达到 87.5%。",
         "kind": "number"},
    ]


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
