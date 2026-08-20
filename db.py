# -*- coding: utf-8 -*-
"""微知 · SQLite 数据层（Python 标准库 sqlite3）。

线程安全：每个操作打开独立连接，并设置 busy timeout，避免多线程下
「database is locked」。cards 的 source_url 唯一，用于去重。
core_points / open_question / quiz 以 JSON 字符串存储，读写时自动
dumps / loads 还原为 list / dict。
"""
import json
import os
import sqlite3
from datetime import datetime, timedelta

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "weizhi.db")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS cards (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_url TEXT UNIQUE,
  title TEXT, summary TEXT, body TEXT,
  core_points TEXT,        -- JSON 数组字符串
  think_question TEXT,
  open_question TEXT,      -- JSON 对象字符串
  quiz TEXT,               -- JSON 数组字符串
  difficulty TEXT, source TEXT, category TEXT,
  template TEXT, generated_at TEXT, date TEXT
);

CREATE TABLE IF NOT EXISTS progress (
  date TEXT, card_source_url TEXT,
  done INTEGER DEFAULT 1,
  PRIMARY KEY (date, card_source_url)
);

CREATE TABLE IF NOT EXISTS user_state (
  key TEXT PRIMARY KEY, value TEXT
);

CREATE TABLE IF NOT EXISTS plans (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  title TEXT,
  topic TEXT,
  source TEXT,
  duration TEXT,
  total_cards INTEGER DEFAULT 0,
  cards_per_day INTEGER DEFAULT 3,
  scale INTEGER DEFAULT 20,
  pace INTEGER DEFAULT 3,
  batch INTEGER DEFAULT 1,
  template TEXT,
  status TEXT DEFAULT 'active',
  created_at TEXT,
  last_studied_at TEXT
);

CREATE TABLE IF NOT EXISTS reviews (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  card_source_url TEXT,
  review_date TEXT,
  result TEXT,
  interval_before INTEGER,
  interval_after INTEGER
);

CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  date TEXT,
  event TEXT,
  source_url TEXT
);

CREATE TABLE IF NOT EXISTS notifications (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  date TEXT,
  type TEXT,
  title TEXT,
  body TEXT,
  level TEXT DEFAULT 'info',
  read INTEGER DEFAULT 0,
  created_at TEXT
);
"""


def _conn():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    """建表（幂等）+ 增量迁移。"""
    conn = _conn()
    try:
        conn.executescript(_SCHEMA)
        conn.commit()
    finally:
        conn.close()
    _migrate()


def _migrate():
    """增量迁移：给 cards / plans 表补字段（老库升级用）。"""
    conn = _conn()
    try:
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(cards)").fetchall()]
        for col, ddl in (
            ("plan_id", "ALTER TABLE cards ADD COLUMN plan_id INTEGER"),
            ("plan_index", "ALTER TABLE cards ADD COLUMN plan_index INTEGER"),
            ("plan_total", "ALTER TABLE cards ADD COLUMN plan_total INTEGER"),
            ("group_index", "ALTER TABLE cards ADD COLUMN group_index INTEGER"),
            ("batch_index", "ALTER TABLE cards ADD COLUMN batch_index INTEGER"),
            ("memory_state", "ALTER TABLE cards ADD COLUMN memory_state TEXT"),
            ("next_review_at", "ALTER TABLE cards ADD COLUMN next_review_at TEXT"),
            ("review_count", "ALTER TABLE cards ADD COLUMN review_count INTEGER DEFAULT 0"),
            ("ease", "ALTER TABLE cards ADD COLUMN ease REAL DEFAULT 2.5"),
            ("interval_days", "ALTER TABLE cards ADD COLUMN interval_days INTEGER DEFAULT 0"),
            ("favorite", "ALTER TABLE cards ADD COLUMN favorite INTEGER DEFAULT 0"),
            ("extra", "ALTER TABLE cards ADD COLUMN extra TEXT"),
        ):
            if col not in cols:
                conn.execute(ddl)

        # plans 表补规模档/节奏档/批次/类型字段
        pcols = [r["name"] for r in conn.execute("PRAGMA table_info(plans)").fetchall()]
        for col, ddl in (
            ("scale", "ALTER TABLE plans ADD COLUMN scale INTEGER DEFAULT 20"),
            ("pace", "ALTER TABLE plans ADD COLUMN pace INTEGER DEFAULT 3"),
            ("batch", "ALTER TABLE plans ADD COLUMN batch INTEGER DEFAULT 1"),
            ("template", "ALTER TABLE plans ADD COLUMN template TEXT"),
        ):
            if col not in pcols:
                conn.execute(ddl)
        conn.commit()
    finally:
        conn.close()


def _dump(v):
    return json.dumps(v, ensure_ascii=False) if v is not None else None


def _load(v):
    if v is None or v == "":
        return None
    try:
        return json.loads(v)
    except (json.JSONDecodeError, TypeError):
        return v


def save_card(card, date=None):
    """插入卡片，source_url 冲突则忽略（去重）。返回是否插入成功。

    date 为空时用今天；migrate 场景传文件名里的日期。
    """
    if not isinstance(card, dict):
        return False
    meta = card.get("_meta") or {}
    date_str = date or datetime.now().strftime("%Y-%m-%d")

    source_url = card.get("source_url")
    title = card.get("title")
    summary = card.get("summary")
    body = card.get("body")
    core_points = _dump(card.get("core_points"))
    think_question = card.get("think_question")
    open_question = _dump(card.get("open_question"))
    quiz = _dump(card.get("quiz"))
    difficulty = card.get("difficulty")
    source = card.get("source")
    category = meta.get("category") or card.get("category")
    template = meta.get("template") or card.get("template")
    generated_at = meta.get("generated_at") or card.get("generated_at")
    plan_id = card.get("plan_id")
    plan_index = card.get("plan_index")
    plan_total = card.get("plan_total")
    group_index = card.get("group_index")
    batch_index = card.get("batch_index")

    # 额外字段（各模板专属字段，如 word/words/intuition/hook/steps 等）存 extra JSON
    fixed = {"source_url", "title", "summary", "body", "core_points", "think_question",
             "open_question", "quiz", "difficulty", "source", "category", "template",
             "generated_at", "date", "plan_id", "plan_index", "plan_total",
             "group_index", "batch_index", "_meta", "_date"}
    extra = {k: v for k, v in card.items() if k not in fixed and v not in (None, "")}
    extra_json = _dump(extra) if extra else None

    conn = _conn()
    try:
        cur = conn.execute(
            """INSERT OR IGNORE INTO cards
               (source_url, title, summary, body, core_points, think_question,
                open_question, quiz, difficulty, source, category, template,
                generated_at, date, plan_id, plan_index, plan_total,
                group_index, batch_index, extra)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (source_url, title, summary, body, core_points, think_question,
             open_question, quiz, difficulty, source, category, template,
             generated_at, date_str, plan_id, plan_index, plan_total,
             group_index, batch_index, extra_json),
        )
        conn.commit()
        return cur.rowcount == 1
    finally:
        conn.close()


def _row_to_card(row):
    d = dict(row)
    d["core_points"] = _load(d.get("core_points")) or []
    d["open_question"] = _load(d.get("open_question"))
    d["quiz"] = _load(d.get("quiz")) or []
    # 还原 extra 字段（各模板专属字段，如 word/words/intuition/hook/steps 等）
    extra = _load(d.pop("extra", None))
    if isinstance(extra, dict):
        d.update(extra)
    # 还原前端依赖的 _meta 与 _date 字段，保证阅读器展示不变
    d["_meta"] = {
        "category": d.get("category"),
        "template": d.get("template"),
        "generated_at": d.get("generated_at"),
    }
    d["_date"] = d.get("date")
    return d


def load_cards(date=None):
    """读卡片。date 为空读全部，按 date 倒序；否则只读指定日期。"""
    conn = _conn()
    try:
        if date:
            rows = conn.execute(
                "SELECT * FROM cards WHERE date = ? ORDER BY date DESC, id DESC",
                (date,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM cards ORDER BY date DESC, id DESC"
            ).fetchall()
    finally:
        conn.close()
    return [_row_to_card(r) for r in rows]


def list_dates():
    """返回所有不重复的 date，倒序。"""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT DISTINCT date FROM cards WHERE date IS NOT NULL ORDER BY date DESC"
        ).fetchall()
    finally:
        conn.close()
    return [r["date"] for r in rows]


def mark_done(date, source_url):
    """记一条完成记录 + 任务卡首次完成时初始化复习状态。"""
    if not date or not source_url:
        return False
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    conn = _conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO progress (date, card_source_url, done) VALUES (?, ?, 1)",
            (date, source_url),
        )
        # 只有任务卡（plan_id 非空）才进复习队列；散卡学完即过
        conn.execute(
            """UPDATE cards SET
               memory_state = COALESCE(memory_state, 'learning'),
               next_review_at = COALESCE(next_review_at, ?),
               interval_days = CASE WHEN interval_days IS NULL OR interval_days = 0 THEN 1 ELSE interval_days END
               WHERE source_url = ? AND memory_state IS NULL AND plan_id IS NOT NULL""",
            (tomorrow, source_url),
        )
        # 任务卡学完全部 → 计划标记完成（前端隐藏）
        row = conn.execute("SELECT plan_id FROM cards WHERE source_url = ?", (source_url,)).fetchone()
        if row and row["plan_id"]:
            _maybe_complete_plan(conn, row["plan_id"])
        conn.commit()
        return True
    finally:
        conn.close()


def _maybe_complete_plan(conn, plan_id):
    """该计划所有卡都学过（progress 有记录）→ status='done'。"""
    total = conn.execute("SELECT COUNT(*) FROM cards WHERE plan_id = ?", (plan_id,)).fetchone()[0]
    if total <= 0:
        return
    done = conn.execute(
        """SELECT COUNT(DISTINCT p.card_source_url) FROM progress p
           JOIN cards c ON c.source_url = p.card_source_url
           WHERE c.plan_id = ?""",
        (plan_id,),
    ).fetchone()[0]
    if done >= total:
        conn.execute("UPDATE plans SET status = 'done' WHERE id = ? AND status != 'done'", (plan_id,))


def get_done_dates():
    """返回所有已完成的 {date: [source_url, ...]}。"""
    conn = _conn()
    try:
        rows = conn.execute("SELECT date, card_source_url FROM progress").fetchall()
    finally:
        conn.close()
    result = {}
    for r in rows:
        result.setdefault(r["date"], []).append(r["card_source_url"])
    return result


def get_user_state(key):
    """取用户状态（返回字符串，未取到返回 None）。"""
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT value FROM user_state WHERE key = ?", (key,)
        ).fetchone()
    finally:
        conn.close()
    return row["value"] if row else None


def set_user_state(key, value):
    """存用户状态（value 以字符串保存）。"""
    conn = _conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO user_state (key, value) VALUES (?, ?)",
            (key, "" if value is None else str(value)),
        )
        conn.commit()
    finally:
        conn.close()


def get_all_state():
    """返回全部用户状态 {key: value}。"""
    conn = _conn()
    try:
        rows = conn.execute("SELECT key, value FROM user_state").fetchall()
    finally:
        conn.close()
    return {r["key"]: r["value"] for r in rows}


def delete_card(source_url):
    """删除一张卡片（按 source_url），同时清理其完成记录。返回是否删除成功。"""
    if not source_url:
        return False
    conn = _conn()
    try:
        conn.execute("DELETE FROM cards WHERE source_url = ?", (source_url,))
        conn.execute("DELETE FROM progress WHERE card_source_url = ?", (source_url,))
        conn.commit()
        return conn.total_changes > 0
    finally:
        conn.close()


def update_card(source_url, fields):
    """编辑卡片字段（title/summary/body/core_points/quiz 白名单）。返回是否成功。"""
    if not source_url:
        return False
    sets = []
    vals = []
    for k in ("title", "summary", "body"):
        if k in fields:
            sets.append(f"{k} = ?")
            vals.append(fields[k])
    if "core_points" in fields:
        sets.append("core_points = ?")
        vals.append(_dump(fields["core_points"]))
    if "quiz" in fields:
        sets.append("quiz = ?")
        vals.append(_dump(fields["quiz"]))
    if not sets:
        return False
    vals.append(source_url)
    conn = _conn()
    try:
        conn.execute(f"UPDATE cards SET {', '.join(sets)} WHERE source_url = ?", vals)
        conn.commit()
        return True
    finally:
        conn.close()


# ===== 计划（plans）=====

def save_plan(plan):
    """插入或更新计划。plan 含 id 时更新，否则插入。返回 plan_id。"""
    conn = _conn()
    try:
        if plan.get("id"):
            conn.execute(
                """UPDATE plans SET title=?, topic=?, source=?, duration=?,
                   total_cards=?, cards_per_day=?, scale=?, pace=?, batch=?, template=?,
                   status=?, last_studied_at=? WHERE id=?""",
                (plan.get("title"), plan.get("topic"), plan.get("source"),
                 plan.get("duration"), plan.get("total_cards"), plan.get("cards_per_day"),
                 plan.get("scale"), plan.get("pace"), plan.get("batch"),
                 plan.get("template"), plan.get("status"), plan.get("last_studied_at"),
                 plan.get("id")),
            )
            conn.commit()
            return plan.get("id")
        cur = conn.execute(
            """INSERT INTO plans (title, topic, source, duration, total_cards,
               cards_per_day, scale, pace, batch, template, status, created_at, last_studied_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (plan.get("title"), plan.get("topic"), plan.get("source", "user"),
             plan.get("duration", "long"), plan.get("total_cards", 0),
             plan.get("cards_per_day", 3), plan.get("scale", 20), plan.get("pace", 3),
             plan.get("batch", 1), plan.get("template"), plan.get("status", "active"),
             datetime.now().isoformat(), plan.get("last_studied_at")),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def load_plans():
    """返回所有计划，按创建时间倒序。"""
    conn = _conn()
    try:
        rows = conn.execute("SELECT * FROM plans ORDER BY id DESC").fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def load_plan_cards(plan_id):
    """返回某计划的所有卡，按 plan_index 排序（用于任务翻卡视图）。"""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM cards WHERE plan_id = ? ORDER BY plan_index ASC, id ASC",
            (plan_id,),
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_card(r) for r in rows]


def get_plan(plan_id):
    """返回单个计划。"""
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def update_plan(plan_id, fields):
    """更新计划的可编辑字段（title/cards_per_day/status 白名单）。"""
    allowed = {"title", "cards_per_day", "status"}
    sets = []
    vals = []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k} = ?")
            vals.append(v)
    if not sets:
        return False
    vals.append(plan_id)
    conn = _conn()
    try:
        conn.execute(f"UPDATE plans SET {', '.join(sets)} WHERE id = ?", vals)
        conn.commit()
        return True
    finally:
        conn.close()


def delete_plan(plan_id):
    """删除计划（卡片保留，仅解除计划关联）。"""
    conn = _conn()
    try:
        conn.execute("DELETE FROM plans WHERE id = ?", (plan_id,))
        conn.execute(
            "UPDATE cards SET plan_id = NULL, plan_index = NULL, plan_total = NULL WHERE plan_id = ?",
            (plan_id,),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def set_plan_studied(plan_id):
    """更新计划最近学习时间。"""
    conn = _conn()
    try:
        conn.execute("UPDATE plans SET last_studied_at = ? WHERE id = ?",
                     (datetime.now().isoformat(), plan_id))
        conn.commit()
    finally:
        conn.close()


def count_plan_done(plan_id):
    """统计某计划已学完的卡数（在 progress 里有完成记录的卡）。"""
    conn = _conn()
    try:
        rows = conn.execute("SELECT source_url FROM cards WHERE plan_id = ?", (plan_id,)).fetchall()
        urls = [r["source_url"] for r in rows]
    finally:
        conn.close()
    if not urls:
        return 0
    conn = _conn()
    try:
        placeholders = ",".join("?" for _ in urls)
        row = conn.execute(
            f"SELECT COUNT(DISTINCT card_source_url) FROM progress WHERE card_source_url IN ({placeholders})",
            urls,
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else 0


# ===== 复习（SRS / SM-2）=====

def get_due_reviews(today, limit=20):
    """返回今天到期的复习卡（next_review_at <= today 且未掌握）。"""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM cards WHERE next_review_at IS NOT NULL AND next_review_at <= ? "
            "AND (memory_state IS NULL OR memory_state != 'mastered') "
            "ORDER BY next_review_at ASC, id ASC LIMIT ?",
            (today, limit),
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_card(r) for r in rows]


def schedule_review(source_url, quality):
    """复习一张卡，quality: 0=忘记, 1=记得。更新 SM-2 状态并返回新状态。"""
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM cards WHERE source_url = ?", (source_url,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    card = dict(row)
    ease = card.get("ease") or 2.5
    interval = card.get("interval_days") or 0
    review_count = card.get("review_count") or 0

    interval_before = interval
    if quality == 0:
        interval = 1
        review_count = 0
        ease = max(1.3, ease - 0.2)
    else:
        review_count += 1
        interval = [1, 3, 7, 15, 30][min(review_count, 5) - 1]
        ease = min(2.5, ease + 0.1)

    memory_state = "mastered" if review_count >= 5 else "learning"
    next_review = (datetime.now() + timedelta(days=interval)).strftime("%Y-%m-%d")

    conn = _conn()
    try:
        conn.execute(
            "UPDATE cards SET memory_state=?, next_review_at=?, review_count=?, ease=?, interval_days=? WHERE source_url=?",
            (memory_state, next_review, review_count, ease, interval, source_url),
        )
        conn.execute(
            "INSERT INTO reviews (card_source_url, review_date, result, interval_before, interval_after) VALUES (?, ?, ?, ?, ?)",
            (source_url, datetime.now().strftime("%Y-%m-%d"),
             "good" if quality == 1 else "again", interval_before, interval),
        )
        conn.commit()
    finally:
        conn.close()
    return {"memory_state": memory_state, "interval_days": interval,
            "review_count": review_count, "ease": ease, "next_review_at": next_review}


def mark_mastered(source_url):
    """手动标记已掌握（跳过后续复习）。"""
    conn = _conn()
    try:
        conn.execute(
            "UPDATE cards SET memory_state='mastered', next_review_at=NULL, interval_days=30, review_count=5 WHERE source_url=?",
            (source_url,),
        )
        conn.commit()
    finally:
        conn.close()


def skip_review_today(source_url):
    """跳过今天（推到明天）。"""
    tomorrow = (datetime.now() + timedelta(days=1)).strftime("%Y-%m-%d")
    conn = _conn()
    try:
        conn.execute(
            "UPDATE cards SET next_review_at=? WHERE source_url=?",
            (tomorrow, source_url),
        )
        conn.commit()
    finally:
        conn.close()


def review_stats():
    """复习统计：复习中总数、已掌握。"""
    conn = _conn()
    try:
        total = conn.execute("SELECT COUNT(*) FROM cards WHERE memory_state IS NOT NULL").fetchone()[0]
        mastered = conn.execute("SELECT COUNT(*) FROM cards WHERE memory_state='mastered'").fetchone()[0]
    finally:
        conn.close()
    return {"total_reviewing": total, "mastered": mastered}


# ===== 搜索 / 收藏 / 统计 =====

def search_cards(q, limit=50):
    """按关键词搜标题/正文/摘要。"""
    if not q:
        return []
    like = f"%{q}%"
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM cards WHERE title LIKE ? OR body LIKE ? OR summary LIKE ? ORDER BY date DESC, id DESC LIMIT ?",
            (like, like, like, limit),
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_card(r) for r in rows]


def toggle_favorite(source_url, favorite):
    """收藏/取消收藏。"""
    if not source_url:
        return False
    conn = _conn()
    try:
        conn.execute("UPDATE cards SET favorite = ? WHERE source_url = ?", (1 if favorite else 0, source_url))
        conn.commit()
        return True
    finally:
        conn.close()


def load_favorites():
    """返回收藏的卡。"""
    conn = _conn()
    try:
        rows = conn.execute("SELECT * FROM cards WHERE favorite = 1 ORDER BY date DESC, id DESC").fetchall()
    finally:
        conn.close()
    return [_row_to_card(r) for r in rows]


def stats():
    """整体统计。"""
    conn = _conn()
    try:
        total_cards = conn.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
        total_dates = conn.execute("SELECT COUNT(DISTINCT date) FROM cards WHERE date IS NOT NULL").fetchone()[0]
        total_done = conn.execute("SELECT COUNT(DISTINCT card_source_url) FROM progress").fetchone()[0]
        reviewing = conn.execute("SELECT COUNT(*) FROM cards WHERE memory_state IS NOT NULL AND memory_state != 'mastered'").fetchone()[0]
        mastered = conn.execute("SELECT COUNT(*) FROM cards WHERE memory_state='mastered'").fetchone()[0]
        total_reviews = conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0]
    finally:
        conn.close()
    return {
        "total_cards": total_cards,
        "total_dates": total_dates,
        "total_done": total_done,
        "reviewing": reviewing,
        "mastered": mastered,
        "total_reviews": total_reviews,
    }


# ===== 质量巡检 M1：daily_check.py / /api/regen / /api/report 用 =====

def get_card(source_url):
    """按 source_url 读单卡（含 extra 合并），找不到返回 None。"""
    if not source_url:
        return None
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM cards WHERE source_url = ?", (source_url,)).fetchone()
    finally:
        conn.close()
    return _row_to_card(row) if row else None


def update_card_extra(source_url, key, value):
    """更新卡片 extra JSON 中的某个字段（value=None 表示删除该字段）。返回是否成功。"""
    if not source_url:
        return False
    conn = _conn()
    try:
        row = conn.execute("SELECT extra FROM cards WHERE source_url = ?", (source_url,)).fetchone()
        if not row:
            return False
        extra = _load(row["extra"]) or {}
        if value is None:
            extra.pop(key, None)
        else:
            extra[key] = value
        conn.execute(
            "UPDATE cards SET extra = ? WHERE source_url = ?",
            (_dump(extra) if extra else None, source_url),
        )
        conn.commit()
        return True
    finally:
        conn.close()


def load_cards_since(dt_str):
    """返回 generated_at >= dt_str 的卡（巡检候选用），按 id 倒序。"""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT * FROM cards WHERE generated_at IS NOT NULL AND generated_at >= ? ORDER BY id DESC",
            (dt_str,),
        ).fetchall()
    finally:
        conn.close()
    return [_row_to_card(r) for r in rows]


def find_duplicate(template, title, exclude_source_url=None):
    """同模板同标题的卡是否已存在。返回重复卡的 source_url，无则 None。"""
    if not title:
        return None
    conn = _conn()
    try:
        if exclude_source_url:
            rows = conn.execute(
                "SELECT source_url FROM cards WHERE template = ? AND title = ? AND source_url != ? LIMIT 1",
                (template, title, exclude_source_url),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT source_url FROM cards WHERE template = ? AND title = ? LIMIT 1",
                (template, title),
            ).fetchall()
    finally:
        conn.close()
    return rows[0]["source_url"] if rows else None


def study_on_date(date):
    """某日学习量：progress 表当日条数（含散卡+任务卡，统一口径）。"""
    conn = _conn()
    try:
        row = conn.execute("SELECT COUNT(*) FROM progress WHERE date = ?", (date,)).fetchone()
    finally:
        conn.close()
    return row[0] if row else 0


def count_due_on_date(date):
    """某日到期卡数：next_review_at == date 且未掌握（精确匹配口径）。"""
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM cards WHERE next_review_at = ? AND (memory_state IS NULL OR memory_state != 'mastered')",
            (date,),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else 0


def reviewed_on_date(date):
    """某日复习卡数（去重卡）。"""
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT COUNT(DISTINCT card_source_url) FROM reviews WHERE review_date = ?",
            (date,),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else 0


def review_accuracy_on_date(date):
    """某日复习正确率：good 记录 / 总记录。返回 (比率, 总次数)，无记录返回 (None, 0)。"""
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN result = 'good' THEN 1 ELSE 0 END) FROM reviews WHERE review_date = ?",
            (date,),
        ).fetchone()
    finally:
        conn.close()
    total = row[0] if row else 0
    good = row[1] or 0
    return (round(good / total, 3) if total else None, total)


def compliance_on_date(date):
    """到期遵守度（口径：当日复习去重卡数 / 当日到期卡数）。到期 = next_review_at <= date 且未掌握。"""
    conn = _conn()
    try:
        due = conn.execute(
            "SELECT COUNT(*) FROM cards WHERE next_review_at <= ? AND (memory_state IS NULL OR memory_state != 'mastered')",
            (date,),
        ).fetchone()[0]
        done = conn.execute(
            "SELECT COUNT(DISTINCT card_source_url) FROM reviews WHERE review_date = ?",
            (date,),
        ).fetchone()[0]
    finally:
        conn.close()
    return (round(done / due, 3) if due else None, done, due)


def mastery_rate():
    """掌握率：mastered / 进入复习队列的卡总数。返回 (比率, 已掌握, 总数)。"""
    conn = _conn()
    try:
        total = conn.execute("SELECT COUNT(*) FROM cards WHERE memory_state IS NOT NULL").fetchone()[0]
        mastered = conn.execute("SELECT COUNT(*) FROM cards WHERE memory_state='mastered'").fetchone()[0]
    finally:
        conn.close()
    return (round(mastered / total, 3) if total else None, mastered, total)


# ===== M2 前端埋点（events 表）=====

def record_event(date, event, source_url=None):
    """记一条前端埋点事件（think_open/open_submit/calendar_open/search_use）。"""
    if not event:
        return
    conn = _conn()
    try:
        conn.execute(
            "INSERT INTO events (date, event, source_url) VALUES (?, ?, ?)",
            (date or datetime.now().strftime("%Y-%m-%d"), event, source_url or ""),
        )
        conn.commit()
    finally:
        conn.close()


def events_on_date(date):
    """某日各事件次数 {event: count}。"""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT event, COUNT(*) AS n FROM events WHERE date = ? GROUP BY event",
            (date,),
        ).fetchall()
    finally:
        conn.close()
    return {r["event"]: r["n"] for r in rows}


# ===== M3 自动修复：备份 / 回滚辅助 =====

def progress_dates_of(source_url):
    """某卡的所有完成记录日期（备份用，供回滚恢复）。"""
    if not source_url:
        return []
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT date FROM progress WHERE card_source_url = ?", (source_url,)
        ).fetchall()
    finally:
        conn.close()
    return [r["date"] for r in rows]


def restore_progress(date, source_url):
    """回滚时恢复一条完成记录。"""
    if not date or not source_url:
        return
    conn = _conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO progress (date, card_source_url, done) VALUES (?, ?, 1)",
            (date, source_url),
        )
        conn.commit()
    finally:
        conn.close()


# ===== M3 程序内通知（daily_agent 产出，前端铃铛/弹窗展示）=====

def add_notification(n_type, title, body, level="info", date=None):
    """写一条通知。返回通知 id。"""
    now = datetime.now()
    conn = _conn()
    try:
        cur = conn.execute(
            "INSERT INTO notifications (date, type, title, body, level, read, created_at) VALUES (?, ?, ?, ?, ?, 0, ?)",
            (date or now.strftime("%Y-%m-%d"), n_type, title, body, level, now.isoformat()),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_notifications(limit=50, unread_only=False):
    """通知列表，倒序。unread_only=True 只返回未读。"""
    conn = _conn()
    try:
        if unread_only:
            rows = conn.execute(
                "SELECT * FROM notifications WHERE read = 0 ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM notifications ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def count_unread_notifications():
    """未读通知数。"""
    conn = _conn()
    try:
        row = conn.execute("SELECT COUNT(*) FROM notifications WHERE read = 0").fetchone()
    finally:
        conn.close()
    return row[0] if row else 0


def mark_notifications_read(ids=None):
    """标记已读。ids=None 全部已读；否则只标指定的 id 列表。"""
    conn = _conn()
    try:
        if ids:
            placeholders = ",".join("?" for _ in ids)
            conn.execute(
                f"UPDATE notifications SET read = 1 WHERE id IN ({placeholders})",
                ids,
            )
        else:
            conn.execute("UPDATE notifications SET read = 1 WHERE read = 0")
        conn.commit()
    finally:
        conn.close()
