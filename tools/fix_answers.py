# -*- coding: utf-8 -*-
"""存量修复：think_answer <150 字的卡，用 AI 补全到 150-250 字。"""
import db, json, reader, prompts

api_key = reader.load_api_key()
client = reader.OpenAI(api_key=api_key, base_url=reader.DEEPSEEK_BASE_URL, timeout=60)

FIX_PROMPT = """你是 AI 学习卡片的思考题回答专家。请针对下面的思考题，结合卡片内容，写一个 150-250 字的专业回答（讲透要点 + 给 1 个具体例子）。

【卡片标题】{title}
【思考题】{q}
【卡片关键内容】{ctx}

只输出 JSON：{{"think_answer": "150-250字的专业回答"}}"""

cards = db.load_cards()
fixed = 0
for c in cards:
    if len(c.get("think_answer") or "") >= 150:
        continue
    q = c.get("think_question") or ""
    title = c.get("title") or ""
    ctx = (c.get("summary") or "")[:150] + " " + (c.get("body") or c.get("intuition") or "")[:200]
    if not q:
        continue
    try:
        resp = client.chat.completions.create(
            model="deepseek-chat",
            messages=[{"role": "system", "content": "只输出合法 JSON。"},
                      {"role": "user", "content": FIX_PROMPT.format(title=title, q=q, ctx=ctx)}],
            temperature=0.6,
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content)
        new_a = (data.get("think_answer") or "").strip()
        if len(new_a) < 150:
            continue
        # 更新 extra JSON 里的 think_answer
        conn = db._conn()
        try:
            row = conn.execute("SELECT extra FROM cards WHERE source_url=?", (c["source_url"],)).fetchone()
            if row and row["extra"]:
                extra = json.loads(row["extra"])
                extra["think_answer"] = new_a
                conn.execute("UPDATE cards SET extra=? WHERE source_url=?", (json.dumps(extra, ensure_ascii=False), c["source_url"]))
                conn.commit()
                fixed += 1
                print("  补全:", title[:30], "| 回答", len(c.get("think_answer") or 0), "->", len(new_a), "字", flush=True)
        finally:
            conn.close()
    except Exception as e:
        print("  失败:", title[:30], str(e)[:60], flush=True)

print("共补全 %d 张" % fixed, flush=True)
