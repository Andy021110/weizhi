import db, json

def score(c):
    s = 0
    for f in ("think_question", "think_answer", "quiz", "review_quiz"):
        if c.get(f):
            s += 1
    if c.get("open_question"):
        s += 1
    return s

cards = db.load_cards()
golden = {}
for t, label in [("t1_vocab","词汇"),("t2_reading","精读"),("t3_math","数学"),("t4_trivia","通识"),("t5_skill","技能"),("t6_code","代码")]:
    cs = [c for c in cards if (c.get("_meta") or {}).get("template") == t]
    cs.sort(key=lambda c: (score(c), len(c.get("think_answer") or ""), len(c.get("body") or "") + len(c.get("intuition") or "")), reverse=True)
    # 取每类 2 张字段最完整的
    samples = cs[:2]
    if not samples:
        continue
    golden[t] = {"label": label, "samples": []}
    for c in samples:
        c2 = dict(c)
        c2.pop("_meta", None)
        c2.pop("id", None)
        golden[t]["samples"].append(c2)
    print("%s: 从 %d 张中选 %d 张（评分 %s）" % (label, len(cs), len(samples), [score(c) for c in samples]))
json.dump(golden, open("/tmp/golden_set.json","w"), ensure_ascii=False, indent=2)
print("golden_set.json 已更新")
