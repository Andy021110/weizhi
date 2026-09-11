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

-- ===== 以下为 v2 侧轨表（M1 起）。与 v1 表零耦合，删表即回滚 =====

CREATE TABLE IF NOT EXISTS v2_sources (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  url TEXT UNIQUE,
  title TEXT,
  site TEXT,
  lang TEXT,
  content_hash TEXT,      -- 清洗后文本哈希，用于快照去重与变更检测
  snapshot_path TEXT,
  clean_text TEXT,
  fetched_at TEXT,
  meta TEXT               -- JSON 字符串
);

CREATE TABLE IF NOT EXISTS v2_claims (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  source_id INTEGER,
  claim_idx INTEGER,
  text TEXT,
  start INTEGER,          -- 在 clean_text 中的偏移，保证 text == clean[start:end]
  end INTEGER,
  kind TEXT,              -- fact / number / definition / step
  usable INTEGER DEFAULT 1,
  meta TEXT               -- JSON 字符串
);

CREATE TABLE IF NOT EXISTS v2_model_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  ts TEXT,
  task TEXT,
  provider TEXT,
  model TEXT,
  prompt_version TEXT,
  input_hash TEXT,        -- 幂等键：同 hash 命中缓存，不重复调用
  output TEXT,
  latency_ms INTEGER,
  prompt_tokens INTEGER,
  completion_tokens INTEGER,
  cost REAL,              -- 未配置单价时为 NULL，不猜价格
  retries INTEGER DEFAULT 0,
  error TEXT,
  cached INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS v2_learning_packs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  goal_key TEXT,
  milestone_id TEXT,        -- 这个包补的是哪个里程碑
  status TEXT,              -- planned / ready / published / archived
  reason TEXT,              -- 为什么推荐这个包（可解释性，方案 M2 要求）
  card_ids TEXT,            -- JSON 数组：这个包包含哪些卡片草稿
  entry_count INTEGER DEFAULT 0,
  failed TEXT,              -- JSON：生成失败的条目及原因，不静默吞掉
  created_at TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS v2_mastery (
  goal_key TEXT,
  concept TEXT,             -- 里程碑 id（m1…）或概念键
  score REAL DEFAULT 0.3,   -- 掌握度 0-1
  attempts INTEGER DEFAULT 0,
  correct INTEGER DEFAULT 0,
  interval_days REAL DEFAULT 1,
  ease REAL DEFAULT 2.5,
  last_review_at TEXT,
  next_review_at TEXT,
  updated_at TEXT,
  PRIMARY KEY (goal_key, concept)
);

CREATE TABLE IF NOT EXISTS v2_review_log (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  goal_key TEXT,
  concept TEXT,
  draft_id INTEGER,
  question_idx INTEGER,
  correct INTEGER,
  confidence REAL,          -- 作答时的自评置信度 0-1
  elapsed_ms INTEGER,
  hint_used INTEGER DEFAULT 0,
  error_type TEXT,          -- 概念混淆 / 数字记错 / 条件漏掉 / 猜的
  detail TEXT,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS v2_goals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  goal_key TEXT,            -- 同一愿望的多个版本共享一个 key
  version INTEGER DEFAULT 1,
  raw_input TEXT,           -- 用户原话，永不覆盖
  spec TEXT,                -- JSON：capability/level/scene/success_evidence/prereq/milestones
  status TEXT,              -- draft / active / paused / archived
  created_at TEXT,
  updated_at TEXT
);

CREATE TABLE IF NOT EXISTS v2_card_drafts (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  input_hash TEXT UNIQUE,   -- 同输入不生成重复版本
  schema_version TEXT,
  source_id INTEGER,
  goal_key TEXT,
  objective TEXT,
  concept TEXT,             -- 映射到的里程碑 id（方案 M2：每张卡都要能映射）
  capability_gap TEXT,      -- 这张卡补的是哪个能力缺口（可解释性）
  figures TEXT,             -- JSON：配图方案（含要解释的命题/图形语法/alt/图注/读图结论）
  assessment TEXT,          -- JSON：题目集（每题绑定学习目标与证据）
  status TEXT,              -- draft / published / rejected
  payload TEXT,             -- JSON 字符串
  gate_report TEXT,         -- JSON 字符串，质量门禁结果
  created_at TEXT,
  updated_at TEXT
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
        # v2 侧轨也要能增量迁移：老库里的 v2_card_drafts 没有 concept / capability_gap
        dcols = [r["name"] for r in conn.execute("PRAGMA table_info(v2_card_drafts)").fetchall()]
        for col, ddl in (
            ("concept", "ALTER TABLE v2_card_drafts ADD COLUMN concept TEXT"),
            ("capability_gap", "ALTER TABLE v2_card_drafts ADD COLUMN capability_gap TEXT"),
            ("figures", "ALTER TABLE v2_card_drafts ADD COLUMN figures TEXT"),
            ("assessment", "ALTER TABLE v2_card_drafts ADD COLUMN assessment TEXT"),
        ):
            if dcols and col not in dcols:
                conn.execute(ddl)

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


def auto_complete_plans():
    """修复存量：已学完（progress 覆盖全部实际卡）但状态仍是 active 的计划 → done（幂等）。
    解决「任务完成了还占位置」：学完判定只在 mark_done 时触发，老数据可能漏判。"""
    conn = _conn()
    try:
        rows = conn.execute("SELECT id FROM plans WHERE status != 'done'").fetchall()
        fixed = 0
        for r in rows:
            pid = r["id"]
            total = conn.execute("SELECT COUNT(*) FROM cards WHERE plan_id = ?", (pid,)).fetchone()[0]
            if total <= 0:
                continue
            done = conn.execute(
                """SELECT COUNT(DISTINCT p.card_source_url) FROM progress p
                   JOIN cards c ON c.source_url = p.card_source_url
                   WHERE c.plan_id = ?""",
                (pid,),
            ).fetchone()[0]
            if done >= total:
                conn.execute("UPDATE plans SET status = 'done' WHERE id = ? AND status != 'done'", (pid,))
                fixed += 1
        conn.commit()
        return fixed
    finally:
        conn.close()


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


def template_dist():
    """模板分布（仪表盘用）：[{template, name, count}]，按数量倒序。"""
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT template, COUNT(*) AS n FROM cards WHERE template IS NOT NULL GROUP BY template ORDER BY n DESC"
        ).fetchall()
    finally:
        conn.close()
    names = {"t1_vocab": "词汇", "t2_reading": "精读", "t3_math": "数学",
             "t4_trivia": "通识", "t5_skill": "技能", "t6_code": "代码"}
    return [{"template": r["template"], "name": names.get(r["template"], r["template"]),
             "count": r["n"]} for r in rows]


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


# ===== 抓取增强：标题归一化 + 跨源查重（资讯产品式去重）=====

def norm_title(t):
    """标题归一化：去标点/空白/小写，用于跨源比较。"""
    import re
    t = (t or "").lower()
    return re.sub(r"[^\w\u4e00-\u9fff]+", "", t)


def _simhash(text, bits=64):
    """简化 SimHash：按 2-gram 切分加权成 64 位指纹。用于识别「标题被改写」的跨源转载。"""
    import hashlib
    import re as _re
    t = _re.sub(r"\s+", "", (text or "").lower())
    if len(t) < 8:
        return 0
    grams = [t[i:i + 2] for i in range(len(t) - 1)]
    v = [0] * bits
    for g in grams:
        h = int(hashlib.md5(g.encode("utf-8")).hexdigest()[:16], 16)
        for i in range(bits):
            v[i] += 1 if (h >> i) & 1 else -1
    fp = 0
    for i in range(bits):
        if v[i] > 0:
            fp |= (1 << i)
    return fp


def _hamming(a, b):
    return bin(a ^ b).count("1")


def find_similar_title(title, summary="", exclude_source_url=None):
    """跨源查重（资讯产品式两级）：
    1) 归一化标题完全匹配；2) 标题足够长时包含关系；3) SimHash 内容指纹（标题+摘要）汉明距离 ≤3。
    返回已存在卡的 source_url，无则 None。短标题只做完全匹配，避免误杀。"""
    nt = norm_title(title)
    if len(nt) < 4:
        return None
    fp_new = _simhash(title + " " + (summary or "")[:200]) if summary else None
    conn = _conn()
    try:
        rows = conn.execute("SELECT source_url, title, summary FROM cards WHERE title IS NOT NULL").fetchall()
    finally:
        conn.close()
    for r in rows:
        if r["source_url"] == exclude_source_url:
            continue
        n_old = norm_title(r["title"])
        if not n_old:
            continue
        if n_old == nt:
            return r["source_url"]
        if len(nt) >= 15 and len(n_old) >= 15 and abs(len(nt) - len(n_old)) <= 10:
            if n_old in nt or nt in n_old:
                return r["source_url"]
        # SimHash：标题被改写但内容相同也能认出
        if fp_new:
            fp_old = _simhash((r["title"] or "") + " " + (r["summary"] or "")[:200])
            if fp_old and _hamming(fp_new, fp_old) <= 3:
                return r["source_url"]
    return None


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


def card_interest(source_url):
    """卡片兴趣分（A2/A3 推荐用）：点开×2 + 看思考题×1 + 提交简答×2。
    返回 0-10 截断的分值；无记录返回 0。"""
    if not source_url:
        return 0
    weights = {"card_open": 2, "think_open": 1, "open_submit": 2}
    conn = _conn()
    try:
        rows = conn.execute(
            "SELECT event, COUNT(*) AS n FROM events WHERE source_url = ? GROUP BY event",
            (source_url,),
        ).fetchall()
    finally:
        conn.close()
    score = sum(weights.get(r["event"], 0) * r["n"] for r in rows)
    return min(score, 10)


# ===== 学习画像（Duolingo/Anki 式：兴趣主题 + 薄弱点 + 准确率）=====

PROFILE_KEYWORDS = [
    "agent", "llm", "大模型", "rag", "transformer", "注意力", "推理", "多模态",
    "训练", "微调", "对齐", "幻觉", "记忆", "上下文", "产品", "数据", "算法",
    "评估", "评测", "部署", "安全", "工具", "工作流", "提示词", "embedding",
    "moe", "蒸馏", "强化学习", "智能体", "检索", "生成", "diffusion", "扩散模型",
]


def weak_cards(limit=10):
    """薄弱卡：复习记错（again）≥2 次且未掌握的卡。返回 [{source_url, title, again_count}]。"""
    conn = _conn()
    try:
        rows = conn.execute(
            """SELECT r.card_source_url, COUNT(*) AS n, c.title, c.memory_state
               FROM reviews r JOIN cards c ON c.source_url = r.card_source_url
               WHERE r.result = 'again'
               GROUP BY r.card_source_url
               HAVING COUNT(*) >= 2""",
        ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        if r["memory_state"] == "mastered":
            continue
        out.append({"source_url": r["card_source_url"],
                    "title": r["title"] or "",
                    "again_count": r["n"]})
    return out[:limit]


def compute_profile():
    """计算学习画像（从行为数据聚合，全部现成数据，零新增埋点）：
    {templates 类型偏好, difficulty 难度偏好, topics 主题兴趣, weak 薄弱卡,
     total_open 点开数, accuracy 近30天复习正确率}"""
    conn = _conn()
    try:
        opened_rows = conn.execute(
            "SELECT DISTINCT source_url FROM events WHERE event = 'card_open' AND source_url != ''"
        ).fetchall()
    finally:
        conn.close()
    opened_urls = [r["source_url"] for r in opened_rows]

    templates = {}
    difficulty = {}
    topics = {}
    if opened_urls:
        cards = []
        conn = _conn()
        try:
            for u in opened_urls:
                row = conn.execute(
                    "SELECT template, title, summary, difficulty FROM cards WHERE source_url = ?",
                    (u,),
                ).fetchone()
                if row:
                    cards.append(dict(row))
        finally:
            conn.close()
        for c in cards:
            tpl = c.get("template") or "unknown"
            templates[tpl] = templates.get(tpl, 0) + 1
            diff = c.get("difficulty") or "未知"
            difficulty[diff] = difficulty.get(diff, 0) + 1
            text = ((c.get("title") or "") + " " + (c.get("summary") or "")).lower()
            for kw in PROFILE_KEYWORDS:
                if kw in text:
                    topics[kw] = topics.get(kw, 0) + 1
    top_topics = sorted(topics.items(), key=lambda kv: kv[1], reverse=True)[:6]

    # 近 30 天复习正确率
    since = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*), SUM(CASE WHEN result = 'good' THEN 1 ELSE 0 END) FROM reviews WHERE review_date >= ?",
            (since,),
        ).fetchone()
    finally:
        conn.close()
    total, good = (row[0] or 0), (row[1] or 0)

    return {
        "templates": templates,
        "difficulty": difficulty,
        "topics": [k for k, _ in top_topics],
        "weak": weak_cards(),
        "total_open": len(opened_urls),
        "accuracy": round(good / total, 3) if total else None,
        "review_total": total,
    }


def get_profile(refresh=False):
    """读画像（user_state 缓存，daily_agent 每天刷新）；无缓存或 refresh=True 时重算。"""
    if not refresh:
        raw = get_user_state("profile")
        if raw:
            try:
                return json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                pass
    p = compute_profile()
    set_user_state("profile", json.dumps(p, ensure_ascii=False))
    return p


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


# ============================================================
# v2 侧轨：信源与证据（CP1）
#
# 与 v1 表零耦合。v2_sources 保存清洗后正文，v2_claims 保存可回溯到
# 原文的证据片段——start/end 是相对 clean_text 的偏移，任何时候都能用
# clean_text[start:end] 原样取回，这是「证据可追溯」的硬性保证。
# ============================================================

def save_v2_source(url, title=None, site=None, lang=None, content_hash=None,
                   clean_text=None, snapshot_path=None, meta=None):
    """写入/更新一个 v2 信源，返回 source id。同 url 视为同一信源。"""
    conn = _conn()
    try:
        row = conn.execute("SELECT id FROM v2_sources WHERE url = ?", (url,)).fetchone()
        if row:
            conn.execute(
                "UPDATE v2_sources SET title=?, site=?, lang=?, content_hash=?, "
                "clean_text=?, snapshot_path=?, meta=? WHERE id=?",
                (title, site, lang, content_hash, clean_text, snapshot_path, _dump(meta), row["id"]),
            )
            conn.commit()
            return row["id"]
        cur = conn.execute(
            "INSERT INTO v2_sources (url, title, site, lang, content_hash, clean_text, "
            "snapshot_path, fetched_at, meta) VALUES (?,?,?,?,?,?,?,?,?)",
            (url, title, site, lang, content_hash, clean_text, snapshot_path,
             datetime.now().isoformat(timespec="seconds"), _dump(meta)),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_v2_source_by_id(source_id):
    """按 id 取信源（规划器拿到的计划里只有 source_id）。"""
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM v2_sources WHERE id = ?", (source_id,)).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def get_v2_source(url):
    """按 url 取信源，没有则 None。"""
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM v2_sources WHERE url = ?", (url,)).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def save_v2_claims(source_id, claims):
    """批量写入一个信源的证据片段。同 source_id 先清后写，保证重跑不累积。"""
    conn = _conn()
    try:
        conn.execute("DELETE FROM v2_claims WHERE source_id = ?", (source_id,))
        conn.executemany(
            "INSERT INTO v2_claims (source_id, claim_idx, text, start, end, kind, usable, meta) "
            "VALUES (?,?,?,?,?,?,?,?)",
            [
                (source_id, c.get("claim_idx", i), c["text"], c["start"], c["end"],
                 c.get("kind", "fact"), 1 if c.get("usable", True) else 0, _dump(c.get("meta")))
                for i, c in enumerate(claims)
            ],
        )
        conn.commit()
    finally:
        conn.close()


def load_v2_claims(source_id, usable_only=False):
    """取某信源的证据片段，按 claim_idx 排序。"""
    conn = _conn()
    try:
        sql = "SELECT * FROM v2_claims WHERE source_id = ?"
        if usable_only:
            sql += " AND usable = 1"
        rows = conn.execute(sql + " ORDER BY claim_idx", (source_id,)).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["meta"] = _load(d.get("meta"))
        d["usable"] = bool(d.get("usable"))
        out.append(d)
    return out


def count_v2_claims(source_id):
    """某信源可用证据数。方案 3.1 要求「至少两条相关核验证据」才允许规划学习包。"""
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM v2_claims WHERE source_id = ? AND usable = 1",
            (source_id,),
        ).fetchone()
    finally:
        conn.close()
    return row[0] if row else 0


# ============================================================
# v2 侧轨：模型调用审计（CP2）
#
# 方案 4.3 要求每次调用都留下 provider / model / prompt_version / input_hash /
# 输出 / 耗时 / 费用 / 重试 / 错误。input_hash 同时是幂等缓存键——
# 同 hash 命中即复用，不重复收费也不生成重复版本。
# ============================================================

def save_v2_model_call(task, provider, model, prompt_version, input_hash,
                       output=None, latency_ms=None, prompt_tokens=None,
                       completion_tokens=None, cost=None, retries=0,
                       error=None, cached=0):
    """记录一次模型调用（成功/失败/缓存命中都记），返回 id。"""
    conn = _conn()
    try:
        cur = conn.execute(
            "INSERT INTO v2_model_calls (ts, task, provider, model, prompt_version, "
            "input_hash, output, latency_ms, prompt_tokens, completion_tokens, cost, "
            "retries, error, cached) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (datetime.now().isoformat(timespec="seconds"), task, provider, model,
             prompt_version, input_hash, output, latency_ms, prompt_tokens,
             completion_tokens, cost, retries, error, 1 if cached else 0),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def find_v2_model_call(input_hash):
    """按幂等键找一条可复用的成功记录。缓存命中行自身不算可复用源。"""
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT * FROM v2_model_calls WHERE input_hash = ? AND error IS NULL "
            "AND cached = 0 ORDER BY id DESC LIMIT 1",
            (input_hash,),
        ).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def recent_v2_model_calls(limit=50, task=None):
    """最近调用记录，倒序。"""
    conn = _conn()
    try:
        if task:
            rows = conn.execute(
                "SELECT * FROM v2_model_calls WHERE task = ? ORDER BY id DESC LIMIT ?",
                (task, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM v2_model_calls ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


# ============================================================
# v2 侧轨：卡片草稿（CP3）
#
# v2 的产出先落 draft，过门禁后才允许成为候选包——这是方案第 7 节
# 「生产与发布之间需要质量门禁」的落点。input_hash 唯一，同输入不重复建版本。
# ============================================================

def save_v2_card_draft(input_hash, schema_version, payload, source_id=None,
                       goal_key=None, objective=None, status="draft", gate_report=None,
                       concept=None, capability_gap=None):
    """写入/更新一份草稿。同 input_hash 视为同一份，返回 id。"""
    now = datetime.now().isoformat(timespec="seconds")
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT id FROM v2_card_drafts WHERE input_hash = ?", (input_hash,)
        ).fetchone()
        if row:
            conn.execute(
                "UPDATE v2_card_drafts SET schema_version=?, payload=?, status=?, "
                "gate_report=?, objective=?, concept=COALESCE(?, concept), "
                "capability_gap=COALESCE(?, capability_gap), updated_at=? WHERE id=?",
                (schema_version, _dump(payload), status, _dump(gate_report),
                 objective, concept, capability_gap, now, row["id"]),
            )
            conn.commit()
            return row["id"]
        cur = conn.execute(
            "INSERT INTO v2_card_drafts (input_hash, schema_version, source_id, goal_key, "
            "objective, concept, capability_gap, status, payload, gate_report, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (input_hash, schema_version, source_id, goal_key, objective, concept,
             capability_gap, status, _dump(payload), _dump(gate_report), now, now),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def get_v2_card_draft(input_hash):
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT * FROM v2_card_drafts WHERE input_hash = ?", (input_hash,)
        ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    d = dict(row)
    d["payload"] = _load(d.get("payload"))
    d["gate_report"] = _load(d.get("gate_report"))
    d["figures"] = _load(d.get("figures"))
    d["assessment"] = _load(d.get("assessment"))
    return d


def get_v2_card_draft_by_id(draft_id):
    """按 id 取草稿。发布前要用它拿到最新 payload 重跑门禁，不信历史状态。"""
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM v2_card_drafts WHERE id = ?", (draft_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    d = dict(row)
    d["payload"] = _load(d.get("payload"))
    d["gate_report"] = _load(d.get("gate_report"))
    d["figures"] = _load(d.get("figures"))
    d["assessment"] = _load(d.get("assessment"))
    return d


def list_v2_card_drafts(status=None, limit=50):
    conn = _conn()
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM v2_card_drafts WHERE status = ? ORDER BY id DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM v2_card_drafts ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["payload"] = _load(d.get("payload"))
        d["gate_report"] = _load(d.get("gate_report"))
        d["figures"] = _load(d.get("figures"))
        d["assessment"] = _load(d.get("assessment"))
        out.append(d)
    return out


def set_v2_card_draft_status(draft_id, status, gate_report=None):
    conn = _conn()
    try:
        conn.execute(
            "UPDATE v2_card_drafts SET status=?, gate_report=?, updated_at=? WHERE id=?",
            (status, _dump(gate_report), datetime.now().isoformat(timespec="seconds"), draft_id),
        )
        conn.commit()
    finally:
        conn.close()


# ============================================================
# v2 侧轨：学习目标（CP9，方案 M2）
#
# 目标必须版本化。方案 M2 验收要求「用户修改目标后旧学习记录保留」——
# 所以改目标不是 UPDATE，而是插入新版本；旧版本留在库里，
# 已有的学习记录仍指向它，历史不会被改写。
# ============================================================

def save_v2_goal(goal_key, raw_input, spec, status="draft"):
    """写入一个新版本的学习目标，返回 (goal_id, version)。

    同 goal_key 再次调用即升版本。raw_input 永远保留用户原话，
    这是之后判断「系统有没有曲解他的意图」的唯一依据。
    """
    now = datetime.now().isoformat(timespec="seconds")
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT MAX(version) AS v FROM v2_goals WHERE goal_key = ?", (goal_key,)
        ).fetchone()
        version = (row["v"] or 0) + 1
        cur = conn.execute(
            "INSERT INTO v2_goals (goal_key, version, raw_input, spec, status, "
            "created_at, updated_at) VALUES (?,?,?,?,?,?,?)",
            (goal_key, version, raw_input, _dump(spec), status, now, now),
        )
        conn.commit()
        return cur.lastrowid, version
    finally:
        conn.close()


def get_v2_goal(goal_id):
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM v2_goals WHERE id = ?", (goal_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    d = dict(row)
    d["spec"] = _load(d.get("spec"))
    return d


def latest_v2_goal(goal_key, status=None):
    """取某 goal_key 的最新版本；给 status 则只在该状态里找。"""
    conn = _conn()
    try:
        if status:
            row = conn.execute(
                "SELECT * FROM v2_goals WHERE goal_key = ? AND status = ? "
                "ORDER BY version DESC LIMIT 1", (goal_key, status),
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT * FROM v2_goals WHERE goal_key = ? ORDER BY version DESC LIMIT 1",
                (goal_key,),
            ).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    d = dict(row)
    d["spec"] = _load(d.get("spec"))
    return d


def list_v2_goals(status=None, limit=50):
    conn = _conn()
    try:
        if status:
            rows = conn.execute(
                "SELECT * FROM v2_goals WHERE status = ? ORDER BY id DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM v2_goals ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["spec"] = _load(d.get("spec"))
        out.append(d)
    return out


def set_v2_goal_status(goal_id, status):
    conn = _conn()
    try:
        conn.execute(
            "UPDATE v2_goals SET status=?, updated_at=? WHERE id=?",
            (status, datetime.now().isoformat(timespec="seconds"), goal_id),
        )
        conn.commit()
    finally:
        conn.close()


# ============================================================
# v2 侧轨：掌握度与复习记录（CP10，方案 M2/M4）
#
# 掌握度按 (goal_key, concept) 存，concept 用里程碑 id。
# 这样「每张卡映射到一个里程碑」之后，卡片的表现能直接汇总成里程碑的掌握度，
# 而掌握度又反过来决定下一个学习包做哪个里程碑。
#
# 复习记录单独一张表：掌握度是可以重算的汇总，记录是不可再生的原始事实。
# 两者分开，将来换掌握度算法时不用重跑历史。
# ============================================================

def upsert_v2_mastery(goal_key, concept, **fields):
    """写入/更新一条掌握度。只更新传入的字段。"""
    now = datetime.now().isoformat(timespec="seconds")
    conn = _conn()
    try:
        row = conn.execute(
            "SELECT * FROM v2_mastery WHERE goal_key = ? AND concept = ?",
            (goal_key, concept),
        ).fetchone()
        if not row:
            conn.execute(
                "INSERT INTO v2_mastery (goal_key, concept, score, updated_at) VALUES (?,?,?,?)",
                (goal_key, concept, fields.get("score", 0.3), now),
            )
        if fields:
            cols = ", ".join("%s = ?" % k for k in fields)
            conn.execute(
                "UPDATE v2_mastery SET %s, updated_at = ? WHERE goal_key = ? AND concept = ?"
                % cols,
                list(fields.values()) + [now, goal_key, concept],
            )
        conn.commit()
    finally:
        conn.close()


def get_v2_mastery(goal_key, concept=None):
    """不给 concept 则返回该目标的 {concept: state}。"""
    conn = _conn()
    try:
        if concept is not None:
            row = conn.execute(
                "SELECT * FROM v2_mastery WHERE goal_key = ? AND concept = ?",
                (goal_key, concept),
            ).fetchone()
            return dict(row) if row else None
        rows = conn.execute(
            "SELECT * FROM v2_mastery WHERE goal_key = ?", (goal_key,)
        ).fetchall()
    finally:
        conn.close()
    return {r["concept"]: dict(r) for r in rows}


def log_v2_review(goal_key, concept, correct, draft_id=None, question_idx=None,
                  confidence=None, elapsed_ms=None, hint_used=False,
                  error_type=None, detail=None):
    """记一次作答。这些字段是方案 M4 要求留存的分析素材。"""
    conn = _conn()
    try:
        cur = conn.execute(
            "INSERT INTO v2_review_log (goal_key, concept, draft_id, question_idx, "
            "correct, confidence, elapsed_ms, hint_used, error_type, detail, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (goal_key, concept, draft_id, question_idx,
             1 if correct else 0, confidence, elapsed_ms,
             1 if hint_used else 0, error_type, detail,
             datetime.now().isoformat(timespec="seconds")),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_v2_reviews(goal_key=None, concept=None, limit=100):
    conn = _conn()
    try:
        where, args = [], []
        if goal_key:
            where.append("goal_key = ?")
            args.append(goal_key)
        if concept:
            where.append("concept = ?")
            args.append(concept)
        sql = "SELECT * FROM v2_review_log"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        rows = conn.execute(sql, args).fetchall()
    finally:
        conn.close()
    return [dict(r) for r in rows]


def save_v2_draft_figures(draft_id, figures):
    """把配图方案挂到草稿上。图形是草稿的附属产物，不单独建表——
    它离开这张卡就没有意义。"""
    conn = _conn()
    try:
        conn.execute(
            "UPDATE v2_card_drafts SET figures=?, updated_at=? WHERE id=?",
            (_dump(figures), datetime.now().isoformat(timespec="seconds"), draft_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_v2_draft_figures(draft_id):
    row = get_v2_card_draft_by_id(draft_id)
    return (row or {}).get("figures")


def save_v2_draft_assessment(draft_id, items):
    """把题目集挂到草稿上。题目离开这张卡就没有意义，不单独建表。"""
    conn = _conn()
    try:
        conn.execute(
            "UPDATE v2_card_drafts SET assessment=?, updated_at=? WHERE id=?",
            (_dump(items), datetime.now().isoformat(timespec="seconds"), draft_id),
        )
        conn.commit()
    finally:
        conn.close()


def get_v2_draft_assessment(draft_id):
    return (get_v2_card_draft_by_id(draft_id) or {}).get("assessment")


# ============================================================
# v2 侧轨：学习包（CP15 / 范围决策 D3）
#
# 范围决策把 LearningPack 列为八个核心实体之一，而此前 planner 只返回一个
# 内存里的 dict —— 没有落库就意味着「一次 20-30 分钟的完整学习」这件事
# 在数据层不存在，掌握度也就无从按包归因。
# ============================================================

def save_v2_learning_pack(goal_key, milestone_id, reason, card_ids=None,
                          status="planned", failed=None):
    now = datetime.now().isoformat(timespec="seconds")
    conn = _conn()
    try:
        cur = conn.execute(
            "INSERT INTO v2_learning_packs (goal_key, milestone_id, status, reason, "
            "card_ids, entry_count, failed, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (goal_key, milestone_id, status, reason, _dump(card_ids or []),
             len(card_ids or []), _dump(failed or []), now, now),
        )
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def _pack_row(row):
    d = dict(row)
    d["card_ids"] = _load(d.get("card_ids")) or []
    d["failed"] = _load(d.get("failed")) or []
    return d


def get_v2_learning_pack(pack_id):
    conn = _conn()
    try:
        row = conn.execute("SELECT * FROM v2_learning_packs WHERE id = ?", (pack_id,)).fetchone()
    finally:
        conn.close()
    return _pack_row(row) if row else None


def list_v2_learning_packs(goal_key=None, status=None, limit=50):
    conn = _conn()
    try:
        where, args = [], []
        if goal_key:
            where.append("goal_key = ?")
            args.append(goal_key)
        if status:
            where.append("status = ?")
            args.append(status)
        sql = "SELECT * FROM v2_learning_packs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        rows = conn.execute(sql, args).fetchall()
    finally:
        conn.close()
    return [_pack_row(r) for r in rows]


def set_v2_learning_pack_status(pack_id, status, card_ids=None):
    conn = _conn()
    try:
        if card_ids is None:
            conn.execute("UPDATE v2_learning_packs SET status=?, updated_at=? WHERE id=?",
                         (status, datetime.now().isoformat(timespec="seconds"), pack_id))
        else:
            conn.execute(
                "UPDATE v2_learning_packs SET status=?, card_ids=?, entry_count=?, "
                "updated_at=? WHERE id=?",
                (status, _dump(card_ids), len(card_ids),
                 datetime.now().isoformat(timespec="seconds"), pack_id))
        conn.commit()
    finally:
        conn.close()
