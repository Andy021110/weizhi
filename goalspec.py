# -*- coding: utf-8 -*-
"""微知 v2 · 目标诊断（CP9，方案 M2「目标诊断与动态学习路径」）。

解决的问题：用户填一句「我想学 Agent」，系统不能就这么拿去生成内容——
生成出来的必然是一堆泛泛的卡片。方案 2.3 要求把模糊愿望变成：
目标能力、当前水平、使用场景、成功证据、前置知识、里程碑、每日预算。

流程：先探（要不要追问）→ 再写（产出规格）→ 落库为 draft →
用户确认后才 active。草案可编辑：改目标不是 UPDATE，而是插新版本，
旧版本与它的学习记录一起留着（方案 M2 验收门槛）。

两次调用的理由和卡片那边一样：判断「该不该继续问」和「怎么写规格」
是两件事。合在一次调用里，模型倾向于一次问完六个问题把用户劝退。

用法::

    from providers import FakeTextProvider
    from goalspec import build
    r = build(provider, "我想学 Agent")
    r["status"]        # "need_more" 时看 r["questions"]
    r = build(provider, "我想学 Agent", answers={...}, confirm=True)
"""
import hashlib
import re

import db
import providers
import schema_v2

TASK_PROBE = "goal_probe"
TASK_SPEC = "goal_spec"

MAX_QUESTIONS = 3
MIN_MILESTONES = 3
MAX_MILESTONES = 7

_VAGUE_ONLY = re.compile(r"^[想要学习]*\s*(理解|了解|熟悉|掌握|入门|搞懂)")
_OBSERVABLE = re.compile(r"能|可以|会")
# 可观察的动作。单靠「会」不够——「感觉差不多已经会用了」里也有「会」，
# 但那仍然是主观感受，第三方没法对照检查。
_OBSERVABLE_ACTION = re.compile(
    r"能|可以|画出|写出|说出|复述|标注|列出|指出|演示|讲清|复盘|不查资料|独立完成")
_SUBJECTIVE = re.compile(r"(感觉|大概|差不多|自认为|应该就|心里有数|觉得)")

MIN_EVIDENCE_CHARS = 8


def _evidence_errors(ev):
    """成功证据必须第三方可对照检查（方案 M2 验收门槛）。"""
    if len(ev) < MIN_EVIDENCE_CHARS:
        return ["success_evidence 过短或缺失（%d 字 < %d）" % (len(ev), MIN_EVIDENCE_CHARS)]
    if _SUBJECTIVE.search(ev):
        return ["success_evidence 是主观感受（「感觉会了」不算证据，要第三方能对照检查）"]
    if not _OBSERVABLE_ACTION.search(ev):
        return ["success_evidence 缺少可观察的动作（要写「能画出/说出/复述/标注」这类）"]
    return []


# ---------- 校验 ----------

def validate_probe(data):
    """校验「探」的输出。"""
    errs = []
    if not isinstance(data, dict):
        return ["探针输出必须是 JSON 对象"]
    if "sufficient" not in data:
        errs.append("缺字段: sufficient")
    qs = data.get("questions")
    if qs is None:
        errs.append("缺字段: questions")
    elif not isinstance(qs, list):
        errs.append("questions 应为数组")
    elif len(qs) > MAX_QUESTIONS:
        # 问太多会把用户劝退，方案 M2 明确「最多三个高信息量问题」
        errs.append("questions %d 个 >%d，问太多会劝退用户" % (len(qs), MAX_QUESTIONS))
    elif data.get("sufficient") is False and not qs:
        errs.append("判定信息不足却没给问题，用户无法继续")
    elif data.get("sufficient") is True and qs:
        errs.append("判定信息已足够却还在问问题")
    else:
        for i, q in enumerate(qs):
            if isinstance(q, str):
                errs.append("questions[%d] 应为对象（含 question/why），不是字符串" % i)
            elif not (q.get("question") or "").strip():
                errs.append("questions[%d].question 为空" % i)
    return errs


def validate_spec(spec):
    """校验「写规格」的输出。这是方案 M2 验收门槛的落点。"""
    errs = []
    if not isinstance(spec, dict):
        return ["规格必须是 JSON 对象"]

    cap = (spec.get("capability") or "").strip()
    if not cap:
        errs.append("capability 为空")
    elif len(cap) > 60:
        errs.append("capability %d 字 >60" % len(cap))
    elif _VAGUE_ONLY.match(cap) and not _OBSERVABLE.search(cap):
        # 方案 M2 验收：目标必须可观察
        errs.append("capability 不可观察（「理解/了解/熟悉」无法验证，要写「能做出什么」）")

    # 方案 M2 验收：每个 active 目标都要有可观察的成功证据
    errs.extend(_evidence_errors((spec.get("success_evidence") or "").strip()))

    if not (spec.get("scene") or "").strip():
        errs.append("scene 为空（不知道在哪用，就没法判断内容该多深）")

    ms = spec.get("milestones")
    if not isinstance(ms, list):
        errs.append("milestones 应为数组")
    elif not (MIN_MILESTONES <= len(ms) <= MAX_MILESTONES):
        errs.append("milestones %d 个，应在 %d-%d 之间"
                    % (len(ms), MIN_MILESTONES, MAX_MILESTONES))
    else:
        for i, m in enumerate(ms):
            if not isinstance(m, dict):
                errs.append("milestones[%d] 应为对象（含 name/evidence）" % i)
                continue
            if not (m.get("name") or "").strip():
                errs.append("milestones[%d].name 为空" % i)
            if not (m.get("evidence") or "").strip():
                errs.append("milestones[%d].evidence 为空（怎么算完成必须写清）" % i)

    prereq = spec.get("prereq")
    if prereq is not None:
        if not isinstance(prereq, list):
            errs.append("prereq 应为数组")
        elif len(prereq) > 6:
            errs.append("prereq %d 项 >6，列太多前置知识会让人还没开始就放弃" % len(prereq))
        elif not all(isinstance(p, str) and p.strip() for p in prereq):
            errs.append("prereq 每一项都应是非空字符串")
    return errs


# ---------- 调用 ----------

def _slug(raw):
    """由原话派生稳定的 goal_key。同一句原话永远得到同一个 key（幂等）。"""
    digest = hashlib.sha1((raw or "").strip().encode("utf-8")).hexdigest()[:8]
    return "g-" + digest


def probe(provider, raw_goal, level=None, scene=None):
    """判断信息够不够，不够就给出最多 3 个高信息量问题。"""
    inputs = {
        "raw_goal": raw_goal,
        "known_level": level or "未说明",
        "known_scene": scene or "未说明",
    }
    return provider.generate_json(TASK_PROBE, validate_probe, inputs)


def draft_spec(provider, raw_goal, answers=None, level=None, scene=None):
    """产出 GoalSpec 草案。"""
    answers = answers or {}
    block = "\n".join(
        "【追问的回答】%s：%s" % (k, v) for k, v in answers.items() if v
    ) or "（用户未补充，请基于原话合理推断，推断不出来的字段写「待确认」）"
    inputs = {
        "raw_goal": raw_goal,
        "known_level": level or "未说明",
        "known_scene": scene or "未说明",
        "answers_block": block,
    }
    return provider.generate_json(TASK_SPEC, validate_spec, inputs)


def normalize_spec(spec, goal_key):
    """补上确定性字段：goal_key、带 id 的里程碑。

    里程碑 id 必须由程序分配——后面「每张卡映射到一个里程碑」要靠它，
    让模型自己编 id 迟早会撞号或漏号。
    """
    out = dict(spec or {})
    out["key"] = goal_key
    out["milestones"] = [
        {"id": "m%d" % (i + 1),
         "name": (m.get("name") or "").strip(),
         "evidence": (m.get("evidence") or "").strip()}
        for i, m in enumerate(out.get("milestones") or [])
    ]
    out["prereq"] = [p.strip() for p in (out.get("prereq") or []) if str(p).strip()]
    out["daily_minutes"] = out.get("daily_minutes") or 60
    return out


def build(provider, raw_goal, goal_key=None, answers=None, level=None,
          scene=None, confirm=False):
    """编排：先探 → 够就出规格；不够且没给答案则返回问题。

    返回 dict：
    - `{"status": "need_more", "questions": [...], "missing": [...]}`
    - `{"status": "draft"|"active", "goal_id", "version", "goal_key", "spec"}`
    """
    if not (raw_goal or "").strip():
        raise ValueError("学习愿望不能为空")

    answers = {k: v for k, v in (answers or {}).items() if v}
    if not answers:
        p = probe(provider, raw_goal, level, scene)
        if not p.get("sufficient"):
            return {
                "status": "need_more",
                "questions": p.get("questions") or [],
                "missing": p.get("missing") or [],
            }

    spec = normalize_spec(
        draft_spec(provider, raw_goal, answers, level, scene),
        goal_key or _slug(raw_goal),
    )
    # normalize 之后 key 才是最终的，用它再校验一次 GoalSpec 结构
    errs = schema_v2.validate_goal_spec(spec)
    if errs:
        raise providers.ProviderError("规格缺少可执行要素: " + "；".join(errs))

    goal_id, version = db.save_v2_goal(spec["key"], raw_goal, spec, status="draft")
    result = {"status": "draft", "goal_id": goal_id, "version": version,
              "goal_key": spec["key"], "spec": spec}
    if confirm:
        db.set_v2_goal_status(goal_id, "active")
        result["status"] = "active"
    return result


# ---------- 生命周期 ----------

def confirm(goal_id):
    """用户确认草案 → active。只有 active 的目标才允许规划学习包。"""
    goal = db.get_v2_goal(goal_id)
    if not goal:
        return False, "目标不存在"
    errs = schema_v2.validate_goal_spec(goal["spec"] or {})
    if errs:
        return False, "规格不可执行: " + "；".join(errs)
    db.set_v2_goal_status(goal_id, "active")
    return True, "ok"


def revise(goal_key, spec, raw_input=None):
    """用户改了目标：写新版本，旧版本原样保留。

    方案 M2 验收门槛明确要求「用户修改目标后旧学习记录保留」——
    所以这里是插入而不是 UPDATE。已经指向旧版本的学习记录不会被改写。
    """
    prev = db.latest_v2_goal(goal_key)
    if not prev:
        return False, "目标不存在"
    normalized = normalize_spec(spec, goal_key)
    errs = schema_v2.validate_goal_spec(normalized)
    if errs:
        return False, "规格不可执行: " + "；".join(errs)
    goal_id, version = db.save_v2_goal(
        goal_key, raw_input or prev["raw_input"], normalized, status="draft")
    return True, {"goal_id": goal_id, "version": version}


def active_spec(goal_key):
    """取该目标当前的 active 规格（Planner 用）。没有则 None。"""
    goal = db.latest_v2_goal(goal_key, status="active")
    return goal["spec"] if goal else None


def pause(goal_id):
    """暂停：方案 3.1 要求目标可创建、暂停、切换。"""
    db.set_v2_goal_status(goal_id, "paused")
    return True, "ok"


def archive(goal_id):
    db.set_v2_goal_status(goal_id, "archived")
    return True, "ok"
