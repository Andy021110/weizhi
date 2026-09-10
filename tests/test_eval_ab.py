"""CP5 测试：A/B 评测脚手架的规则版基线、盲评编号、评分表与局限提示。"""
import eval_ab
from conftest import make_claims

SHORT = {"title": "一句话新闻", "url": "u", "text": "某公司于 2026 年发布了新模型。"}
MATERIAL = {
    "title": "Agent Harness 的执行循环",
    "url": "https://example.com/x",
    "text": eval_ab.DEMO_MATERIALS[0]["text"],
}


def test_rule_version_is_plain_concatenation():
    """规则版刻意保留 v1 特征：原文顺序拼接，不解释、不设目标、不给迁移任务。"""
    claims = make_claims()
    v = eval_ab.rule_version(claims, MATERIAL)
    assert v["version"] == "rule"
    assert v["objective"] == "" and v["transfer_task"] == ""
    assert v["body"] == "\n".join(c["text"] for c in claims)
    assert v["cites"] == [c["claim_idx"] for c in claims]


def test_build_pair_skips_thin_material(tmp_db):
    """证据不足 2 条的材料不进评测——方案 3.1 的硬门槛。"""
    import db
    db.init_db()
    provider = eval_ab._make_provider("fake")
    assert eval_ab.build_pair(SHORT, eval_ab.schema_v2.make_goal("k", "c"), provider) is None


def test_build_pair_produces_both_versions(tmp_db):
    import db
    db.init_db()
    provider = eval_ab._make_provider("fake")
    goal = eval_ab.schema_v2.make_goal("k", "能说清核心机制", success_evidence="能复述")
    pair = eval_ab.build_pair(MATERIAL, goal, provider)
    assert pair is not None
    assert pair["rule"]["version"] == "rule"
    assert pair["model"]["version"] == "model"
    assert pair["model"]["gate_passed"] is True, "合成草稿应能通过门禁（脚手架自检）"
    assert pair["claim_count"] >= 2


def test_blind_pairs_are_deterministic_and_balanced():
    pairs = [{"material": "m%d" % i,
              "rule": {"version": "rule"}, "model": {"version": "model"}}
             for i in range(20)]
    first = eval_ab.blind_pairs(pairs, seed=1)
    again = eval_ab.blind_pairs(pairs, seed=1)
    assert [b["_answer"] for b in first] == [b["_answer"] for b in again]

    # 每组里 A/B 必须恰好一个是模型版
    for b in first:
        assert sorted(b["_answer"].values()) == ["model", "rule"]
    # 20 组里两种顺序都该出现，否则编号有偏
    orders = {b["_answer"]["A"] for b in first}
    assert orders == {"model", "rule"}


def test_score_sheet_covers_all_dimensions():
    blinded = eval_ab.blind_pairs(
        [{"material": "m", "rule": {"version": "rule"}, "model": {"version": "model"}}], seed=1)
    md = eval_ab.render_score_md(blinded, 1)
    for name, _ in eval_ab.DIMENSIONS:
        assert name in md
    assert "阅读负荷越低越好" in md, "阅读负荷是反向指标，必须写清否则会被评反"
    assert "判定规则" in md


def test_blind_md_warns_when_using_fake_provider():
    blinded = eval_ab.blind_pairs(
        [{"material": "m", "rule": {"version": "rule"}, "model": {"version": "model"}}], seed=1)
    assert "盲评不成立" in eval_ab.render_blind_md(blinded, 1, "fake")
    assert "盲评不成立" not in eval_ab.render_blind_md(blinded, 1, "deepseek")
