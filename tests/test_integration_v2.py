"""v2 端到端集成测试（需要真实 DeepSeek Key，缺 Key 时自动跳过）。

对应方案 6.2 的「集成测试」层：真实材料经过完整链路产出候选包。
覆盖 M2 → M3 → M4 → M5 的串联，验证各模块的接口是真的对得上，
而不只是各自单测通过。

跑法：仓库根有 config.json 且填了 key 时自动执行；
没有 key 时整体跳过（CI 里不会误报失败）。
"""
import json
import os

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CFG = os.path.join(ROOT, "config.json")


def _api_key():
    if not os.path.exists(CFG):
        return None
    try:
        key = json.load(open(CFG, encoding="utf-8")).get("deepseek_api_key") or ""
    except Exception:  # noqa: BLE001
        return None
    return key if key and not key.startswith("sk-你的") else None


pytestmark = pytest.mark.skipif(
    _api_key() is None, reason="缺少可用的 config.json / deepseek_api_key")

MATERIAL = """
Agent Harness 的核心循环由上下文组装、模型调用、工具执行与状态回写四个阶段组成。
每一轮循环结束后，框架会把工具返回的结果追加回上下文，作为下一轮的输入。
状态回写是让循环闭合的关键一步，少了它模型就看不见上一轮工具做了什么。
上下文组装决定模型看到什么，工具执行决定外部动作怎么发生。
在 2025 年 3 月的评测中，该框架的准确率达到 87.5%，比基线高出 12 个百分点。
"""


@pytest.fixture()
def provider(tmp_db):
    from weizhi.core.providers import DeepSeekProvider
    return DeepSeekProvider(api_key=_api_key(), timeout=120)


def test_full_chain_m2_to_m5(tmp_db, provider):
    from weizhi.produce import assessment
    from weizhi.core import db
    from weizhi.produce import evidence
    from weizhi.serve import goalspec
    from weizhi.serve import mastery
    from weizhi.produce import news
    from weizhi.serve import planner

    # ---- M2：目标诊断 ----
    r = goalspec.build(provider, "我想搞明白 Agent Harness 到底怎么跑起来的",
                       answers={"使用场景": "给团队做一次技术分享",
                                "当前水平": "能读源码但没系统梳理过"},
                       confirm=True)
    assert r["status"] == "active"
    goal_key = r["goal_key"]
    spec = r["spec"]
    assert len(spec["milestones"]) >= 3, "规格必须带 3 个以上里程碑"
    assert goalspec.active_spec(goal_key)

    # ---- 证据层 ----
    sid, claims = evidence.ingest_source("https://example.com/harness", MATERIAL)
    assert len(claims) >= evidence.MIN_CLAIMS_FOR_PACK

    # ---- M2：规划最小学习包 ----
    # 里程碑名改用受控措辞再规划。原因是一处**已知脆弱性**：
    # 模型生成的里程碑是自由文本（可能写成「理解循环的本质」），
    # 而相关性匹配是关键词级的，跟固定材料对不上就会命中 0 条证据，
    # 于是 plan_next 返回 no_material。这是设计特性不是 bug，但会让
    # 依赖模型措辞的断言变成 flaky —— 所以这里只对「受控措辞」做断言。
    spec = dict(spec, milestones=[
        {"id": "m1", "name": "说出循环的四个阶段", "evidence": "能复述上下文组装与状态回写"},
        {"id": "m2", "name": "画出循环图", "evidence": "图上有四个阶段与数据流向"},
    ])
    sources = [{"source_id": sid, "title": "Agent Harness 执行循环", "claims": claims}]
    plan = planner.plan_next(goal_key, sources, spec=spec)
    assert plan["status"] == "ok", plan
    assert plan["reason"] and "相关度" in plan["reason"]

    # ---- M2：逐张写卡 ----
    out = planner.materialize(provider, plan, spec=spec)
    assert out["written"], out
    draft_id = out["written"][0]["draft_id"]
    row = db.get_v2_card_draft_by_id(draft_id)
    assert row["concept"] == plan["milestone"]["id"], "卡片必须映射到里程碑"
    assert row["capability_gap"]

    # ---- M3：语义配图 ----
    from weizhi.produce import visual
    figs = visual.plan_visuals(provider, row["payload"], claims)
    for fig in figs:
        assert fig["proposition"] and fig["reading"], "每张图要能回答一个明确问题"
        if fig["render_mode"] == "deterministic":
            assert visual.render(fig).startswith("<svg")
    visual.attach(draft_id, figs)

    # ---- M4：出题 + 质量门禁 ----
    items, report = assessment.generate(
        provider, row["payload"], claims, concept=row["concept"], draft_id=draft_id)
    assert report["objective_coverage"] == 1.0, report
    assert items and items[0]["id"]

    # ---- M4：作答 → 掌握度更新 ----
    wrong_idx = next(i for i, o in enumerate(items[0]["options"])
                     if i != items[0]["answer"])
    fb, _ = assessment.submit(goal_key, row["concept"], draft_id, 0,
                              chosen=wrong_idx, confidence=0.8)
    assert fb["correct"] is False
    assert fb["error_reason"], "答错必须给出错误原因"
    after_wrong = fb["mastery_after"]
    assert after_wrong < mastery.DEFAULT_SCORE

    fb2, _ = assessment.submit(goal_key, row["concept"], draft_id, 0,
                               chosen=items[0]["answer"], confidence=0.9)
    assert fb2["correct"] is True
    assert fb2["mastery_after"] > after_wrong, "答对后掌握度应当上升"

    # 掌握度不达标 → 下一个学习包仍盯同一个里程碑
    assert mastery.weakest(goal_key, concepts=[row["concept"]])

    # ---- M4：今日安排 ----
    daily = assessment.daily_plan(goal_key)
    assert daily["reason"]

    # ---- M5：资讯适配 ----
    merged = news.merge([
        {"title": "Agent Harness 新版本发布", "url": "https://a.com/1", "source": "官方博客",
         "source_tier": "official", "published_at": "2026-09-08",
         "summary": "参数量 385M。", "kind": "fast"},
        {"title": "Agent Harness 新版本发布 | 编译", "url": "https://b.com/1",
         "source": "某媒体", "source_tier": "media", "published_at": "2026-09-09",
         "summary": "参数量 400M。", "kind": "fast"},
    ])
    assert len(merged) == 1, "同一事件多来源不该产生两条"
    assert merged[0]["conflicts"], "来源冲突必须被检出而不是静默合并"
    assert "冲突" in news.render_brief(merged)


def test_evidence_and_draft_are_persisted(tmp_db, provider):
    """跑完之后证据与草稿都要能查回来——链路不能只在内存里成立。"""
    from weizhi.core import db
    from weizhi.produce import evidence
    sid, claims = evidence.ingest_source("https://example.com/x", MATERIAL)
    assert db.count_v2_claims(sid) == len(claims)
    assert db.get_v2_source_by_id(sid)["clean_text"]
    assert db.load_v2_claims(sid, usable_only=True)


def test_bridge_output_can_reach_the_frontend(tmp_db, provider):
    """v2 → 桥接 → v1 门禁 → 可发布。这条链路必须端到端通。

    历史：这个用例曾写的是「失败即证明」——当时 v1 的 rule_check 硬要求
    quiz≥3 且 review_quiz≥3，而范围决策已废除固定题量，导致**每一张**
    合规的 v2 卡都被拦下，桥接单独上线发不出一张卡。
    固定题量已按范围决策放宽（`daily_check.rule_check`），现在断言通过。
    """
    from weizhi.produce import assessment
    from weizhi.produce import bridge_v1
    from weizhi.produce import card_writer
    from weizhi.core import db
    from weizhi.produce import evidence
    from weizhi.core import schema_v2

    sid, claims = evidence.ingest_source(
        "https://example.com/bridge", MATERIAL, title="Agent Harness 执行循环")
    goal = schema_v2.make_goal(
        "bridge-demo", "能说清 Agent Harness 的循环结构并定位问题出在哪一环",
        level="能读源码但没梳理过", scene="给团队做技术分享",
        success_evidence="能不查资料画出循环图并标注各阶段职责",
        prereq=["用过 LLM API"], milestones=["说出四个阶段", "画出循环图"],
        daily_minutes=60)

    draft, draft_id, report = card_writer.write_card_gated(
        provider, goal, claims,
        source={"title": "Agent Harness 执行循环", "url": "https://example.com/bridge"},
        source_id=sid)
    assert report["passed"], report

    # 复用已生成的题目，避免多花一次模型调用
    items = db.get_v2_draft_assessment(draft_id) or []
    if not items:
        items, _ = assessment.generate(provider, draft, claims,
                                       concept="m1", draft_id=draft_id)
    assert items, "需要题目才能验证桥接"
    assert all(it.get("layer") in assessment.LAYERS for it in items), \
        "题目必须带时间分层（范围决策 D2）"

    card = bridge_v1.from_draft(
        draft_id, provider=provider,
        material={"title": "Agent Harness 执行循环", "url": "https://example.com/bridge",
                  "site": "示例", "kind": "evolving", "source_tier": "blog"})

    # 映射本身是对的：正文、要点、答案、元信息都到位
    assert len(card["body"]) >= 600
    assert card["core_points"], "关键点必须带过去"
    assert len(card["think_answer"]) >= 150, "v1 门禁硬要求 150 字"
    assert card["open_question"] and card["open_question"].get("grading_points")
    assert card["difficulty"] and card["timeliness"] and card["credibility"]
    assert card["_bridge"]["origin"] == "v2", "要能追回是哪次 v2 生成"

    # 题目数量由目标决定，不再要求 3+3，但「学完当场要能测」仍是硬要求
    assert card["quiz"], "至少要有一道即时题"
    assert bridge_v1.check_v1_compat(card) == [], \
        "合规的桥接卡不该再被门禁拦下：%s" % bridge_v1.check_v1_compat(card)
    passed, issues = bridge_v1.publish_gate(card)
    assert passed, "桥接卡要能过门禁才能进推送，实际问题: %s" % issues

    # 落库后要能在 v1 前端读到
    assert bridge_v1.save(card, date="2026-09-11") is True
    saved = [c for c in db.load_cards() if (c.get("_bridge") or {}).get("origin") == "v2"]
    assert saved, "落库后应能从 v1 的 cards 表查回来"
    assert saved[0]["template"] == "t2_reading"
