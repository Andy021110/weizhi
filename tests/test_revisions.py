# -*- coding: utf-8 -*-
"""CP24 测试：修订候选。自动路径不许改卡；采纳也不许丢掉学习进度。

范围决策：「允许自动生成修订候选，但重新发布前应经过门禁」，
「已经被用户学过的卡片尤其不能静默变化」。

这里最要紧的两条：
1. `propose` 之后卡片必须**一模一样**（自动路径不能碰它）
2. `apply` 之后**学习进度与复习状态必须还在**（原来的实现会 `delete_card`，
   连带删掉 progress——"修卡"顺手抹掉学习历史）
"""
import pytest

import db
import reader
import revisions
from conftest import make_card


@pytest.fixture()
def card(tmp_db):
    """一张已发布、且用户已经学过的卡。"""
    c = make_card(source_url="custom:rev:1", title="旧标题",
                  body="旧正文" * 100, core_points=["旧点"])
    assert db.save_card(c, date="2026-09-01") is True
    db.mark_done("2026-09-01", "custom:rev:1")
    return db.get_card("custom:rev:1")


@pytest.fixture()
def fake_gen(monkeypatch):
    """替掉真实模型调用：只生成内容，不落库。"""
    def _gen(api_key, old, apply=True):
        return {"success": True, "applied": False, "card": {
            "title": "新标题", "body": "新正文" * 60,
            "core_points": ["新点一", "新点二"],
            "quiz": [{"question": "q", "options": ["A", "B"], "answer": 0}],
        }}
    monkeypatch.setattr(reader, "regen_card", _gen)
    return _gen


# ---------- propose：候选不生效 ----------

def test_propose_leaves_the_card_untouched(card, fake_gen):
    before = dict(card)
    r = revisions.propose("k", "custom:rev:1", reason="正文太短")
    assert r["revision_id"]
    after = db.get_card("custom:rev:1")
    assert after["title"] == before["title"], "自动路径改了标题"
    assert after["body"] == before["body"], "自动路径改了正文"
    assert after["core_points"] == before["core_points"]


def test_propose_records_that_the_card_was_studied(card, fake_gen):
    """已学过的卡要能一眼认出来——人已经把时间投进去了。"""
    r = revisions.propose("k", "custom:rev:1", reason="x")
    assert r["studied"] is True
    assert db.list_card_revisions(status="pending")[0]["studied"] is True


def test_propose_keeps_previous_content_for_rollback(card, fake_gen):
    revisions.propose("k", "custom:rev:1", reason="x")
    rev = db.list_card_revisions(status="pending")[0]
    assert rev["prev_payload"]["title"] == "旧标题"


def test_propose_twice_replaces_the_pending_candidate(card, fake_gen):
    """同一张卡不该堆一摞候选——审核界面会被淹。"""
    a = revisions.propose("k", "custom:rev:1", reason="第一次")["revision_id"]
    b = revisions.propose("k", "custom:rev:1", reason="第二次")["revision_id"]
    assert a == b
    assert db.count_pending_revisions() == 1


def test_propose_on_missing_card(tmp_db):
    r = revisions.propose("k", "不存在", reason="x")
    assert "error" in r


def test_propose_surfaces_generation_failure(card, monkeypatch):
    monkeypatch.setattr(reader, "regen_card",
                        lambda *a, **kw: {"error": "模型超时"})
    r = revisions.propose("k", "custom:rev:1")
    assert r["error"] == "模型超时"
    assert db.count_pending_revisions() == 0, "生成失败不该留下空候选"


# ---------- apply：内容换掉，进度留下 ----------

def test_apply_swaps_content_and_keeps_progress(card, fake_gen):
    rid = revisions.propose("k", "custom:rev:1", reason="正文太短")["revision_id"]
    result, err = revisions.apply(rid)
    assert err is None and result["applied"] is True

    after = db.get_card("custom:rev:1")
    assert after["title"] == "新标题"
    assert after["body"].startswith("新正文")
    assert after["core_points"] == ["新点一", "新点二"]
    assert len(after["quiz"]) == 1
    # 学习产物必须留着
    assert db.was_studied("custom:rev:1") is True, "采纳把学习进度弄丢了"
    assert after["source_url"] == "custom:rev:1", "采纳不该换卡号"


def test_apply_keeps_review_state(card, fake_gen):
    db.merge_card_extra("custom:rev:1", {"_x": 1})
    conn = db._conn()
    try:
        conn.execute("UPDATE cards SET next_review_at=?, memory_state=?, review_count=?, "
                     "ease=?, interval_days=? WHERE source_url=?",
                     ("2026-10-01", "learning", 3, 2.6, 7, "custom:rev:1"))
        conn.commit()
    finally:
        conn.close()

    rid = revisions.propose("k", "custom:rev:1", reason="x")["revision_id"]
    revisions.apply(rid)
    after = db.get_card("custom:rev:1")
    assert after["next_review_at"] == "2026-10-01"
    assert after["memory_state"] == "learning"
    assert after["review_count"] == 3
    assert after["interval_days"] == 7


def test_apply_keeps_attachments(card, fake_gen):
    """v2 的溯源、配图不是"内容"，内容更新不该把它们抹掉。"""
    db.merge_card_extra("custom:rev:1",
                        {"_bridge": {"origin": "v2"}, "figures": [{"kind": "flow"}]})
    rid = revisions.propose("k", "custom:rev:1", reason="x")["revision_id"]
    revisions.apply(rid)
    after = db.get_card("custom:rev:1")
    assert (after.get("_bridge") or {}).get("origin") == "v2"
    assert len(after.get("figures") or []) == 1


def test_apply_marks_the_candidate_handled(card, fake_gen):
    rid = revisions.propose("k", "custom:rev:1", reason="x")["revision_id"]
    revisions.apply(rid, note="看着行")
    assert db.count_pending_revisions() == 0
    rev = db.get_card_revision(rid)
    assert rev["status"] == "applied" and rev["note"] == "看着行"


def test_apply_twice_is_refused(card, fake_gen):
    rid = revisions.propose("k", "custom:rev:1", reason="x")["revision_id"]
    revisions.apply(rid)
    result, err = revisions.apply(rid)
    assert result is None and "已经处理过" in err


def test_apply_refuses_unknown_candidate(card):
    result, err = revisions.apply(999)
    assert result is None and "不存在" in err


def test_apply_refuses_when_card_was_deleted(card, fake_gen):
    rid = revisions.propose("k", "custom:rev:1", reason="x")["revision_id"]
    db.delete_card("custom:rev:1")
    result, err = revisions.apply(rid)
    assert result is None and "原卡片已不存在" in err


# ---------- reject / revert ----------

def test_reject_leaves_the_card_alone(card, fake_gen):
    rid = revisions.propose("k", "custom:rev:1", reason="x")["revision_id"]
    result, err = revisions.reject(rid, note="不用改")
    assert err is None
    assert db.get_card("custom:rev:1")["title"] == "旧标题"
    assert db.get_card_revision(rid)["status"] == "rejected"
    assert db.count_pending_revisions() == 0


def test_revert_restores_previous_content_in_place(card, fake_gen):
    """采纳之后反悔，要能原地回滚——同样不能丢进度。"""
    rid = revisions.propose("k", "custom:rev:1", reason="x")["revision_id"]
    revisions.apply(rid)
    assert db.get_card("custom:rev:1")["title"] == "新标题"

    result, err = revisions.revert(rid)
    assert err is None
    after = db.get_card("custom:rev:1")
    assert after["title"] == "旧标题"
    assert after["body"].startswith("旧正文")
    assert db.was_studied("custom:rev:1") is True, "回滚把学习进度弄丢了"


def test_revert_refuses_before_apply(card, fake_gen):
    rid = revisions.propose("k", "custom:rev:1", reason="x")["revision_id"]
    result, err = revisions.revert(rid)
    assert result is None and "只有已采纳" in err


# ---------- 审核界面要看到"改了什么" ----------

def test_diff_summary_lists_changed_fields():
    prev = {"title": "旧", "body": "短", "core_points": ["a"], "quiz": []}
    payload = {"title": "新的标题", "body": "长得多得多的正文",
               "core_points": ["a", "b"], "quiz": [{"q": 1}]}
    d = revisions.diff_summary(prev, payload)
    fields = {x["field"]: x for x in d}
    assert fields["title"]["before"] == 1 and fields["title"]["after"] == 4
    assert fields["body"]["kind"] == "len"
    assert fields["core_points"]["kind"] == "count"
    assert fields["core_points"]["after"] == 2
    assert fields["quiz"]["after"] == 1


def test_diff_summary_is_empty_when_nothing_changed():
    same = {"title": "同", "body": "同", "core_points": ["a"]}
    assert revisions.diff_summary(same, dict(same)) == []


def test_pending_lists_change_summary(card, fake_gen):
    revisions.propose("k", "custom:rev:1", reason="正文太短")
    rows = revisions.pending()
    assert len(rows) == 1
    assert rows[0]["reason"] == "正文太短"
    assert rows[0]["studied"] is True
    assert any(x["field"] == "title" for x in rows[0]["changes"])


# ---------- 自动路径的守卫 ----------

def test_auto_paths_never_apply_a_revision(tmp_db, monkeypatch):
    """daily_check 与 daily_agent 的自动路径只许提出候选，不许动卡。"""
    import inspect
    import daily_check
    import daily_agent

    check_src = inspect.getsource(daily_check.propose_revisions)
    assert "revisions.propose" in check_src
    assert "regen_card" not in check_src, "巡检还在直接重生成"
    assert "apply" not in check_src, "巡检还在直接写回"

    exec_src = inspect.getsource(daily_agent.execute)
    assert "revisions.propose" in exec_src
    assert "regen_card(api_key, src)" not in exec_src, "编排还在直接重生成"


def test_regen_no_longer_deletes_the_old_card():
    """写回实现里不能再出现 delete_card——它会连带删掉 progress。"""
    import inspect
    src = inspect.getsource(reader.regen_card)
    assert "delete_card" not in src
    assert "apply_card_revision" in src


def test_propose_uses_a_non_applying_regen():
    """候选必须用「只生成不写库」的模式，否则候选一生成卡就变了。"""
    import inspect
    src = inspect.getsource(revisions.propose)
    assert "apply=False" in src
