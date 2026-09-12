# -*- coding: utf-8 -*-
"""主产线证据化测试。

改造的是什么：`pipeline.py` 原来把整篇正文塞进 prompt，让模型自己找重点——
生成的事实无法回溯到原文，也没法做数字一致性校验。现在改走
`evidence` + `card_writer` 那条本来就在线上的链路。

这里测的**主要是「该停的时候真的停了」**：严格模式下证据不足必须不成卡。
如果它悄悄退回老路径，改造等于没做，而且从产出上完全看不出来。
"""
import json
import os

from weizhi.produce import evidence
from weizhi.core import db
from weizhi.produce import pipeline


def _article(title="测试材料", url="https://example.com/a", summary=None):
    return {"title": title, "url": url,
            "summary": summary if summary is not None else "内容" * 600}


def _source(name="测试源"):
    return {"name": name, "rss": "https://example.com/feed"}


class _StubProvider:
    prompt_version = "test"


# ---------- goal 构造与证据筛选 ----------

def test_goal_for_article_passes_schema():
    """构造出的 goal 必须过 GoalSpec 校验——不过的话 write_card_gated 会抛异常。"""
    from weizhi.core import schema_v2
    goal = pipeline._goal_for_article(_article(title="MoE 为什么能降低成本"))
    assert schema_v2.validate_goal_spec(goal) == []


def test_goal_terms_come_from_title():
    """标题里的词要进关键词，否则相关性恒为 0，等于白丢一档排序信息。"""
    goal = pipeline._goal_for_article(_article(title="费马大定理的形式化证明"))
    terms = evidence._goal_terms(goal)
    assert any("费马" in t for t in terms), terms
    assert any("大定" in t or "定理" in t for t in terms), terms


def test_relevance_discriminates():
    """同档证据里，贴题的应该拿到更高分。"""
    goal = pipeline._goal_for_article(_article(title="费马大定理的形式化证明"))
    terms = evidence._goal_terms(goal)
    on_topic = {"text": "费马大定理的形式化证明在 Lean 中完成。"}
    off_topic = {"text": "公司食堂本周菜单与营养搭配建议。"}
    assert evidence._relevance(on_topic, terms) > evidence._relevance(off_topic, terms)


# ---------- 严格模式：该停就停 ----------

def test_short_material_rejected_before_ingest(tmp_db, monkeypatch):
    """正文太短就不进证据链路——省一次模型调用，也不污染 v2_sources。"""
    monkeypatch.setattr(pipeline, "extract_full_text", lambda url: "")
    touched = []
    monkeypatch.setattr(evidence, "ingest_source",
                        lambda *a, **k: (touched.append(1), (1, []))[1])
    card, why = pipeline.generate_card_evidenced(
        _StubProvider(), _source(), _article(summary="太短了"), {})
    assert card is None
    assert "正文太短" in why
    assert not touched, "太短的材料不该落库"


def test_insufficient_claims_rejected(tmp_db, monkeypatch):
    """严格模式的核心：证据不够就不出卡，而不是退回去生成一张浅卡。"""
    monkeypatch.setattr(
        evidence, "ingest_source",
        lambda *a, **k: (7, [{"usable": True, "text": "只有一条证据"}]))
    card, why = pipeline.generate_card_evidenced(
        _StubProvider(), _source(), _article(), {})
    assert card is None
    assert "证据不足" in why
    assert str(evidence.MIN_CLAIMS_FOR_PACK) in why


def test_write_card_gate_failure_rejected(tmp_db, monkeypatch):
    """写作门禁不过 → 不成卡。"""
    from weizhi.produce import card_writer
    _stub_chain(monkeypatch)
    monkeypatch.setattr(
        card_writer, "write_card_gated",
        lambda *a, **k: ({}, 1, {"passed": False, "issues": ["某段没有引用编号"]}))
    card, why = pipeline.generate_card_evidenced(
        _StubProvider(), _source(), _article(), {})
    assert card is None
    assert "写作门禁未过" in why


def test_v1_gate_failure_rejected(tmp_db, monkeypatch):
    """过不了 v1 门禁就不落库——宁可当天不出，也不污染卡片库。"""
    from weizhi.produce import bridge_v1
    _stub_chain(monkeypatch)
    monkeypatch.setattr(bridge_v1, "publish_gate", lambda card: (False, ["缺字段"]))
    card, why = pipeline.generate_card_evidenced(
        _StubProvider(), _source(), _article(), {})
    assert card is None
    assert "v1 门禁未过" in why


# ---------- 成功路径：接线是否真的串上了 ----------

def _stub_chain(monkeypatch):
    """把证据链路后半段全部桩掉，只验证接线顺序与参数传递。"""
    from weizhi.produce import assessment
    from weizhi.produce import bridge_v1
    from weizhi.produce import card_writer
    from weizhi.produce import visual

    claims = [
        {"usable": True, "text": "第一条证据的内容足够长", "kind": "fact", "claim_idx": 0},
        {"usable": True, "text": "第二条证据的内容也够长", "kind": "number", "claim_idx": 1},
    ]
    seen = {}

    def fake_ingest(url, raw, **k):
        seen["ingest_url"] = url
        seen["ingest_title"] = k.get("title")
        return 5, claims

    def fake_write(provider, goal, cs, source=None, source_id=None, **k):
        seen["goal"] = goal
        seen["passed_claims"] = cs
        seen["source_id"] = source_id
        return {"objective": "x"}, 11, {"passed": True}

    def fake_assess(provider, draft, cs, **k):
        seen["assessed"] = True
        return [{"question": "q"}], {}

    def fake_from_draft(draft_id, **k):
        seen["draft_id"] = draft_id
        seen["shadow"] = k.get("shadow")
        seen["material"] = k.get("material")
        return {"source_url": "https://example.com/a#v2:deadbeef",
                "title": "卡", "quiz": [], "review_quiz": []}

    monkeypatch.setattr(evidence, "ingest_source", fake_ingest)
    monkeypatch.setattr(card_writer, "write_card_gated", fake_write)
    monkeypatch.setattr(assessment, "generate", fake_assess)
    monkeypatch.setattr(visual, "plan_visuals", lambda *a, **k: [])
    monkeypatch.setattr(visual, "attach", lambda *a, **k: None)
    monkeypatch.setattr(bridge_v1, "from_draft", fake_from_draft)
    monkeypatch.setattr(bridge_v1, "publish_gate", lambda card: (True, []))
    return seen


def test_happy_path_wires_chain_in_order(tmp_db, monkeypatch):
    seen = _stub_chain(monkeypatch)
    card, why = pipeline.generate_card_evidenced(
        _StubProvider(), _source(), _article(title="费马大定理"), {})
    assert why is None and card is not None
    assert seen["ingest_title"] == "费马大定理"
    assert seen["assessed"] is True, "出题这一步不能漏"
    assert seen["source_id"] == 5, "source_id 必须传给写卡器，否则卡与证据断链"
    assert seen["draft_id"] == 11
    assert seen["shadow"] is False, "主产线的卡不是影子卡"
    assert seen["goal"]["key"] == "daily"
    assert seen["material"]["url"] == "https://example.com/a"


# ---------- 生产报告（前端空状态要用） ----------

def test_pipeline_report_written(tmp_db):
    report = pipeline._write_pipeline_report(
        2, [{"title": "某材料", "why": "证据不足（0 条，需 ≥2）"}],
        {"sources": [{"name": "a"}, {"name": "b"}]})
    assert report["generated"] == 2
    assert report["sources"] == 2

    raw = db.get_user_state(pipeline.PIPELINE_REPORT_KEY)
    assert raw, "报告必须落库，否则前端无法解释「今天为什么是空的」"
    saved = json.loads(raw)
    assert saved["generated"] == 2
    assert saved["skipped"][0]["why"].startswith("证据不足")
    assert "date" in saved


# ---------- 默认路径必须是证据链路 ----------

def test_default_path_is_evidenced_not_legacy():
    """旧路径只能出现在 --legacy 分支里。默认走老路径 = 证据化失效。

    这条用源码断言是刻意的：接线错误不会让任何测试变红，
    只会让线上悄悄退回「整篇塞 prompt」。
    """
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "weizhi", "produce", "pipeline.py")
    src = open(path, encoding="utf-8").read()

    # main 的默认分支走 _run_pick（候选筛选）
    main_body = src.split("def main(")[1]
    assert "if args.legacy:" in main_body
    default_branch = main_body.split("if args.legacy:")[1].split("else:")[1]
    assert "_run_pick" in default_branch

    # _run_pick 走证据链路
    pick_body = src.split("def _run_pick(")[1].split("\ndef _run_legacy")[0]
    assert "generate_card_evidenced" in pick_body
    assert "generate_card(" not in pick_body.replace("generate_card_evidenced", "")

    # 旧路径只出现在 _run_legacy 里
    assert "generate_card(" in src.split("def _run_legacy(")[1]


def test_main_initializes_db_itself():
    """脚本要能自包含运行：不主动建表，全新环境第一次抓取就报
    「抓取失败：no such table: user_state」——而且会被读成网络故障。

    这是真实踩到的：原代码只在 save_card 里 init_db，
    而 fetch_rss 一开始就要读 user_state 存 ETag/Last-Modified。
    线上库早就有表，所以这个坑一直没暴露。
    """
    path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "weizhi", "produce", "pipeline.py")
    body = open(path, encoding="utf-8").read().split("def main(")[1]
    assert "db.init_db()" in body, "main 必须主动建表"
    assert body.index("db.init_db()") < body.index("fetch_rss(source"), \
        "建表必须在第一次抓取之前"
