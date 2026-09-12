"""Small HTTP-independent dispatcher used by reader.py and tests."""
from . import storage
from .planner import plan_pack
from .source_processor import process_source


def handle_get(path, query):
    if path == "/api/v2/goals":
        return {"goals": storage.list_goals()}
    if path == "/api/v2/sources":
        return {"sources": storage.list_sources()}
    if path == "/api/v2/packs":
        pack_id = (query.get("id") or [None])[0]
        if pack_id:
            return {"pack": storage.get_pack(pack_id)}
        status = (query.get("status") or [None])[0]
        return {"packs": storage.list_packs(status)}
    if path == "/api/v2/today":
        value = (query.get("minutes") or [None])[0]
        minutes = int(value) if value is not None else None
        return storage.today_plan(minutes)
    if path == "/api/v2/reviews/due":
        return {"reviews": storage.due_reviews()}
    return None


def handle_post(path, data):
    if path == "/api/v2/goals":
        return storage.create_goal(data)
    if path == "/api/v2/goals/status":
        return storage.set_goal_status(data.get("goal_id"), data.get("status"))
    if path == "/api/v2/sources":
        return storage.create_source(data)
    if path == "/api/v2/sources/process":
        return process_source(data.get("source_id"), force=bool(data.get("force")))
    if path == "/api/v2/packs":
        return storage.create_pack(data)
    if path == "/api/v2/packs/plan":
        return plan_pack(data)
    if path == "/api/v2/packs/publish":
        return storage.publish_pack(data.get("pack_id"))
    if path == "/api/v2/packs/reject":
        return storage.reject_pack(data.get("pack_id"))
    if path == "/api/v2/cards/complete":
        return storage.complete_card(data.get("card_id"))
    if path == "/api/v2/cards/versions":
        return storage.create_card_version(data.get("card_id"), data)
    if path == "/api/v2/reviews":
        return storage.submit_review(data)
    return None
