import db, json
cards = db.load_cards()
plans = db.load_plans()
out = {"scatter": [], "plans": []}
by_tpl = {}
for c in cards:
    if c.get("plan_id"):
        continue
    t = (c.get("_meta") or {}).get("template") or "?"
    by_tpl.setdefault(t, []).append(c)
for t in sorted(by_tpl):
    for c in by_tpl[t]:
        c2 = dict(c)
        c2["template"] = (c.get("_meta") or {}).get("template")
        c2["category"] = (c.get("_meta") or {}).get("category")
        c2.pop("_meta", None)
        out["scatter"].append(c2)
for p in plans:
    pcs = db.load_plan_cards(p["id"])
    if not pcs:
        continue
    pcs.sort(key=lambda c: (c.get("plan_index") or 0))
    p2 = dict(p)
    p2["cards"] = []
    for c in pcs:
        c2 = dict(c)
        c2["template"] = (c.get("_meta") or {}).get("template")
        c2["category"] = (c.get("_meta") or {}).get("category")
        c2.pop("_meta", None)
        p2["cards"].append(c2)
    out["plans"].append(p2)
json.dump(out, open("/tmp/weizhi_cases2.json", "w"), ensure_ascii=False)
print("OK", len(cards), "cards,", len(plans), "plans")
