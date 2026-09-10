"""CP4 测试：四项门禁各自的正/反例 + 修复一次 + rejected 不成为候选包。"""
import copy

import pytest

import card_gates
import db
from card_writer import promote_to_candidate, write_card_gated
from conftest import make_claims, make_draft, make_v2_goal
from providers import FakeTextProvider

CLAIMS = make_claims()


# ---------- 各项门禁的正反例 ----------

def test_good_draft_passes_all_gates():
    assert card_gates.run_gates(make_draft(), CLAIMS) == []


def test_citation_coverage_catches_missing_cites():
    draft = make_draft()
    draft["explanation"][0]["cites"] = []
    issues = card_gates.check_citation_coverage(draft, CLAIMS)
    assert any(tag == "缺引用" for tag, _ in issues)


def test_citation_coverage_catches_dangling_cite():
    draft = make_draft()
    draft["examples"][0]["cites"] = [99]
    issues = card_gates.check_citation_coverage(draft, CLAIMS)
    assert any(tag == "引用悬空" for tag, _ in issues)


def test_number_consistency_catches_fabricated_number():
    """正文里出现证据中没有的数字 = 模型编造，必须拦下。"""
    draft = make_draft()
    draft["explanation"][0]["text"] += "它的延迟只有 12 毫秒。"
    issues = card_gates.check_number_consistency(draft, CLAIMS)
    assert any(t == "数字无出处" and "12" in d for t, d in issues)


def test_number_consistency_allows_numbers_present_in_cited_claim():
    draft = make_draft()
    assert card_gates.check_number_consistency(draft, CLAIMS) == []


def test_banned_phrase_is_caught():
    draft = make_draft()
    draft["lead"] = "值得注意，这个循环决定了 Agent 的上限。"
    issues = card_gates.check_banned_phrases(draft, CLAIMS)
    assert any(t == "通用填充句" and "值得注意" in d for t, d in issues)


@pytest.mark.parametrize("phrase", ["在当今时代", "众所周知", "综上所述"])
def test_each_banned_phrase_is_caught(phrase):
    draft = make_draft()
    draft["lead"] = phrase + "，循环结构是 Agent 的核心。"
    assert card_gates.check_banned_phrases(draft, CLAIMS)


def test_body_density_catches_hollow_draft():
    draft = make_draft()
    draft["explanation"] = [{"text": "很短。", "cites": [0]}]
    draft["examples"] = [{"text": "也很短。", "cites": [1]}]
    draft["boundaries"] = [{"text": "还是短。", "cites": [2]}]
    issues = card_gates.check_body_density(draft, CLAIMS)
    tags = [t for t, _ in issues]
    assert "展开不足" in tags or "正文过短" in tags


def test_body_density_catches_flooded_draft():
    draft = make_draft()
    draft["explanation"][0]["text"] = "内容" * 1200
    assert any(t == "正文过长" for t, _ in card_gates.check_body_density(draft, CLAIMS))


# ---------- 汇总与报告 ----------

def test_run_gates_aggregates_all_gates():
    draft = make_draft()
    draft["explanation"][0]["cites"] = []
    draft["lead"] = "值得注意，这个循环很关键。"
    issues = card_gates.run_gates(draft, CLAIMS)
    tags = {t for t, _ in issues}
    assert {"缺引用", "通用填充句"} <= tags


def test_gate_report_shape():
    report = card_gates.gate_report(make_draft(), CLAIMS)
    assert report["passed"] is True
    assert report["issues"] == []
    assert set(report["by_gate"]) == {
        "check_citation_coverage", "check_number_consistency",
        "check_banned_phrases", "check_body_density",
    }

    bad = make_draft()
    bad["lead"] = "众所周知，循环很重要。"
    bad_report = card_gates.gate_report(bad, CLAIMS)
    assert bad_report["passed"] is False
    assert bad_report["issue_distribution"]["通用填充句"] == 1


def test_render_issues_is_readable():
    text = card_gates.render_issues([("缺引用", "explanation 未标注")])
    assert "缺引用" in text and "explanation" in text


# ---------- 修复一次 + 不创建候选包 ----------

def _provider_that_returns(*drafts):
    """依次返回给定草稿；用尽后重复最后一个。"""
    seq = list(drafts)
    state = {"i": 0}

    def responder(task, inputs):
        d = seq[min(state["i"], len(seq) - 1)]
        state["i"] += 1
        return copy.deepcopy(d)
    return FakeTextProvider(responder=responder)


def test_bad_first_version_is_repaired_once(tmp_db):
    """第一版被门禁拦下 → 修复一次后通过，状态 draft 且记录修复次数。"""
    bad = make_draft()
    bad["lead"] = "值得注意，这个循环决定了 Agent 的上限。"
    p = _provider_that_returns(bad, make_draft())
    draft, draft_id, report = write_card_gated(p, _goal(), CLAIMS)
    assert report["passed"] is True
    assert report["repair_attempts"] == 1
    assert p.call_count == 2
    row = db_row(draft_id)
    assert row["status"] == "draft"


def test_still_bad_after_one_repair_is_rejected(tmp_db):
    """修复一次仍不合格 → rejected，失败原因留在 gate_report，不成为候选包。"""
    bad = make_draft()
    bad["lead"] = "值得注意，这个循环决定了 Agent 的上限。"
    p = _provider_that_returns(bad, bad)
    draft, draft_id, report = write_card_gated(p, _goal(), CLAIMS)
    assert report["passed"] is False
    assert report["repair_attempts"] == 1, "只应修复一次"
    assert p.call_count == 2, "修复一次即停"

    row = db_row(draft_id)
    assert row["status"] == "rejected"
    assert row["gate_report"]["issues"], "必须保留失败原因"
    assert any(i["tag"] == "通用填充句" for i in row["gate_report"]["issues"])


def test_promote_refuses_rejected_draft(tmp_db):
    bad = make_draft()
    bad["lead"] = "值得注意，这个循环决定了 Agent 的上限。"
    p = _provider_that_returns(bad, bad)
    _, draft_id, _ = write_card_gated(p, _goal(), CLAIMS)
    ok, reason = promote_to_candidate(draft_id, CLAIMS)
    assert ok is False
    assert "门禁未过" in reason
    assert db_row(draft_id)["status"] == "rejected"


def test_promote_allows_passing_draft(tmp_db):
    p = _provider_that_returns(make_draft())
    _, draft_id, report = write_card_gated(p, _goal(), CLAIMS)
    assert report["passed"] is True
    ok, reason = promote_to_candidate(draft_id, CLAIMS)
    assert ok is True and reason == "ok"
    assert db_row(draft_id)["status"] == "published"


def test_promote_rechecks_gates_even_if_status_says_draft(tmp_db):
    """发布前重跑门禁，不信历史状态——草稿可能已被手工改坏。"""
    p = _provider_that_returns(make_draft())
    _, draft_id, _ = write_card_gated(p, _goal(), CLAIMS)
    row = db.get_v2_card_draft_by_id(draft_id)
    row["payload"]["lead"] = "综上所述，循环很重要。"
    db.save_v2_card_draft(
        input_hash=row["input_hash"], schema_version=row["schema_version"],
        payload=row["payload"], status="draft",
    )
    ok, reason = promote_to_candidate(draft_id, CLAIMS)
    assert ok is False and "门禁未过" in reason


def _goal():
    return make_v2_goal()


def db_row(draft_id):
    return db.get_v2_card_draft_by_id(draft_id)
