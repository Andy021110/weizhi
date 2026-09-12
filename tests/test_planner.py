"""CP10 测试：最小学习包规划。依赖顺序、相关度门禁、可解释性、逐张写卡。"""
import re

import pytest

from weizhi.core import db
from weizhi.produce import evidence
from weizhi.serve import mastery
from weizhi.serve import planner
from weizhi.core import schema_v2
from conftest import make_goalspec, make_probe  # noqa: F401
from weizhi.core.providers import FakeTextProvider

GOAL_KEY = "g-plan"
MATERIAL = """
Agent Harness 的核心循环由上下文组装、模型调用、工具执行与状态回写四个阶段组成。
每一轮循环结束后，框架会把工具返回的结果追加回上下文，作为下一轮的输入。
画出循环图时，需要把四个阶段按数据流向串起来，并标出状态回写的箭头指向。
状态回写是让循环闭合的关键一步，少了它模型就看不见上一轮工具做了什么。
上下文组装决定模型看到什么，工具执行决定外部动作怎么发生。
天气应用的日活数据与本文无关，仅用于制造一条不相关的证据。
"""


def _setup_goal(tmp_db):
    spec = schema_v2.make_goal(
        key=GOAL_KEY,
        capability="能说清 Agent Harness 的循环结构并定位问题出在哪一环",
        level="能读源码但没系统梳理过",
        scene="给团队做一次 20 分钟分享",
        success_evidence="能不查资料画出循环图并逐段标注各阶段职责",
        prereq=["用过 LLM API"],
        milestones=["说出四个阶段", "画出循环图", "定位一次失败请求"],
        daily_minutes=60,
    )
    spec["milestones"] = [
        {"id": "m%d" % (i + 1), "name": n, "evidence": "能独立完成：" + n}
        for i, n in enumerate(spec.pop("milestones"))
    ]
    return spec


def _ingest(url, text):
    sid, claims = evidence.ingest_source(url, text)
    return {"source_id": sid, "title": url.rsplit("/", 1)[-1], "claims": claims}


def _responder(task, inputs):
    """假模型：从 evidence_block 里取真实存在的编号来引用。"""
    ids = [int(n) for n in re.findall(r"\[#(\d+)\]", inputs.get("evidence_block", ""))]
    a = ids[0] if ids else 0
    b = ids[1] if len(ids) > 1 else a
    if task == "card_body":
        return {
            "schema_version": "1.0",
            "objective": "能说清循环的四个阶段",
            "title": "循环的四个阶段",
            "lead": "把循环拆成四段，才知道一次失败卡在哪。",
            "explanation": [
                {"text": "循环由上下文组装、模型调用、工具执行与状态回写四个阶段组成。"
                         "前两个阶段决定模型看到什么、产出什么；后两个阶段决定外部动作怎么发生、"
                         "结果怎么回到循环里。少了状态回写，模型下一轮就看不见上一轮工具做了什么，"
                         "循环会退化回一次性的单次调用，多步任务也就无从谈起。",
                 "cites": [a]},
                {"text": "四个阶段的划分不是实现细节，而是排查问题的抓手。"
                         "当一次 Agent 表现异常，先看上下文里到底装了什么，再看模型产出的动作是否合法，"
                         "接着看工具是否被正确执行，最后看结果有没有写回；按这个顺序走，"
                         "比对着最终答案猜要快得多。",
                 "cites": [b]},
                {"text": "把循环拆开之后，每一轮都变成可观测的对象，而不是一个黑箱。"
                         "这让优化有了明确落点：你能说清改的是哪一段、预期哪一段的指标会动，"
                         "而不是笼统地调参、调完也不知道到底哪里起了作用。"
                         "反过来说，如果一次改动同时动了两段，你就永远分不清收益来自哪一边。",
                 "cites": [a, b]},
            ],
            "examples": [
                {"text": "一次典型调用会依次走完四步：先把工具描述和系统提示组装进上下文，"
                         "再让模型决定调用哪个工具，执行后把返回结果追加回上下文，最后进入下一轮。"
                         "任何一步断了，循环就停在那里，后面的步骤都不会发生。",
                 "cites": [b]},
            ],
        }
    return {
        "boundaries": [{"text": "这套划分来自单轮工具调用场景，涉及多智能体协作时还需要额外的调度层。",
                        "cites": [a]}],
        "key_points": ["循环由四个阶段组成", "状态回写让循环闭合", "四段划分是排查抓手"],
        "transfer_task": "挑一个你用过的 Agent 产品，指出它的状态回写发生在哪一步。",
    }


# ---------- 依赖顺序 ----------

def test_next_milestone_follows_dependency_order(tmp_db):
    """跳过前置会生成用户接不住的卡，所以按顺序而不是直接挑最弱的。"""
    spec = _setup_goal(tmp_db)
    db.upsert_v2_mastery(GOAL_KEY, "m1", score=0.95)
    db.upsert_v2_mastery(GOAL_KEY, "m2", score=0.2)
    m, state = planner.next_milestone(spec)
    assert m["id"] == "m2"
    assert state["score"] == 0.2


def test_next_milestone_returns_none_when_all_mastered(tmp_db):
    spec = _setup_goal(tmp_db)
    for mid in ("m1", "m2", "m3"):
        db.upsert_v2_mastery(GOAL_KEY, mid, score=0.9)
    assert planner.next_milestone(spec) == (None, None)


# ---------- 相关度 ----------

def test_relevance_is_zero_for_unrelated_claim(tmp_db):
    spec = _setup_goal(tmp_db)
    milestones = spec["milestones"]
    claims = evidence.extract_claims(evidence.clean_text(MATERIAL))
    weather = [c for c in claims if "天气" in c["text"]]
    loop = [c for c in claims if "循环图" in c["text"]]
    assert weather, "样本里应有不相关证据"
    assert planner.relevance(loop[0], milestones[1], spec) > \
        planner.relevance(weather[0], milestones[1], spec)


# ---------- 规划 ----------

def test_plan_requires_active_goal(tmp_db):
    r = planner.plan_next("nope", [])
    assert r["status"] == "no_goal"


def test_plan_reports_goal_complete(tmp_db):
    spec = _setup_goal(tmp_db)
    for mid in ("m1", "m2", "m3"):
        db.upsert_v2_mastery(GOAL_KEY, mid, score=0.9)
    r = planner.plan_next(GOAL_KEY, [], spec=spec)
    assert r["status"] == "goal_complete"
    assert "达标" in r["reason"]


def test_plan_reports_no_material_with_reason(tmp_db):
    spec = _setup_goal(tmp_db)
    src = _ingest("https://x/weather", "今天天气晴朗，适合出门散步，气温舒适。")
    r = planner.plan_next(GOAL_KEY, [src], spec=spec)
    assert r["status"] == "no_material"
    assert r["skipped"], "被跳过的材料要给出理由"


def test_plan_picks_relevant_material_and_explains(tmp_db):
    spec = _setup_goal(tmp_db)
    relevant = _ingest("https://x/harness", MATERIAL)
    noise = _ingest("https://x/cooking", "红烧肉要先焯水，再小火慢炖四十分钟。")
    r = planner.plan_next(GOAL_KEY, [noise, relevant], spec=spec)
    assert r["status"] == "ok"
    assert r["cards"][0]["title"] == "harness"
    assert r["milestone"]["id"] == "m1"
    # 可解释性：方案 M2 要求「系统可以解释为什么下一步推荐这张卡」
    assert "m1" or "说出四个阶段" in r["reason"]
    assert ("缺口" in r["reason"] or "掌握度" in r["reason"])
    assert "相关度" in r["reason"]


def test_plan_respects_pack_size(tmp_db):
    spec = _setup_goal(tmp_db)
    sources = [_ingest("https://x/a%d" % i, MATERIAL) for i in range(5)]
    r = planner.plan_next(GOAL_KEY, sources, spec=spec, limit=2)
    assert len(r["cards"]) == 2
    assert "本轮不生成" in r["reason"]


def test_plan_skips_material_with_too_few_relevant_claims(tmp_db):
    spec = _setup_goal(tmp_db)
    thin = _ingest("https://x/thin", "循环的四个阶段组成了这套机制，状态回写让循环闭合。")
    r = planner.plan_next(GOAL_KEY, [thin], spec=spec)
    assert r["status"] == "no_material"
    assert any("不足" in s["why"] for s in r["skipped"])


# ---------- 逐张写卡 ----------

def test_materialize_writes_cards_with_milestone_mapping(tmp_db):
    """方案 M2 验收：每张卡都要能映射到一个里程碑和一个能力缺口。"""
    spec = _setup_goal(tmp_db)
    src = _ingest("https://x/harness", MATERIAL)
    plan = planner.plan_next(GOAL_KEY, [src], spec=spec)
    out = planner.materialize(FakeTextProvider(responder=_responder), plan, spec=spec)

    assert len(out["written"]) == 1, out
    row = db.get_v2_card_draft_by_id(out["written"][0]["draft_id"])
    assert row["goal_key"] == GOAL_KEY
    assert row["concept"] == "m1"
    assert row["capability_gap"], "要能说明补的是哪个能力缺口"
    assert row["status"] == "draft"


def test_materialize_does_not_stop_on_single_failure(tmp_db, monkeypatch):
    """单张失败不拖垮整包，但要记进结果里，不能静默吞掉。"""
    from weizhi.produce import card_writer
    spec = _setup_goal(tmp_db)
    sources = [_ingest("https://x/a%d" % i, MATERIAL) for i in range(2)]
    plan = planner.plan_next(GOAL_KEY, sources, spec=spec)
    assert len(plan["cards"]) == 2

    calls = {"n": 0}
    real = card_writer.write_card_gated

    def flaky(*a, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("模拟模型超时")
        return real(*a, **kw)

    monkeypatch.setattr(card_writer, "write_card_gated", flaky)
    out = planner.materialize(FakeTextProvider(responder=_responder), plan, spec=spec)
    assert len(out["written"]) == 1 and len(out["failed"]) == 1
    assert "模拟模型超时" in out["failed"][0]["error"]


def test_materialize_is_noop_for_non_ok_plan(tmp_db):
    out = planner.materialize(FakeTextProvider(), {"status": "goal_complete", "reason": "x"})
    assert out["written"] == [] and out["reason"] == "x"


def test_goal_from_spec_reuses_the_same_goal():
    """卡片生成直接吃规格里的目标，不另造一份——两份描述迟早漂移。"""
    spec = _setup_goal(None) if False else schema_v2.make_goal(
        key="k", capability="能画出循环图", success_evidence="能画出循环图并标注职责")
    spec["milestones"] = [{"id": "m1", "name": "画图", "evidence": "能画"}]
    goal = planner._goal_from_spec(spec, "k")
    assert goal["capability"] == spec["capability"]
    assert goal["success_evidence"] == spec["success_evidence"]
    assert goal["milestones"] == ["画图"]
    assert schema_v2.validate_goal_spec(goal) == []
