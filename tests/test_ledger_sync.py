"""守卫测试：服务器领先于仓库的功能不能被误删。

背景：仓库的 db.py 曾比线上服务器**落后**——线上有一个 `ledger_concepts`
台账功能（与浏览器翻译插件的「读前简报」打通，微知作为掌握度权威源），
仓库里完全没有。部署时若直接覆盖 db.py，这个功能会被静默删除。

这类「线上领先于仓库」的漂移不会报错、不会失败，只会在某天被人发现
功能没了。所以这里用测试把它钉住。
"""
import db


def test_ledger_functions_exist():
    for name in ("upsert_ledger_concept", "ledger_mastery", "load_ledger_from_reading"):
        assert hasattr(db, name), "台账函数缺失: %s（线上依赖它，别误删）" % name


def test_ledger_table_is_created(tmp_db):
    import sqlite3
    db.init_db()
    conn = sqlite3.connect(db.DB_PATH)
    try:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(ledger_concepts)")]
    finally:
        conn.close()
    assert "norm_id" in cols and "term" in cols, "ledger_concepts 表结构丢失: %s" % cols


def test_upsert_ledger_concept_adds_and_updates(tmp_db):
    db.init_db()
    first = db.upsert_ledger_concept(
        {"norm_id": "rag", "term": "RAG", "one_line": "检索增强生成",
         "why_matters": "让模型能引用外部知识", "depends_on": ["embedding"]},
        {"title": "某文章", "url": "https://x/1"})
    assert first == "added"
    second = db.upsert_ledger_concept(
        {"norm_id": "rag", "term": "RAG", "one_line": "检索增强生成",
         "why_matters": "让模型能引用外部知识", "depends_on": ["rerank"]},
        {"title": "另一篇", "url": "https://x/2"})
    assert second == "updated"


def test_upsert_rejects_empty_norm_id(tmp_db):
    db.init_db()
    assert db.upsert_ledger_concept({"term": "无 id"}, None) is None
