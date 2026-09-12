# -*- coding: utf-8 -*-
"""微知 v2 · 卡片渲染与全链路演示（把 draft + 配图 + 题目 出成可看的 HTML）。

为什么需要这个模块：v2 的产出（CardDraft / 图形方案 / 题目集）都是 JSON，
在接上前端之前没人看得见。方案 M6 的「浏览器端回归」也需要一个稳定的
渲染入口，所以这里做成正式模块而不是临时脚本。

自包含 HTML：CSS 与 SVG 全部内联，单个文件可以直接打开或发给别人看。

用法::

    python render_card.py --latest              # 抓今天最新一篇，走完整链路
    python render_card.py --url https://...     # 指定文章
    python render_card.py --demo                # 用内置样例材料（离线）
    python render_card.py --demo --no-llm       # 连模型也不调，只看渲染效果
"""
from weizhi.core import paths
import argparse
import os
import sys
from datetime import datetime

from weizhi.core import db
from weizhi.produce import evidence
from weizhi.core import schema_v2

CSS = """
:root{--ink:#1f2937;--muted:#6b7280;--line:#e5e7eb;--bg:#f8fafc;--card:#fff;
--accent:#4f46e5;--accent-soft:#eef2ff;--warn:#b45309;--warn-soft:#fffbeb}
*{box-sizing:border-box}
body{margin:0;padding:16px;background:var(--bg);color:var(--ink);
font:16px/1.75 -apple-system,BlinkMacSystemFont,"PingFang SC","Hiragino Sans GB",
"Microsoft YaHei",sans-serif}
.wrap{max-width:720px;margin:0 auto}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:22px;margin-bottom:16px}
h1{font-size:21px;line-height:1.5;margin:0 0 10px}
.obj{background:var(--accent-soft);border-left:3px solid var(--accent);
padding:10px 14px;border-radius:8px;font-size:14px;color:#3730a3;margin-bottom:16px}
.lead{color:#374151;font-size:15px;margin:0 0 18px}
p.seg{margin:0 0 16px}
.cites{margin-left:6px;font-size:11px;color:var(--accent);
background:var(--accent-soft);border-radius:5px;padding:1px 6px;
vertical-align:middle;white-space:nowrap}
h2{font-size:14px;color:var(--muted);font-weight:600;margin:22px 0 10px;
letter-spacing:.04em}
ul.pts{margin:0;padding-left:20px}ul.pts li{margin-bottom:6px}
.bound{background:var(--warn-soft);border-left:3px solid var(--warn);
padding:10px 14px;border-radius:8px;font-size:14px;margin-bottom:10px}
.transfer{background:#f0fdf4;border-left:3px solid #16a34a;padding:12px 14px;
border-radius:8px;font-size:15px}
figure{margin:18px 0}figure svg{display:block;width:100%;height:auto}
figcaption{font-size:13px;color:var(--muted);margin-top:8px;text-align:center}
.figmeta{font-size:12px;color:var(--muted);margin-top:4px;text-align:center}
.q{border:1px solid var(--line);border-radius:10px;padding:14px;margin-bottom:12px}
.q .qt{font-weight:600;margin-bottom:10px;font-size:15px}
.opt{padding:7px 10px;border-radius:7px;background:#f9fafb;margin-bottom:6px;
font-size:14px}
.opt.right{background:#dcfce7;font-weight:600}
details{margin-top:8px;font-size:13px;color:var(--muted)}
details summary{cursor:pointer;color:var(--accent)}
.meta{font-size:12px;color:var(--muted);text-align:center;padding:10px 0 4px}
"""


def _cites(block):
    cs = block.get("cites") or []
    if not cs:
        return ""
    return '<span class="cites">证据 %s</span>' % "、".join("#%d" % c for c in cs)


def to_html(draft, figures=None, items=None, meta=None):
    """把卡片渲染成自包含 HTML。"""
    meta = meta or {}
    parts = ['<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">',
             '<meta name="viewport" content="width=device-width,initial-scale=1">',
             "<title>%s</title><style>%s</style></head><body><div class='wrap'>"
             % (draft.get("title") or "微知卡片", CSS)]

    parts.append("<div class='card'>")
    parts.append("<h1>%s</h1>" % (draft.get("title") or ""))
    if draft.get("objective"):
        parts.append("<div class='obj'>学习目标：%s</div>" % draft["objective"])
    if draft.get("lead"):
        parts.append("<p class='lead'>%s</p>" % draft["lead"])

    for block in draft.get("explanation") or []:
        parts.append("<p class='seg'>%s%s</p>" % (block.get("text") or "", _cites(block)))

    # 配图插在解释与例子之间：正文先讲清，图再固化结构
    for fig in (figures or []):
        svg = fig.get("_svg")
        if not svg:
            continue
        parts.append("<figure>%s<figcaption>%s</figcaption>"
                     "<div class='figmeta'>%s ｜ 读图结论：%s</div></figure>"
                     % (svg, fig.get("caption") or fig.get("proposition") or "",
                        fig.get("kind"), fig.get("reading") or ""))

    for block in draft.get("examples") or []:
        parts.append("<p class='seg'>%s%s</p>" % (block.get("text") or "", _cites(block)))

    if draft.get("boundaries"):
        parts.append("<h2>适用边界</h2>")
        for b in draft["boundaries"]:
            parts.append("<div class='bound'>%s%s</div>" % (b.get("text") or "", _cites(b)))

    if draft.get("key_points"):
        parts.append("<h2>关键点</h2><ul class='pts'>")
        parts.extend("<li>%s</li>" % k for k in draft["key_points"])
        parts.append("</ul>")

    if draft.get("transfer_task"):
        parts.append("<h2>迁移任务</h2><div class='transfer'>%s</div>"
                     % draft["transfer_task"])
    parts.append("</div>")

    if items:
        parts.append("<div class='card'><h2>随堂检测</h2>")
        for i, it in enumerate(items, 1):
            parts.append("<div class='q'><div class='qt'>%d. %s</div>" % (i, it.get("question") or ""))
            for j, opt in enumerate(it.get("options") or []):
                cls = "opt right" if j == it.get("answer") else "opt"
                parts.append("<div class='%s'>%s. %s</div>"
                             % (cls, "ABCD"[j] if j < 4 else j + 1, opt))
            parts.append("<details><summary>看解析（先自己答）</summary>"
                         "<p>%s</p><p>目标：%s</p><p>选错多半是因为：%s</p></details></div>"
                         % (it.get("explanation") or "", it.get("objective") or "",
                            it.get("error_reason") or ""))
        parts.append("</div>")

    if meta:
        parts.append("<div class='meta'>%s</div>" % " ｜ ".join(
            "%s：%s" % (k, v) for k, v in meta.items()))
    parts.append("</div></body></html>")
    return "\n".join(parts)


# ---------- 全链路 ----------

DEMO_MATERIAL = {
    "title": "Agent Harness 的执行循环",
    "url": "https://example.com/agent-harness",
    "text": """
Agent Harness 是一种把模型、工具与循环组织起来的执行框架。
它的核心循环由上下文组装、模型调用、工具执行与状态回写四个阶段组成。
每一轮循环结束后，框架会把工具返回的结果追加回上下文，作为下一轮的输入。
画出循环图时，需要把四个阶段按数据流向串起来，并标出状态回写的箭头指向。
状态回写是让循环闭合的关键一步，少了它模型就看不见上一轮工具做了什么。
上下文组装决定模型看到什么，工具执行决定外部动作怎么发生。
在 2025 年 3 月的评测中，该框架的准确率达到 87.5%，比基线高出 12 个百分点。
""",
}


def _fetch_latest(timeout=30):
    """从已配置的 RSS 源取最新一篇能抓全的文章。"""
    import socket

    import feedparser
    import trafilatura

    cfg = _load_config()
    socket.setdefaulttimeout(timeout)
    for src in (cfg.get("sources") or [])[:6]:
        try:
            feed = feedparser.parse(src.get("rss"))
        except Exception:  # noqa: BLE001
            continue
        for entry in (feed.entries or [])[:4]:
            try:
                raw = trafilatura.fetch_url(entry.link)
                text = trafilatura.extract(raw, include_comments=False) if raw else None
            except Exception:  # noqa: BLE001
                continue
            if text and len(text) > 1200:
                return {"title": entry.title, "url": entry.link, "text": text,
                        "site": src.get("name")}
    return None


def run(url=None, material=None, provider=None, use_llm=True):
    """走一遍 v2 链路，返回 (draft, figures, items, meta, notes)。"""
    notes = []
    if material is None:
        material = _fetch_latest() if url is None else _fetch_url(url)
    if material is None:
        raise SystemExit("没有拿到可用材料（检查网络或换 --demo）")

    sid, claims = evidence.ingest_source(
        material.get("url") or "demo:material", material["text"],
        title=material.get("title"))
    notes.append("证据 %d 条" % len(claims))

    goal = schema_v2.make_goal(
        key="demo",
        capability="能说清这篇材料讲的核心机制，并判断它适用到什么边界",
        level="有基础但不系统",
        scene="读完能在自己的项目里判断要不要用",
        success_evidence="能不查资料复述核心机制并说出一个不适用场景",
        prereq=["基本技术背景"],
        milestones=["复述核心机制", "举出一个不适用场景"],
        daily_minutes=60,
    )

    if not use_llm:
        return None, [], [], {"材料": material.get("title")}, notes + ["--no-llm：未调用模型"]

    from weizhi.core.providers import DeepSeekProvider

    if provider is None:
        provider = _provider_from_config()

    from weizhi.produce import card_writer
    card, draft_id, report = card_writer.write_card_gated(
        provider, goal, claims,
        source={"title": material.get("title"), "url": material.get("url")},
        source_id=sid)
    notes.append("卡片门禁：%s" % ("通过" if report["passed"] else "未通过 " + str(report["issues"])))

    from weizhi.produce import visual
    figures = visual.plan_visuals(provider, card, claims)
    for fig in figures:
        if fig.get("render_mode") == "deterministic":
            try:
                fig["_svg"] = visual.render(fig)
            except ValueError as exc:
                notes.append("图形 %s 渲染跳过：%s" % (fig.get("kind"), exc))
    visual.attach(draft_id, [{k: v for k, v in f.items() if k != "_svg"} for f in figures])
    notes.append("配图 %d 张（%s）" % (
        len([f for f in figures if f.get("_svg")]),
        "、".join(f.get("kind") for f in figures) or "无"))

    from weizhi.produce import assessment
    items, qreport = assessment.generate(provider, card, claims,
                                         concept="m1", draft_id=draft_id)
    notes.append("题目 %d 道，目标覆盖 %.0f%%"
                 % (len(items), qreport["objective_coverage"] * 100))

    meta = {
        "来源": material.get("title"),
        "材料字数": len(evidence.clean_text(material["text"])),
        "生成时间": datetime.now().strftime("%Y-%m-%d %H:%M"),
    }
    return card, figures, items, meta, notes


def _fetch_url(url):
    import trafilatura
    raw = trafilatura.fetch_url(url)
    text = trafilatura.extract(raw, include_comments=False) if raw else None
    if not text:
        return None
    return {"title": url.rsplit("/", 1)[-1], "url": url, "text": text}


def _load_config():
    import json
    cfg_path = os.path.join(paths.data_dir(), "config.json")
    if not os.path.exists(cfg_path):
        return {}
    return json.load(open(cfg_path, encoding="utf-8"))


def _provider_from_config():
    from weizhi.core.providers import DeepSeekProvider
    key = _load_config().get("deepseek_api_key") or ""
    if not key or key.startswith("sk-你的"):
        raise SystemExit("config.json 里没有可用的 deepseek_api_key")
    return DeepSeekProvider(api_key=key, timeout=120)


def main(argv=None):
    ap = argparse.ArgumentParser(description="微知 v2 卡片渲染（全链路）")
    ap.add_argument("--url", help="指定文章 URL")
    ap.add_argument("--latest", action="store_true", help="抓已配置 RSS 源的最新一篇")
    ap.add_argument("--demo", action="store_true", help="用内置样例材料")
    ap.add_argument("--no-llm", action="store_true", help="不调模型，只看渲染效果")
    ap.add_argument("--out", default="demo_out", help="输出目录")
    args = ap.parse_args(argv)

    db.init_db()
    material = DEMO_MATERIAL if args.demo else None
    card, figures, items, meta, notes = run(
        url=args.url, material=material, use_llm=not args.no_llm)

    if card is None:
        print("已跳过模型调用。材料：%s" % material.get("title"))
        return 0

    html = to_html(card, figures, items, meta)
    os.makedirs(args.out, exist_ok=True)
    path = os.path.join(args.out, "card-%s.html" % datetime.now().strftime("%Y%m%d-%H%M%S"))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)

    print("✅ 已生成：%s" % path)
    for n in notes:
        print("   · %s" % n)
    return 0


if __name__ == "__main__":
    sys.exit(main())
