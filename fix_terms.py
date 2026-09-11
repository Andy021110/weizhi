import db, json, prompts

conn = db._conn()
try:
    fixed = 0
    rows = conn.execute("SELECT source_url, extra FROM cards WHERE template = ?", ("t1_vocab",)).fetchall()
    for r in rows:
        if not r["extra"]:
            continue
        try:
            extra = json.loads(r["extra"])
        except Exception:
            continue
        word = (extra.get("word") or "").strip().lower()
        if word in prompts.AI_TERMS:
            old = extra.get("definition_cn", "")
            extra["definition_cn"] = prompts.AI_TERMS[word]["definition"]
            conn.execute("UPDATE cards SET extra = ? WHERE source_url = ?", (json.dumps(extra, ensure_ascii=False), r["source_url"]))
            fixed += 1
            print("  修复:", word, "| 旧:", old[:28], "-> 新:", prompts.AI_TERMS[word]["definition"][:28])
    conn.commit()
finally:
    conn.close()
print("共修复", fixed, "张卡")
