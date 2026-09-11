import db, json
cards = db.load_cards()
golden = {}
for t, label in [("t1_vocab","词汇"),("t2_reading","精读"),("t3_math","数学"),("t4_trivia","通识"),("t5_skill","技能")]:
    cs = [c for c in cards if (c.get("_meta") or {}).get("template")==t]
    if not cs: continue
    c = dict(cs[0])
    c.pop("_meta", None)
    c.pop("id", None)
    golden[t] = {"label": label, "sample": c}
json.dump(golden, open("/tmp/golden_set.json","w"), ensure_ascii=False, indent=2)
print("OK", len(golden), "类 golden set 已导出")
for t in golden:
    c = golden[t]["sample"]
    print(" ", t, "|", c.get("title") or c.get("word"), "| think:", bool(c.get("think_question")), "| open:", bool(c.get("open_question")))
