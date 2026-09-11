import db, datetime

# 取一张 T3 数学卡做测试
cards = db.load_cards()
c = None
for x in cards:
    if (x.get("_meta") or {}).get("template") == "t3_math":
        c = x
        break
if not c:
    c = cards[0]
src = c["source_url"]
print("测试卡:", c.get("title") or c.get("word"))

# 模拟「学习」：标记 learning，到期时间设为今天
today = datetime.datetime.now().strftime("%Y-%m-%d")
conn = db._conn()
conn.execute(
    "UPDATE cards SET memory_state=?, next_review_at=?, interval_days=?, review_count=?, ease=? WHERE source_url=?",
    ("learning", today, 1, 0, 2.5, src),
)
conn.commit()
conn.close()

# 验证到期队列
due = db.get_due_reviews(today)
print("今日到期复习卡数:", len(due), "| 含测试卡:", any(d["source_url"] == src for d in due))

# 连续答对 6 次，看 SM-2 间隔
print("=== 连续答对（quality=1）===")
for i in range(6):
    r = db.schedule_review(src, 1)
    print("第%d次: interval=%s天 review_count=%s state=%s next=%s" % (
        i + 1, r["interval_days"], r["review_count"], r["memory_state"], r["next_review_at"]))

# 答错一次，看回退
print("=== 答错一次（quality=0）===")
r = db.schedule_review(src, 0)
print("答错后: interval=%s天 review_count=%s state=%s ease=%s" % (
    r["interval_days"], r["review_count"], r["memory_state"], r["ease"]))
