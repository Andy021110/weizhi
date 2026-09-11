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


# ---------- 前端侧：reader.html 也曾经落后于线上 ----------
#
# 同一个漂移在 reader.html 上重复了一次：线上有整套「阅读待学」界面
# （入口按钮、弹层、/api/ledger/from-reading 调用），仓库里没有。
# 这次差点在部署时被覆盖掉。
#
# 所以这里补三类断言：
# 1) 线上已有的台账前端必须留在仓库里；
# 2) 本次新加的配图渲染不能被未来的覆盖冲掉；
# 3) 前端引用的后端路由必须真的存在——这是最容易两边各改一半的地方。

import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READER = os.path.join(ROOT, "reader.html")
SERVER = os.path.join(ROOT, "reader.py")


def _reader():
    return open(READER, encoding="utf-8").read()


def test_ledger_ui_is_not_lost_in_reader():
    """线上已有的台账界面（入口 + 弹层 + 取数）必须留在仓库里。"""
    html = _reader()
    for token in ("ledgerBtn", "ledgerOverlay", "showLedger", "loadLedger",
                  "/api/ledger/from-reading"):
        assert token in html, "reader.html 丢失了线上已有的台账界面元素: %s" % token


def test_ledger_routes_exist_on_the_server():
    """前端调的路由必须真的存在，否则界面在、点了没反应。"""
    srv = open(SERVER, encoding="utf-8").read()
    assert "api/ledger/from-reading" in srv, "reader.py 缺少台账取数路由"


def test_figure_rendering_is_not_lost_in_reader():
    """v2 配图的渲染入口不能被未来的覆盖冲掉。"""
    html = _reader()
    for token in ("function renderFigures", "fig-canvas", "renderFigures(c.figures)"):
        assert token in html, "reader.html 丢失了配图渲染: %s" % token


def test_figure_renderer_rejects_untrusted_svg():
    """前端要独立再挡一次——不能假设后端永远正确。"""
    fn = _reader()
    start = fn.index("function renderFigures")
    body = fn[start:start + 1600]
    assert "<script" in body, "前端缺少对脚本注入的拦截"
    assert "aria-label" in body, "配图要有无障碍标签"


def test_reader_js_still_parses():
    """整段 JS 语法检查。前端没有构建步骤，语法错只会在浏览器里炸。"""
    import shutil
    import subprocess
    node = shutil.which("node") or "/Users/minghan/.workbuddy/binaries/node/versions/22.22.2/bin/node"
    if not (os.path.exists(node) if os.path.isabs(node) else shutil.which(node)):
        import pytest
        pytest.skip("本机无 node")
    html = _reader()
    scripts = re.findall(r"<script[^>]*>(.*?)</script>", html, re.S)
    src = "\n;\n".join(scripts)
    out = subprocess.run([node, "--check", "/dev/stdin"], input=src,
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr[-600:]
