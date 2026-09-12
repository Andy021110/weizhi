"""SQLite repositories for the non-destructive v2 data model."""
import json
import os
import sqlite3
import uuid
from datetime import datetime, timedelta

from .validators import evidence_hash, validate_pack

HERE = os.path.dirname(os.path.abspath(__file__))


def _now():
    return datetime.now().isoformat(timespec="seconds")


def _id(prefix):
    return "%s_%s" % (prefix, uuid.uuid4().hex[:16])


def _db_path():
    import db
    return db.DB_PATH


def _conn():
    conn = sqlite3.connect(_db_path(), timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_schema(db_path=None):
    path = db_path or _db_path()
    conn = sqlite3.connect(path, timeout=30)
    try:
        with open(os.path.join(HERE, "schema.sql"), "r", encoding="utf-8") as f:
            conn.executescript(f.read())
        conn.commit()
    finally:
        conn.close()


def _row(row):
    if not row:
        return None
    item = dict(row)
    for key in list(item):
        if key.endswith("_json") and item[key] is not None:
            try:
                item[key[:-5]] = json.loads(item.pop(key))
            except (TypeError, json.JSONDecodeError):
                pass
    return item


def create_goal(data):
    outcome = (data.get("outcome") or "").strip()
    level = (data.get("current_level") or "").strip()
    context = (data.get("use_context") or "").strip()
    minutes = int(data.get("daily_minutes") or 90)
    if not outcome or not level or not context:
        return {"error": "目标、当前水平和使用场景不能为空"}
    if not 60 <= minutes <= 120:
        return {"error": "每日时间必须在60-120分钟之间"}
    status = data.get("status") or "active"
    if status not in ("draft", "active", "paused", "completed", "archived"):
        return {"error": "学习目标状态无效"}
    goal_id = _id("goal")
    success = data.get("success_evidence") or []
    now = _now()
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO v2_goals VALUES (?,?,?,?,?,?,?,?,?)",
            (goal_id, outcome, level, context, minutes, json.dumps(success, ensure_ascii=False), status, now, now),
        )
        milestones = data.get("milestones") or []
        for index, milestone in enumerate(milestones[:5], 1):
            text = milestone.get("outcome") if isinstance(milestone, dict) else str(milestone)
            if text.strip():
                conn.execute(
                    "INSERT INTO v2_milestones VALUES (?,?,?,?,?,?)",
                    (_id("mile"), goal_id, index, text.strip(), "[]", "active" if status == "active" and index == 1 else "planned"),
                )
        conn.commit()
    finally:
        conn.close()
    return {"success": True, "goal": get_goal(goal_id)}


def list_goals():
    conn = _conn()
    try:
        rows = conn.execute("SELECT * FROM v2_goals ORDER BY created_at DESC").fetchall()
        result = []
        for row in rows:
            goal = _row(row)
            ms = conn.execute("SELECT * FROM v2_milestones WHERE goal_id=? ORDER BY position", (goal["id"],)).fetchall()
            goal["milestones"] = [_row(x) for x in ms]
            result.append(goal)
        return result
    finally:
        conn.close()


def get_goal(goal_id):
    return next((g for g in list_goals() if g["id"] == goal_id), None)


def set_goal_status(goal_id, status):
    """Select one active learning path, or pause the current path."""
    if status not in ("active", "paused"):
        return {"error": "只支持开始或暂停学习路径"}
    conn = _conn()
    try:
        goal = conn.execute("SELECT * FROM v2_goals WHERE id=?", (goal_id,)).fetchone()
        if not goal or goal["status"] in ("archived", "completed"):
            return {"error": "学习路径不存在或不可修改"}
        now = _now()
        if status == "active":
            conn.execute("UPDATE v2_goals SET status='paused', updated_at=? WHERE status='active' AND id<>?", (now, goal_id))
            conn.execute("UPDATE v2_goals SET status='active', updated_at=? WHERE id=?", (now, goal_id))
            current = conn.execute(
                "SELECT id FROM v2_milestones WHERE goal_id=? AND status IN ('active','validated') ORDER BY position DESC LIMIT 1",
                (goal_id,),
            ).fetchone()
            if not current:
                first = conn.execute("SELECT id FROM v2_milestones WHERE goal_id=? ORDER BY position LIMIT 1", (goal_id,)).fetchone()
                if first:
                    conn.execute("UPDATE v2_milestones SET status='active' WHERE id=?", (first["id"],))
        else:
            conn.execute("UPDATE v2_goals SET status='paused', updated_at=? WHERE id=?", (now, goal_id))
        conn.commit()
    finally:
        conn.close()
    return {"success": True, "goal": get_goal(goal_id)}


def create_source(data):
    uri = (data.get("canonical_uri") or data.get("url") or "").strip()
    title = (data.get("title") or uri).strip()
    source_type = data.get("source_type") or "article"
    if source_type not in ("article", "pdf", "paper", "repository", "note"):
        return {"error": "不支持的来源类型"}
    if not uri or not title:
        return {"error": "来源地址和标题不能为空"}
    source_id = _id("src")
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO v2_sources VALUES (?,?,?,?,?,?,?,?,?,?)",
            (source_id, source_type, uri, title, data.get("author"), data.get("published_at"),
             data.get("snapshot_path"), data.get("snapshot_sha256"), data.get("status", "pending"), _now()),
        )
        conn.commit()
    except sqlite3.IntegrityError:
        return {"error": "该来源快照已存在"}
    finally:
        conn.close()
    return {"success": True, "source": get_source(source_id)}


def get_source(source_id):
    conn = _conn()
    try:
        return _row(conn.execute("SELECT * FROM v2_sources WHERE id=?", (source_id,)).fetchone())
    finally:
        conn.close()


def list_sources():
    conn = _conn()
    try:
        return [_row(x) for x in conn.execute(
            "SELECT s.*, (SELECT COUNT(*) FROM v2_evidence_claims e "
            "WHERE e.source_id=s.id AND e.verification_status='supported') AS supported_claim_count, "
            "(SELECT r.detail_json FROM v2_source_processing_runs r WHERE r.source_id=s.id "
            "ORDER BY r.started_at DESC LIMIT 1) AS processing_detail_json "
            "FROM v2_sources s ORDER BY s.created_at DESC"
        ).fetchall()]
    finally:
        conn.close()


def create_pack(data):
    cards = data.get("cards") or []
    claims = data.get("claims") or []
    provenance = data.get("provenance") or "verified"
    errors = validate_pack(cards, claims, provenance=provenance)
    if errors:
        return {"error": "学习包未通过门禁", "issues": errors}
    if provenance == "verified":
        source_ids = {item.get("source_id") for item in (data.get("sources") or [])}
        if not source_ids:
            return {"error": "核验型学习包必须关联来源"}
        conn = _conn()
        try:
            rows = conn.execute(
                "SELECT id,status FROM v2_sources WHERE id IN (%s)" % ",".join("?" for _ in source_ids),
                tuple(source_ids),
            ).fetchall()
        finally:
            conn.close()
        ready = {row["id"] for row in rows if row["status"] == "ready"}
        if ready != source_ids:
            return {"error": "学习包包含未就绪来源"}
        if any(claim.get("source_id") not in source_ids for claim in claims):
            return {"error": "证据主张引用了学习包之外的来源"}
    pack_id = _id("pack")
    now = _now()
    total_minutes = sum(c["estimated_minutes"] for c in cards)
    conn = _conn()
    try:
        conn.execute("BEGIN")
        for claim in claims:
            conn.execute(
                "INSERT OR REPLACE INTO v2_evidence_claims VALUES (?,?,?,?,?,?,?,?,?)",
                (claim["id"], claim["source_id"], claim["claim_text"], claim["evidence_text"],
                 claim.get("locator"), claim.get("claim_type", "fact"), claim.get("verification_status", "unknown"),
                 json.dumps(claim.get("verification") or {}, ensure_ascii=False), now),
            )
        conn.execute(
            "INSERT INTO v2_learning_packs VALUES (?,?,?,?,?,?,?,?,?,?)",
            (pack_id, data.get("goal_id"), data.get("milestone_id"), data.get("title") or "未命名学习包",
             data.get("pack_objective") or "完成本学习包", total_minutes, "candidate",
             data.get("generation_version") or "v2-manual", now, now),
        )
        for source in data.get("sources") or []:
            conn.execute("INSERT INTO v2_pack_sources VALUES (?,?,?)", (pack_id, source["source_id"], source.get("role", "primary")))
        ev_hash = evidence_hash(claims) if claims else "user-authored"
        for position, card in enumerate(cards, 1):
            card_id = card.get("id") or _id("card")
            concept_id = card.get("concept_id") or _id("concept")
            version_id = _id("version")
            conn.execute(
                "INSERT INTO v2_card_specs VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (card_id, pack_id, concept_id, position, card["learning_objective"], card.get("minimum_recall") or card["learning_objective"],
                 card.get("transfer_task"), json.dumps(card.get("prerequisites") or [], ensure_ascii=False),
                 1 if card.get("durable", True) else 0, card["estimated_minutes"], None),
            )
            qa = {"status": "passed", "issues": []}
            conn.execute(
                "INSERT INTO v2_card_versions VALUES (?,?,?,?,?,?,?,?,?)",
                (version_id, card_id, 1, json.dumps(card.get("content") or {}, ensure_ascii=False), ev_hash,
                 data.get("generation_version") or "v2-manual", json.dumps(qa, ensure_ascii=False), "candidate", now),
            )
            for claim_id in sorted(set(card.get("claim_ids") or [])):
                conn.execute("INSERT INTO v2_card_claims VALUES (?,?,?)", (version_id, claim_id, "body"))
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {"success": True, "pack": get_pack(pack_id)}


def publish_pack(pack_id):
    conn = _conn()
    try:
        pack = conn.execute("SELECT * FROM v2_learning_packs WHERE id=?", (pack_id,)).fetchone()
        if not pack:
            return {"error": "学习包不存在"}
        if pack["status"] not in ("candidate", "published"):
            return {"error": "只有候选学习包可以发布"}
        specs = conn.execute("SELECT * FROM v2_card_specs WHERE pack_id=? ORDER BY position", (pack_id,)).fetchall()
        for spec in specs:
            candidate = conn.execute(
                "SELECT * FROM v2_card_versions WHERE card_spec_id=? AND status='candidate' ORDER BY version_number DESC LIMIT 1",
                (spec["id"],),
            ).fetchone()
            if not candidate and not spec["active_version_id"]:
                return {"error": "存在没有候选版本的卡片"}
            if candidate:
                conn.execute("UPDATE v2_card_versions SET status='superseded' WHERE card_spec_id=? AND status='active'", (spec["id"],))
                conn.execute("UPDATE v2_card_versions SET status='active' WHERE id=?", (candidate["id"],))
                conn.execute("UPDATE v2_card_specs SET active_version_id=? WHERE id=?", (candidate["id"], spec["id"]))
        conn.execute("UPDATE v2_learning_packs SET status='published', updated_at=? WHERE id=?", (_now(), pack_id))
        conn.commit()
    finally:
        conn.close()
    return {"success": True, "pack": get_pack(pack_id)}


def reject_pack(pack_id):
    """Reject a generated candidate after human review."""
    conn = _conn()
    try:
        pack = conn.execute("SELECT status FROM v2_learning_packs WHERE id=?", (pack_id,)).fetchone()
        if not pack:
            return {"error": "学习包不存在"}
        if pack["status"] != "candidate":
            return {"error": "只有候选学习包可以拒绝"}
        conn.execute("UPDATE v2_learning_packs SET status='rejected', updated_at=? WHERE id=?", (_now(), pack_id))
        conn.execute(
            "UPDATE v2_card_versions SET status='rejected' WHERE card_spec_id IN "
            "(SELECT id FROM v2_card_specs WHERE pack_id=?) AND status='candidate'",
            (pack_id,),
        )
        conn.commit()
    finally:
        conn.close()
    return {"success": True, "pack": get_pack(pack_id)}


def get_pack(pack_id):
    conn = _conn()
    try:
        pack = _row(conn.execute("SELECT * FROM v2_learning_packs WHERE id=?", (pack_id,)).fetchone())
        if not pack:
            return None
        source_rows = conn.execute(
            "SELECT s.*, ps.source_role AS role FROM v2_sources s "
            "JOIN v2_pack_sources ps ON ps.source_id=s.id WHERE ps.pack_id=? ORDER BY s.created_at",
            (pack_id,),
        ).fetchall()
        pack["sources"] = [_row(row) for row in source_rows]
        specs = conn.execute("SELECT * FROM v2_card_specs WHERE pack_id=? ORDER BY position", (pack_id,)).fetchall()
        cards = []
        for spec_row in specs:
            card = _row(spec_row)
            version_id = card.get("active_version_id")
            version = None
            if version_id:
                version = conn.execute("SELECT * FROM v2_card_versions WHERE id=?", (version_id,)).fetchone()
            if not version:
                version = conn.execute("SELECT * FROM v2_card_versions WHERE card_spec_id=? ORDER BY version_number DESC LIMIT 1", (card["id"],)).fetchone()
            if version:
                v = dict(version)
                v["content"] = json.loads(v.pop("content_json"))
                v["qa"] = json.loads(v.pop("qa_json"))
                card["version"] = v
            state = conn.execute("SELECT * FROM v2_learning_states WHERE concept_id=?", (card["concept_id"],)).fetchone()
            card["learning_state"] = _row(state)
            cards.append(card)
        pack["cards"] = cards
        return pack
    finally:
        conn.close()


def list_packs(status=None):
    conn = _conn()
    try:
        if status:
            ids = [r[0] for r in conn.execute("SELECT id FROM v2_learning_packs WHERE status=? ORDER BY created_at DESC", (status,)).fetchall()]
        else:
            ids = [r[0] for r in conn.execute("SELECT id FROM v2_learning_packs ORDER BY created_at DESC").fetchall()]
    finally:
        conn.close()
    return [get_pack(x) for x in ids]


def create_card_version(card_id, data):
    """Create a candidate revision and optionally activate it without touching learning state."""
    conn = _conn()
    try:
        spec = conn.execute("SELECT * FROM v2_card_specs WHERE id=?", (card_id,)).fetchone()
        if not spec:
            return {"error": "卡片不存在"}
        claim_rows = conn.execute(
            "SELECT DISTINCT e.* FROM v2_evidence_claims e "
            "JOIN v2_pack_sources ps ON ps.source_id=e.source_id WHERE ps.pack_id=?",
            (spec["pack_id"],),
        ).fetchall()
        claims = [dict(x) for x in claim_rows]
        content = data.get("content") or {}
        claim_ids = data.get("claim_ids") or []
        candidate = {
            "concept_id": spec["concept_id"],
            "learning_objective": data.get("learning_objective") or spec["learning_objective"],
            "estimated_minutes": int(data.get("estimated_minutes") or spec["estimated_minutes"]),
            "claim_ids": claim_ids,
            "content": content,
        }
        from .validators import validate_card
        errors = validate_card(candidate, claims, allow_user_authored=data.get("provenance") == "user_authored")
        if errors:
            return {"error": "新版本未通过门禁", "issues": errors}
        version_number = conn.execute(
            "SELECT COALESCE(MAX(version_number),0)+1 FROM v2_card_versions WHERE card_spec_id=?", (card_id,)
        ).fetchone()[0]
        version_id = _id("version")
        ev_hash = evidence_hash([c for c in claims if c["id"] in claim_ids]) if claim_ids else "user-authored"
        conn.execute(
            "INSERT INTO v2_card_versions VALUES (?,?,?,?,?,?,?,?,?)",
            (version_id, card_id, version_number, json.dumps(content, ensure_ascii=False), ev_hash,
             data.get("generator_version") or "user-edit", json.dumps({"status": "passed", "issues": []}, ensure_ascii=False),
             "candidate", _now()),
        )
        for claim_id in sorted(set(claim_ids)):
            conn.execute("INSERT INTO v2_card_claims VALUES (?,?,?)", (version_id, claim_id, "body"))
        if data.get("publish"):
            conn.execute("UPDATE v2_card_versions SET status='superseded' WHERE card_spec_id=? AND status='active'", (card_id,))
            conn.execute("UPDATE v2_card_versions SET status='active' WHERE id=?", (version_id,))
            conn.execute("UPDATE v2_card_specs SET active_version_id=?, learning_objective=?, estimated_minutes=? WHERE id=?",
                         (version_id, candidate["learning_objective"], candidate["estimated_minutes"], card_id))
        conn.commit()
    finally:
        conn.close()
    pack = get_pack(spec["pack_id"])
    card = next(c for c in pack["cards"] if c["id"] == card_id)
    return {"success": True, "card": card, "published": bool(data.get("publish"))}


def complete_card(card_id):
    conn = _conn()
    try:
        card = conn.execute("SELECT * FROM v2_card_specs WHERE id=?", (card_id,)).fetchone()
        if not card or not card["active_version_id"]:
            return {"error": "卡片不存在或尚未发布"}
        now = _now()
        next_review = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d") if card["durable"] else None
        pack = conn.execute("SELECT * FROM v2_learning_packs WHERE id=?", (card["pack_id"],)).fetchone()
        conn.execute(
            "INSERT INTO v2_learning_states VALUES (?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(concept_id) DO UPDATE SET first_learned_at=COALESCE(first_learned_at,excluded.first_learned_at), next_review_at=excluded.next_review_at, updated_at=excluded.updated_at",
            (card["concept_id"], pack["goal_id"], now, next_review, 0, 2.5, 0, None, now),
        )
        total = conn.execute("SELECT COUNT(*) FROM v2_card_specs WHERE pack_id=?", (card["pack_id"],)).fetchone()[0]
        learned = conn.execute(
            "SELECT COUNT(*) FROM v2_card_specs c JOIN v2_learning_states s ON s.concept_id=c.concept_id "
            "WHERE c.pack_id=? AND s.first_learned_at IS NOT NULL", (card["pack_id"],),
        ).fetchone()[0]
        if learned >= total:
            conn.execute("UPDATE v2_learning_packs SET status='completed', updated_at=? WHERE id=?", (now, card["pack_id"]))
            if pack["milestone_id"]:
                conn.execute("UPDATE v2_milestones SET status='validated' WHERE id=?", (pack["milestone_id"],))
                next_milestone = conn.execute(
                    "SELECT id FROM v2_milestones WHERE goal_id=? AND position>(SELECT position FROM v2_milestones WHERE id=?) "
                    "AND status='planned' ORDER BY position LIMIT 1",
                    (pack["goal_id"], pack["milestone_id"]),
                ).fetchone()
                if next_milestone:
                    conn.execute("UPDATE v2_milestones SET status='active' WHERE id=?", (next_milestone["id"],))
                elif pack["goal_id"]:
                    conn.execute("UPDATE v2_goals SET status='completed', updated_at=? WHERE id=?", (now, pack["goal_id"]))
        conn.commit()
    finally:
        conn.close()
    return {"success": True, "pack": get_pack(card["pack_id"])}


def due_reviews(today=None, limit=20):
    today = today or datetime.now().strftime("%Y-%m-%d")
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT s.*, c.id card_id, c.learning_objective, c.active_version_id "
            "FROM v2_learning_states s JOIN v2_card_specs c ON c.concept_id=s.concept_id "
            "WHERE s.next_review_at IS NOT NULL AND s.next_review_at<=? ORDER BY s.next_review_at LIMIT ?",
            (today, limit),
        ).fetchall()
        return [_row(x) for x in rows]
    finally:
        conn.close()


def submit_review(data):
    concept_id = data.get("concept_id")
    result = 1 if data.get("result") in (1, True, "good") else 0
    conn = _conn()
    try:
        state = conn.execute("SELECT * FROM v2_learning_states WHERE concept_id=?", (concept_id,)).fetchone()
        if not state:
            return {"error": "学习目标不存在"}
        count = state["review_count"] or 0
        ease = state["ease"] or 2.5
        if result:
            count += 1
            interval = [1, 3, 7, 15, 30][min(count, 5) - 1]
            ease = min(2.5, ease + 0.1)
        else:
            count = 0
            interval = 1
            ease = max(1.3, ease - 0.2)
        next_review = (datetime.now() + timedelta(days=interval)).strftime("%Y-%m-%d")
        now = _now()
        conn.execute(
            "UPDATE v2_learning_states SET next_review_at=?, interval_days=?, ease=?, review_count=?, last_result=?, updated_at=? WHERE concept_id=?",
            (next_review, interval, ease, count, result, now, concept_id),
        )
        conn.execute(
            "INSERT INTO v2_assessments VALUES (?,?,?,?,?,?,?,?,?)",
            (_id("assessment"), concept_id, data.get("card_version_id"), data.get("assessment_type", "delayed"),
             data.get("question_version", "v1"), result, data.get("answer_text"), data.get("duration_seconds"), now),
        )
        conn.commit()
    finally:
        conn.close()
    return {"success": True, "next_review_at": next_review, "interval_days": interval, "review_count": count}


def today_plan(minutes=None):
    if minutes is None:
        conn = _conn()
        try:
            row = conn.execute(
                "SELECT daily_minutes FROM v2_goals WHERE status='active' ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
        finally:
            conn.close()
        minutes = row[0] if row else 90
    minutes = int(minutes)
    if not 60 <= minutes <= 120:
        return {"error": "每日时间必须在60-120分钟之间"}
    review_budget = round(minutes * 0.25)
    reviews = due_reviews(limit=100)
    picked_reviews, used_review = [], 0
    for item in reviews:
        if used_review + 3 > review_budget:
            break
        item["estimated_minutes"] = 3
        picked_reviews.append(item)
        used_review += 3
    remaining = minutes - used_review
    conn = _conn()
    try:
        active_goal_ids = {row[0] for row in conn.execute("SELECT id FROM v2_goals WHERE status='active'").fetchall()}
    finally:
        conn.close()
    available = list_packs("published")
    if active_goal_ids:
        available = [pack for pack in available if pack.get("goal_id") in active_goal_ids]
    packs = []
    for pack in available:
        if len(packs) >= 2:
            break
        if pack["estimated_minutes"] <= remaining:
            packs.append(pack)
            remaining -= pack["estimated_minutes"]
    return {
        "minute_budget": minutes,
        "estimated_minutes": minutes - remaining,
        "remaining_minutes": remaining,
        "reviews": picked_reviews,
        "packs": packs,
    }
