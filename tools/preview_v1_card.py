# -*- coding: utf-8 -*-
"""用「真实前端代码」渲染一张 v1 卡片，产出单文件 HTML。

**为什么不是另写一套预览样式**：预览的价值在于它和手机上看到的一致。
如果在 Python 里重写一遍排版，那就只是在验证我自己写的另一套东西——
图在预览里好看、在真机上挤成一团，正是这么来的。

所以这个脚本把 `reader.html` 里的 `<style>` 与渲染函数**原样抽出来**，
在 node 里对真实卡片数据跑一遍，把结果写进单文件 HTML。
与浏览器里的差异只剩「同一个函数被调用了一次」。

用法::

    python preview_v1_card.py                 # 最近一张 v2 桥接卡
    python preview_v1_card.py --draft 42      # 指定 v2 草稿
    python preview_v1_card.py --json card.json  # 直接预览一份卡片 JSON
    python preview_v1_card.py --all           # 把最近的桥接卡都列出来

`--json` 是给「本地没有那张卡」的场景用的：线上（服务器）产出的卡，
导出成 JSON 就能在本地用真实前端代码预览，不必把库搬回来。
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys

from weizhi.core import paths
READER = os.path.join(paths.web_dir(), "reader.html")
OUT_DIR = os.path.join(paths.data_dir(), "demo_out")

# 渲染一张卡所需的全部前端函数。**缺一个就白屏**——CP22 把渲染改成内容块
# 驱动之后，这里漏掉 CARD_BLOCKS 就直接 ReferenceError。
NEEDED = ("escapeHtml", "renderBody", "renderFigures", "layerLabel",
          "_section", "_numbered", "renderCardBlock", "blockTitle",
          "renderCardContent")

# 还要带上这两个变量声明（它们是数据不是函数，抽不出来）
NEEDED_VARS = (("  var CARD_BLOCKS = [", "  function _section("),
               ("  var LIST_KINDS = ", "  function renderCardContent("))


def _vars(src):
    return [src[src.index(a):src.index(b)] for a, b in NEEDED_VARS]

NODE_CANDIDATES = (
    "/Users/minghan/.workbuddy/binaries/node/versions/22.22.2/bin/node",
    "node",
)


def _node():
    for c in NODE_CANDIDATES:
        if os.path.isabs(c):
            if os.path.exists(c):
                return c
        else:
            found = shutil.which(c)
            if found:
                return found
    raise SystemExit("找不到 node，无法复用前端渲染函数")


def _extract(src, name):
    """按括号配对抽出一个顶层 function 的完整源码。"""
    start = src.index("function %s(" % name)
    i = src.index("{", start)
    depth = 0
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
        i += 1
    raise AssertionError("括号不配对: %s" % name)


def _style_block(src):
    m = re.search(r"<style>(.*?)</style>", src, re.S)
    if not m:
        raise SystemExit("reader.html 里找不到 <style>")
    return m.group(1)


def render_card_html(card, reader_src=None):
    """用 reader.html 的真实函数渲染一张卡，返回卡片的 HTML 片段。"""
    src = reader_src or open(READER, encoding="utf-8").read()
    js = "\n".join(_vars(src) + [_extract(src, n) for n in NEEDED] + [
        "var CARD = %s;" % json.dumps(card, ensure_ascii=False),
        "process.stdout.write(renderCardContent(CARD));",
    ])
    out = subprocess.run([_node(), "-e", js], capture_output=True, text=True, timeout=60)
    if out.returncode != 0:
        raise SystemExit("前端渲染失败：\n" + out.stderr[-1500:])
    return out.stdout


PAGE = """<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>%(title)s</title>
<style>%(style)s</style>
<style>
  body { background: #faf8f3; margin: 0; padding: 16px 14px 40px; }
  .wrap { max-width: 680px; margin: 0 auto; }
  .hdr { margin-bottom: 14px; }
  .hdr h1 { font-size: 19px; line-height: 1.45; margin: 0 0 6px; color: var(--text); }
  .hdr .src { font-size: 12.5px; color: var(--text-3); }
  .badges { margin-top: 8px; display: flex; gap: 6px; flex-wrap: wrap; }
  .badges span { font-size: 11.5px; padding: 2px 8px; border-radius: 999px;
    background: var(--green-soft); color: var(--primary-dark); }
  .banner { background: #fff5e6; border: 1px solid #f0d9ae; border-radius: 10px;
    padding: 10px 12px; font-size: 12.5px; color: #7a5a21; margin-bottom: 14px;
    line-height: 1.65; }
  .shadow-tag { font-size: 12px; color: var(--text-3); margin-top: 10px; }
</style>
</head><body>
<div class="wrap">
  <div class="banner">%(banner)s</div>
  <div class="hdr">
    <h1>%(title)s</h1>
    <div class="src">%(source)s</div>
    <div class="badges">%(badges)s</div>
  </div>
  %(content)s
</div>
</body></html>
"""


def build_page(card, content):
    figs = card.get("figures") or []
    notes = ((card.get("_bridge") or {}).get("figure_notes")) or []
    banner = ("这是 <b>v2 生成的影子卡</b>，用 <code>reader.html</code> 的真实函数渲染，"
              "与手机上看到的一致。它不参与每日推送。<br>"
              "配图 %d 张（服务端确定性渲染的 SVG）" % len(figs))
    if notes:
        banner += "；跳过 %d 张：%s" % (len(notes), "、".join(n.get("why", "") for n in notes))
    badges = []
    for k, label in (("difficulty", "难度"), ("timeliness", "时效"),
                     ("credibility", "来源权威度")):
        if card.get(k):
            badges.append("<span>%s：%s</span>" % (label, card[k]))
    layers = (card.get("_bridge") or {}).get("layers") or []
    if layers:
        badges.append("<span>题目分层：%s</span>" % " / ".join(layers))
    return PAGE % {
        "title": card.get("title") or "未命名卡片",
        "style": _style_block(open(READER, encoding="utf-8").read()),
        "banner": banner,
        "source": card.get("source") or "",
        "badges": "".join(badges),
        "content": content,
    }


def _bridged_cards(limit=10):
    """从本地库取 v2 桥接卡（含影子）。"""
    from weizhi.core import db
    cards = []
    for c in db.load_cards(None):
        if (c.get("_bridge") or {}).get("origin") == "v2":
            cards.append(c)
    return cards[-limit:]


def main(argv=None):
    ap = argparse.ArgumentParser(description="用真实前端代码渲染一张 v1 卡片")
    ap.add_argument("--draft", type=int, help="按 v2 草稿 id 桥接并渲染")
    ap.add_argument("--json", help="直接预览一份卡片 JSON（不查本地库）")
    ap.add_argument("--all", action="store_true", help="列出库里所有 v2 桥接卡")
    ap.add_argument("--out", default=None, help="输出 html 路径")
    args = ap.parse_args(argv)

    if args.json:
        cards = [json.load(open(args.json, encoding="utf-8"))]
    else:
        from weizhi.core import db
        db.init_db()

    if args.json:
        pass
    elif args.draft is not None:
        from weizhi.produce import bridge_v1
        card = bridge_v1.from_draft(args.draft)
        cards = [card]
    else:
        cards = _bridged_cards()
        if not cards:
            raise SystemExit("库里没有 v2 桥接卡。先跑一次 v2_shadow.py，或用 --draft 指定草稿。")

    if args.all:
        for c in cards:
            b = c.get("_bridge") or {}
            print("- draft=%s | 图 %d 张 | %s" %
                  (b.get("draft_id"), len(c.get("figures") or []), (c.get("title") or "")[:40]))
        return 0

    card = cards[-1]
    html = build_page(card, render_card_html(card))

    os.makedirs(OUT_DIR, exist_ok=True)
    name = args.out or "v1card-%d.html" % (card.get("_bridge") or {}).get("draft_id", 0)
    path = name if os.path.isabs(name) else os.path.join(OUT_DIR, name)
    open(path, "w", encoding="utf-8").write(html)
    print("已生成: %s (%d 字节)" % (path, len(html)))
    print("配图 %d 张" % len(card.get("figures") or []))
    print("标题:", card.get("title"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
