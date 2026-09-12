"""WeiZhi v2 vertical-slice contract tests."""
from datetime import datetime
from pathlib import Path

import db
import io
import reader
from reader import ReaderHandler
from scripts.seed_v2_demo import card_definitions
from weizhi_v2 import storage
from weizhi_v2.api import handle_get, handle_post


def make_goal():
    return {
        "outcome": "理解 Agent Harness 并能比较架构取舍",
        "current_level": "知道 LLM 和 Tool Calling",
        "use_context": "Agent 产品与工程设计",
        "daily_minutes": 90,
        "success_evidence": ["能画出运行时边界", "能为新场景做选择"],
        "milestones": ["建立心智模型", "理解真实系统", "完成迁移设计"],
    }


def make_source():
    return {"url": "note:test-source", "title": "测试来源", "source_type": "note", "status": "ready"}


def make_pack(goal_id, source_id):
    claims = [{
        "id": "claim_42",
        "source_id": source_id,
        "claim_text": "该基准包含42个任务",
        "evidence_text": "The benchmark included 42 tasks.",
        "verification_status": "supported",
        "verification": {"method": "exact excerpt"},
    }]
    cards = []
    for i, (cid, objective) in enumerate([
        ("evolution", "从模型边界推导 Agent 部件"),
        ("runtime", "把故障映射到 Harness 能力"),
        ("decision", "为新场景选择 Harness 设计"),
    ], 1):
        cards.append({
            "concept_id": cid,
            "position": i,
            "learning_objective": objective,
            "minimum_recall": objective,
            "estimated_minutes": 8,
            "claim_ids": ["claim_42"],
            "content": {
                "body": "用于验证学习包结构的正文。",
                "factual_sentences": [{"text": "该基准包含42个任务。", "claim_ids": ["claim_42"]}],
                "quiz": [{
                    "question": "当前学习目标的检查题",
                    "options": ["正确", "错误"],
                    "answer": 0,
                    "objective_id": cid,
                }],
            },
        })
    return {
        "goal_id": goal_id,
        "title": "Agent Harness 学习包",
        "pack_objective": "理解形成、职责与取舍",
        "sources": [{"source_id": source_id, "role": "primary"}],
        "claims": claims,
        "cards": cards,
    }


def test_v2_schema_is_created_without_removing_v1(tmp_db):
    conn = db._conn()
    try:
        names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()
    assert "cards" in names
    assert "v2_goals" in names
    assert "v2_card_versions" in names


def test_create_goal_uses_outcomes_not_card_count(tmp_db):
    result = storage.create_goal(make_goal())
    assert result["success"]
    assert result["goal"]["daily_minutes"] == 90
    assert len(result["goal"]["milestones"]) == 3
    assert "total_cards" not in result["goal"]


def test_today_plan_uses_active_goal_budget_by_default(tmp_db):
    payload = make_goal()
    payload["daily_minutes"] = 105
    storage.create_goal(payload)
    assert storage.today_plan()["minute_budget"] == 105


def test_goal_rejects_wrong_daily_budget(tmp_db):
    data = make_goal()
    data["daily_minutes"] = 20
    assert "error" in storage.create_goal(data)


def test_selecting_active_goal_pauses_previous_path(tmp_db):
    first = storage.create_goal(make_goal())["goal"]
    second = storage.create_goal(make_goal())["goal"]
    result = storage.set_goal_status(second["id"], "active")
    assert result["goal"]["status"] == "active"
    goals = {item["id"]: item for item in storage.list_goals()}
    assert goals[first["id"]]["status"] == "paused"
    assert goals[second["id"]]["milestones"][0]["status"] == "active"


def test_today_prioritizes_only_active_goal_packs(tmp_db):
    first = storage.create_goal(make_goal())["goal"]
    second = storage.create_goal(make_goal())["goal"]
    source = storage.create_source(make_source())["source"]
    for goal, suffix in ((first, "a"), (second, "b")):
        payload = make_pack(goal["id"], source["id"])
        payload["title"] += suffix
        pack = storage.create_pack(payload)["pack"]
        storage.publish_pack(pack["id"])
    storage.set_goal_status(second["id"], "active")
    today = storage.today_plan(90)
    assert today["packs"]
    assert {pack["goal_id"] for pack in today["packs"]} == {second["id"]}


def test_completing_milestone_pack_advances_goal(tmp_db):
    goal = storage.create_goal(make_goal())["goal"]
    source = storage.create_source(make_source())["source"]
    payload = make_pack(goal["id"], source["id"])
    payload["milestone_id"] = goal["milestones"][0]["id"]
    pack = storage.create_pack(payload)["pack"]
    pack = storage.publish_pack(pack["id"])["pack"]
    for card in pack["cards"]:
        storage.complete_card(card["id"])
    updated = storage.get_goal(goal["id"])
    assert updated["milestones"][0]["status"] == "validated"
    assert updated["milestones"][1]["status"] == "active"


def test_verified_three_card_pack_can_publish(tmp_db):
    goal = storage.create_goal(make_goal())["goal"]
    source = storage.create_source({
        "url": "https://example.com/harness",
        "title": "Harness",
        "source_type": "article",
        "snapshot_sha256": "abc",
        "status": "ready",
    })["source"]
    created = storage.create_pack(make_pack(goal["id"], source["id"]))
    assert created["pack"]["status"] == "candidate"
    assert created["pack"]["sources"][0]["canonical_uri"] == "https://example.com/harness"
    assert len(created["pack"]["cards"]) == 3
    published = storage.publish_pack(created["pack"]["id"])
    assert published["pack"]["status"] == "published"
    assert all(c["version"]["status"] == "active" for c in published["pack"]["cards"])


def test_unreferenced_fact_is_blocked(tmp_db):
    goal = storage.create_goal(make_goal())["goal"]
    source = storage.create_source({"url": "note:x", "title": "x", "source_type": "note", "status": "ready"})["source"]
    payload = make_pack(goal["id"], source["id"])
    payload["cards"][0]["content"]["factual_sentences"][0]["claim_ids"] = []
    result = storage.create_pack(payload)
    assert result["error"] == "学习包未通过门禁"
    assert any("证据" in issue for issue in result["issues"])


def test_completion_creates_concept_state_and_completes_pack(tmp_db):
    goal = storage.create_goal(make_goal())["goal"]
    source = storage.create_source({"url": "note:x", "title": "x", "source_type": "note", "status": "ready"})["source"]
    pack = storage.create_pack(make_pack(goal["id"], source["id"]))["pack"]
    pack = storage.publish_pack(pack["id"])["pack"]
    for card in pack["cards"]:
        result = storage.complete_card(card["id"])
        assert result["success"]
    assert result["pack"]["status"] == "completed"
    reviews = storage.due_reviews(today="9999-12-31")
    assert len(reviews) == 3


def test_today_plan_respects_budget_and_pack_cap(tmp_db):
    goal = storage.create_goal(make_goal())["goal"]
    source = storage.create_source({"url": "note:x", "title": "x", "source_type": "note", "status": "ready"})["source"]
    for i in range(3):
        payload = make_pack(goal["id"], source["id"])
        payload["title"] += str(i)
        payload["claims"][0]["id"] += str(i)
        for card in payload["cards"]:
            card["concept_id"] += str(i)
            card["claim_ids"] = [payload["claims"][0]["id"]]
            card["content"]["factual_sentences"][0]["claim_ids"] = [payload["claims"][0]["id"]]
            card["content"]["quiz"][0]["objective_id"] = card["concept_id"]
        pack = storage.create_pack(payload)["pack"]
        storage.publish_pack(pack["id"])
    today = storage.today_plan(60)
    assert today["estimated_minutes"] <= 60
    assert len(today["packs"]) == 2


def test_http_independent_dispatcher(tmp_db):
    created = handle_post("/api/v2/goals", make_goal())
    assert created["success"]
    result = handle_get("/api/v2/goals", {})
    assert len(result["goals"]) == 1
    assert handle_get("/api/v2/unknown", {}) is None


def test_v2_static_page_is_served(tmp_db):
    class FakeHandler:
        def __init__(self):
            self.wfile = io.BytesIO()
            self.status = None
            self.headers = {}

        def send_response(self, status):
            self.status = status

        def send_header(self, key, value):
            self.headers[key] = value

        def end_headers(self):
            pass

        def send_error(self, status):
            self.status = status

    fake = FakeHandler()
    ReaderHandler._serve_static(fake, "/v2")
    assert fake.status == 200
    html = fake.wfile.getvalue().decode("utf-8")
    assert "学习包" in html
    assert "renderReading" in html
    assert "内容在下方完整展开" in html
    assert "function packUrl" in html
    assert "location.href=packUrl" in html
    definitions = card_definitions()
    assert all(len(card["content"]["sections"]) >= 4 for card in definitions)
    assert all(sum(len(p) for s in card["content"]["sections"] for p in s["paragraphs"]) >= 450 for card in definitions)
    assert all(len(card["content"]["illustrations"]) >= 2 for card in definitions)
    root = Path(__file__).resolve().parents[1]
    for card in definitions:
        for figure in card["content"]["illustrations"]:
            assert (root / figure["src"].lstrip("/")).exists()
            assert figure["alt"] and figure["caption"] and figure["takeaway"]
        assert card["content"]["evidence_quotes"][0]["source_url"].startswith("https://")


def test_root_page_skips_auth_when_token_is_not_configured(tmp_db, monkeypatch):
    monkeypatch.setattr(reader, "load_access_token", lambda: "")

    class FakeHandler:
        def __init__(self):
            self.wfile = io.BytesIO()
            self.status = None

        def send_response(self, status):
            self.status = status

        def send_header(self, key, value):
            pass

        def end_headers(self):
            pass

        def send_error(self, status):
            self.status = status

    fake = FakeHandler()
    ReaderHandler._serve_static(fake, "/")
    html = fake.wfile.getvalue().decode("utf-8")
    assert fake.status == 200
    assert "var AUTH_REQUIRED = false;" in html
    assert "__WEIZHI_AUTH_REQUIRED__" not in html


def test_root_page_requires_auth_when_token_is_configured(tmp_db, monkeypatch):
    monkeypatch.setattr(reader, "load_access_token", lambda: "configured-secret")

    class FakeHandler:
        def __init__(self):
            self.wfile = io.BytesIO()
            self.status = None

        def send_response(self, status):
            self.status = status

        def send_header(self, key, value):
            pass

        def end_headers(self):
            pass

        def send_error(self, status):
            self.status = status

    fake = FakeHandler()
    ReaderHandler._serve_static(fake, "/")
    html = fake.wfile.getvalue().decode("utf-8")
    assert fake.status == 200
    assert "var AUTH_REQUIRED = true;" in html


def test_published_revision_preserves_learning_state(tmp_db):
    goal = storage.create_goal(make_goal())["goal"]
    source = storage.create_source({"url": "note:x", "title": "x", "source_type": "note", "status": "ready"})["source"]
    pack = storage.create_pack(make_pack(goal["id"], source["id"]))["pack"]
    pack = storage.publish_pack(pack["id"])["pack"]
    card = pack["cards"][0]
    storage.complete_card(card["id"])
    revised = dict(card["version"]["content"])
    revised["body"] = "修订后的正文，不删除原学习记录。"
    result = storage.create_card_version(card["id"], {
        "content": revised,
        "claim_ids": ["claim_42"],
        "publish": True,
    })
    assert result["success"]
    assert result["card"]["version"]["version_number"] == 2
    assert result["card"]["learning_state"]["first_learned_at"] is not None
