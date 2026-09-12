# -*- coding: utf-8 -*-
"""
微知 App · 内容抓取管线（最小可运行版）

流程：RSS 抓取 → 提取正文 → DeepSeek 生成卡片 → 存本地 JSON

用法：
    python pipeline.py            # 跑一轮，抓取并生成
    python pipeline.py --fetch-only   # 只抓取，不调 AI（测源用）
    python pipeline.py --limit 1      # 只处理 1 篇文章（测试用）
    python pipeline.py --template t1_vocab --input "serendipity"   # 按需生成词汇卡
    python pipeline.py --template t3_math --input "贝叶斯定理"      # 按需生成数学卡
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime

import feedparser
import trafilatura

# DeepSeek 用 OpenAI 兼容接口
from openai import OpenAI

import db

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")

DEEPSEEK_BASE_URL = "https://api.deepseek.com"


def load_config():
    """读取 config.json，若不存在则引导用户从模板创建。"""
    if not os.path.exists(CONFIG_PATH):
        print("❌ 未找到 config.json。请先复制 config.example.json 为 config.json 并填入 DeepSeek API key。")
        sys.exit(1)
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


def fetch_rss(source, limit=None):
    """抓取一个 RSS 源，返回文章列表 [{title, url, summary}]。
    B1 条件请求：带 ETag/Last-Modified，304 直接返回空（未更新）。
    B2 容错：失败重试 3 次，指数退避（0.5s/2s/8s）。"""
    key = re.sub(r"[^a-zA-Z0-9]+", "_", source["name"])
    headers = {"User-Agent": "Mozilla/5.0 (WeiZhiReader/1.0)"}
    etag = db.get_user_state("etag_" + key)
    lm = db.get_user_state("lm_" + key)
    if etag:
        headers["If-None-Match"] = etag
    if lm:
        headers["If-Modified-Since"] = lm

    last_err = None
    for attempt in range(3):
        try:
            req = urllib.request.Request(source["rss"], headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                code = resp.getcode()
                if code == 304:
                    return []  # 源未更新，跳过
                new_etag = resp.headers.get("ETag")
                new_lm = resp.headers.get("Last-Modified")
                content = resp.read()
            if new_etag:
                db.set_user_state("etag_" + key, new_etag)
            if new_lm:
                db.set_user_state("lm_" + key, new_lm)
            feed = feedparser.parse(content)
            entries = feed.entries
            if limit:
                entries = entries[:limit]
            articles = []
            for e in entries:
                articles.append({
                    "title": getattr(e, "title", "").strip(),
                    "url": getattr(e, "link", "").strip(),
                    "summary": getattr(e, "summary", "") or getattr(e, "description", ""),
                })
            return articles
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return []  # 未更新
            last_err = e
        except Exception as e:
            last_err = e
        if attempt < 2:
            time.sleep(0.5 * (2 ** attempt))
    print(f"  ⚠️ 抓取失败（重试 3 次）：{source['name']} - {last_err}")
    raise last_err if last_err else RuntimeError("fetch failed")


def extract_full_text(url):
    """用 trafilatura 从网页抓正文，返回纯文本。失败返回空字符串。"""
    try:
        html = trafilatura.fetch_url(url)
        if not html:
            return ""
        text = trafilatura.extract(html, include_links=False, include_images=False)
        return text or ""
    except Exception:
        return ""


def focus_on_title_section(title, content):
    """主题聚焦预处理：若正文含多个小节标题，提示 AI 关注与文章标题最相关的小节。

    阮一峰周刊这类源一篇常混多个独立主题，导致生成的 Quiz 跑偏。
    这里检测「## / ### / ####」小节标题，若出现多个，就找出与主标题
    关键词最匹配的小节，并在正文前插入聚焦提示。
    """
    import re

    headings = re.findall(r"^#{2,4}\s+(.+)$", content, flags=re.MULTILINE)
    if len(headings) < 2:
        return content

    title_words = set(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]+", title))
    best, best_score = None, 0
    for h in headings:
        h_words = set(re.findall(r"[\u4e00-\u9fffA-Za-z0-9]+", h))
        score = len(title_words & h_words)
        if score > best_score:
            best, best_score = h, score

    if not best:
        return content

    note = (
        f"（注意：本文是多主题文章，请优先聚焦与主标题最相关的「{best}」"
        f"这一小节生成卡片，忽略其余小节）\n\n"
    )
    return note + content


def _call_deepseek(client, tpl, user_prompt):
    """调 DeepSeek 生成卡片，带 3 次重试。返回解析后的 card dict，失败或跳过返回 None。"""
    for attempt in range(3):  # 重试 3 次
        try:
            resp = client.chat.completions.create(
                model=tpl["model"],
                messages=[
                    {"role": "system", "content": tpl["system"]},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=tpl["temperature"],
                response_format={"type": "json_object"},
            )
            raw = resp.choices[0].message.content
            card = json.loads(raw)
            if card.get("skip"):
                print(f"  ⏭️ AI 判定跳过：{card.get('reason', '')[:40]}")
                return None
            return card
        except Exception as e:
            print(f"  ⚠️ 生成失败（第 {attempt + 1} 次）：{e}")
            time.sleep(2)
    return None


def generate_card(client, source, article, template):
    """调 DeepSeek 把文章加工成卡片。返回卡片 dict，失败返回 None。"""
    content = article.get("summary", "")
    # 若 RSS 摘要太短，尝试抓全文
    if len(content) < 300:
        full = extract_full_text(article["url"])
        if full:
            content = full

    if len(content.strip()) < 100:
        print(f"  ⚠️ 正文太短，跳过：{article['title'][:40]}")
        return None

    # 主题聚焦：周刊类多主题文章，提示 AI 关注标题相关小节
    content = focus_on_title_section(article["title"], content)

    # 截断，避免超 token
    content = content[:8000]

    from prompts import TEMPLATES  # 延迟导入，避免循环
    tpl = TEMPLATES.get(template, TEMPLATES["t2_reading"])

    user_prompt = tpl["user"].format(
        source=source["name"],
        title=article["title"],
        content=content,
        url=article["url"],
    )

    card = _call_deepseek(client, tpl, user_prompt)
    if not card:
        return None
    card["_meta"] = {
        "generated_at": datetime.now().isoformat(),
        "template": template,
        "category": source.get("category", ""),
    }
    return card


# ===== 证据约束成卡（主产线）=====
#
# 为什么改：`generate_card` 把整篇正文塞进 prompt，让模型自己找重点——
# 生成的事实无法回溯到原文，也没法做数字一致性校验。这是「同质化且浅」的
# 机制原因，不是模型不够好。
#
# `evidence.py`（确定性抽证据）+ `card_writer.py`（只喂已定位证据、每段标引用）
# 这套链路本来就在线上，v2 影子每天在用，只是没接到占产量 99% 的主产线上。
# 这里做的就是把两段接通——不是新建，是接线。
#
# 严格模式：证据不足就不出卡。宁可当天少几张，也不产出无证据的浅卡。

MIN_MATERIAL_CHARS = 1000


def build_provider(cfg):
    """构造证据链路用的 provider。key 无效时返回 None。"""
    key = (cfg.get("deepseek_api_key") or "").strip()
    if not key or key.startswith("sk-你的"):
        return None
    from providers import DeepSeekProvider
    return DeepSeekProvider(api_key=key, timeout=180)


def _short(x, n=80):
    s = x if isinstance(x, str) else json.dumps(x, ensure_ascii=False)
    return s[:n]


def _material_text(article):
    """取材料正文：摘要太短就去抓全文。返回纯文本（可能为空）。"""
    content = article.get("summary", "") or ""
    if len(content) < 300:
        full = extract_full_text(article["url"])
        if full:
            content = full
    return content.strip()


def _goal_for_article(article):
    """把一篇文章转成一个临时学习目标。

    为什么需要它：`card_writer` 只喂「与目标相关的证据」。证据筛选的评分是
    `kind 权重 × 2 + 相关性`（见 evidence.select_claims），其中
    kind 权重 2~6、相关性 0~3 —— 所以相关性是**微调而非主导**，它决定的是
    「同档证据里谁更贴题」。主产线没有用户目标，若给一个与材料无关的通用目标，
    这一维就恒为 0，等于白丢一档信息。
    把标题写进 capability，相关性才有区分度：选出的是「这篇文章讲的」，
    而不是「文章里随便哪几句」。

    注意：capability/scene/success_evidence 里的通用词（「核心机制」这类）
    也会被抽成关键词参与打分。这是可接受的——它们对所有文章一致，不产生
    相对偏差；而真正决定取舍的是 kind 权重。
    """
    import schema_v2
    title = (article.get("title") or "").strip() or "这份材料"
    return schema_v2.make_goal(
        key="daily",
        capability="读懂《%s》里的核心机制" % title[:40],
        level="有相关技术背景",
        scene="读完能用上",
        success_evidence="能复述",
        milestones=["复述核心机制", "说出一个边界"],
        daily_minutes=30,
    )


def generate_card_evidenced(provider, source, article, cfg=None):
    """证据约束成卡（主产线）。返回 (card, why)——card 为 None 时 why 说明原因。

    链路（每一步都已在线上跑过，这里只是按主产线的输入重新串起来）：
        取证 → 抽证据 → 证据不足就停 → 受约束写卡 → 出题 → 配图 → 桥接 → 门禁
    """
    import assessment
    import bridge_v1
    import card_writer
    import evidence
    import visual

    text = _material_text(article)
    if len(text) < MIN_MATERIAL_CHARS:
        return None, "正文太短（%d 字）" % len(text)

    sid, claims = evidence.ingest_source(
        article["url"], text,
        title=article.get("title"), site=source.get("name"))
    if len(claims) < evidence.MIN_CLAIMS_FOR_PACK:
        return None, "证据不足（%d 条，需 ≥%d）" % (
            len(claims), evidence.MIN_CLAIMS_FOR_PACK)

    goal = _goal_for_article(article)
    draft, draft_id, report = card_writer.write_card_gated(
        provider, goal, claims,
        source={"title": article.get("title"), "url": article.get("url")},
        source_id=sid)
    if not report.get("passed"):
        return None, "写作门禁未过：%s" % _short(report.get("issues"))

    items, _qreport = assessment.generate(
        provider, draft, claims, draft_id=draft_id)

    figures = []
    try:
        figures = visual.plan_visuals(provider, draft, claims)
        visual.attach(draft_id, [{k: v for k, v in f.items() if k != "_svg"}
                                 for f in figures])
    except Exception as exc:  # noqa: BLE001 - 配图失败不该作废整张卡
        print("    ⚠️ 配图失败（不影响出卡）：%s" % exc)

    card = bridge_v1.from_draft(
        draft_id, items=items,
        material={"title": article.get("title"), "url": article.get("url"),
                  "site": source.get("name"), "source_tier": "blog",
                  "kind": "evolving"},
        provider=provider, figures=figures, shadow=False)

    ok, issues = bridge_v1.publish_gate(card)
    if not ok:
        # 过不了 v1 门禁就不落库：宁可当天不出，也不污染卡片库
        return None, "v1 门禁未过：%s" % _short(issues)
    return card, None


PIPELINE_REPORT_KEY = "pipeline_report"


def _write_pipeline_report(generated, skipped, cfg):
    """把本轮结果写进 user_state，供前端在「今天一张都没出」时解释原因。

    为什么需要：严格模式下当天可能一张卡都不出。如果前端只显示空白，
    用户会读成「坏了」，而不是「今天的材料没达到标准」——这两者该给的动作
    完全不同（前者要排查，后者要投材料或等明天）。
    """
    report = {
        "date": datetime.now().strftime("%Y-%m-%d"),
        "finished_at": datetime.now().isoformat(),
        "generated": generated,
        "skipped": skipped[:20],
        "sources": len(cfg.get("sources") or []),
    }
    try:
        db.set_user_state(PIPELINE_REPORT_KEY, json.dumps(report, ensure_ascii=False))
    except Exception as exc:  # noqa: BLE001 - 报告写失败不该影响已生成的卡
        print(f"  ⚠️ 写生产报告失败：{exc}")
    return report


def generate_on_demand_card(client, template, input_text):
    """按需生成卡片（T1 词汇 / T3 数学），输入来自 --input 参数，不走 RSS 抓取。"""
    from prompts import TEMPLATES  # 延迟导入，避免循环
    tpl = TEMPLATES.get(template)
    if not tpl:
        print(f"  ❌ 未知模板：{template}")
        return None

    if template == "t1_vocab":
        user_prompt = tpl["user"].format(input=input_text)
    elif template in ("t3_math", "t4_trivia", "t5_skill", "t6_code"):
        user_prompt = tpl["user"].format(topic=input_text)
    else:
        user_prompt = input_text

    card = _call_deepseek(client, tpl, user_prompt)
    if not card:
        return None
    card["_meta"] = {
        "generated_at": datetime.now().isoformat(),
        "template": template,
        "category": {
            "t1_vocab": "词汇",
            "t3_math": "数学",
            "t4_trivia": "通识",
            "t5_skill": "技能",
        }.get(template, ""),
    }
    # 按需卡片没有原文链接，用唯一 key 保证能正常入库（save_card 按 source_url 去重）
    card["source_url"] = f"{template}:{input_text}:{int(time.time())}"
    # T1 词汇卡：AI 术语/缩写用术语表覆盖释义，避免被当普通英文词翻译
    if template == "t1_vocab":
        from prompts import AI_TERMS
        w = (card.get("word") or "").strip().lower()
        if w in AI_TERMS:
            card["definition_cn"] = AI_TERMS[w]["definition"]
            card["title"] = card.get("word") or w
    return card


def save_card(card):
    """把卡片存入 SQLite（source_url 去重）。"""
    db.init_db()
    return db.save_card(card)


# ===== 抓取增强：增量过滤（seen 集合） + 跨源查重（一手优先）=====

PREFERRED_DOMAINS = ("arxiv.org", "openai.com", "anthropic.com",
                     "deepmind.google", "huggingface.co", "github.com")


def _title_fp(title):
    """标题指纹：归一化后 md5 前 12 位，用于增量去重。"""
    return hashlib.md5(db.norm_title(title).encode("utf-8")).hexdigest()[:12]


def _article_sim(a):
    """文章内容指纹（SimHash）：只算摘要主体（标题常被转载改写，摘要一般保留）。
    摘要为空时退化为标题指纹。"""
    s = (a.get("summary") or "").strip()
    if len(s) >= 20:
        return db._simhash(s[:200])
    return db._simhash(a.get("title") or "")


GLOBAL_SEEN_KEY = "seen_global"  # 全局已处理指纹（不分源），跨源转载在文章层拦截


def _load_seen():
    raw = db.get_user_state(GLOBAL_SEEN_KEY)
    try:
        return json.loads(raw) if raw else []
    except (json.JSONDecodeError, TypeError):
        return []


def _save_seen(seen):
    """保存全局已见指纹（保留最近 300 条，容量可控）。每项 {t: 标题指纹, s: 内容指纹}。"""
    db.set_user_state(GLOBAL_SEEN_KEY, json.dumps(seen[:300]))


def _is_preferred(url):
    """是否一手/官方域名（跨源重复时优先保留这些来源）。"""
    m = re.search(r"https?://([^/]+)", url or "")
    d = (m.group(1) if m else "").lower()
    return any(d == p or d.endswith("." + p) for p in PREFERRED_DOMAINS)


def filter_fresh(articles, dry=False):
    """全局增量 + 跨源内容去重：返回新增文章列表。
    - 标题指纹已见 → 跳过（增量）
    - 内容指纹（SimHash）与已见汉明距离 ≤3 → 跳过（跨源转载，保留先到的源）
    dry=True 时只算不保存（fetch-only 用）。"""
    seen = _load_seen()
    fresh = []
    new_items = []
    for a in articles:
        fp = _title_fp(a.get("title") or "")
        sim = _article_sim(a)
        dup = False
        for item in seen:
            if fp and item.get("t") == fp:
                dup = True
                break
            if sim and item.get("s") and db._hamming(sim, item["s"]) <= 3:
                dup = True
                break
        if dup or not fp:
            continue
        fresh.append(a)
        new_items.append({"t": fp, "s": sim})
    if new_items and not dry:
        _save_seen(new_items + [i for i in seen if i not in new_items])
    return fresh


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--fetch-only", action="store_true", help="只抓取，不调 AI")
    parser.add_argument("--limit", type=int, default=None, help="每个源最多处理几篇")
    parser.add_argument(
        "--template",
        default="t2_reading",
        choices=["t2_reading", "t1_vocab", "t3_math", "t4_trivia", "t5_skill"],
        help="卡片模板（按需生成时用 t1_vocab / t3_math / t4_trivia / t5_skill）",
    )
    parser.add_argument(
        "--input",
        default=None,
        help="按需生成时的输入：t1_vocab 填英文单词，t3_math 填数学概念/问题，t4_trivia 填冷知识/趣闻，t5_skill 填实用技能/技巧",
    )
    parser.add_argument(
        "--legacy", action="store_true",
        help="回退到旧路径（整篇正文塞 prompt，无证据约束）。仅在证据链路出问题时应急用。",
    )
    args = parser.parse_args()

    config = load_config()
    # 幂等建表：脚本要能自包含运行。原来只有 save_card 里调 init_db，
    # 而 fetch_rss 一开始就要读 user_state（ETag/Last-Modified）——
    # 在全新环境（库里还没有表）会直接失败，且失败信息是
    # 「抓取失败：no such table: user_state」，看着像网络问题。
    db.init_db()

    client = None
    provider = None
    if not args.fetch_only:
        api_key = config.get("deepseek_api_key", "")
        if not api_key or api_key.startswith("sk-你的"):
            print("❌ config.json 里没有有效的 DeepSeek API key。")
            sys.exit(1)
        client = OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL)
        # 证据链路用 provider 接口（providers.TextModelProvider），
        # 不是裸 OpenAI client——它带 schema 校验、重试与调用审计。
        provider = build_provider(config)

    # 按需生成模式：--input 提供时，直接生成单张卡，不走 RSS
    if args.input:
        if args.template == "t2_reading":
            print("❌ --input 仅支持按需模板，请用 --template t1_vocab / t3_math / t4_trivia / t5_skill。")
            sys.exit(1)
        if client is None:
            print("❌ 按需生成需要有效的 DeepSeek API key。")
            sys.exit(1)
        print(f"🎯 按需生成（模板：{args.template}）")
        card = generate_on_demand_card(client, args.template, args.input)
        if card and save_card(card):
            label = card.get("title") or card.get("word") or args.input
            print(f"✅ 已生成卡片：{label[:40]}")
        else:
            print("❌ 生成失败。")
        return

    import bridge_v1  # 证据链路落库走桥接层（与 v2 影子同一入口）

    total_generated = 0
    skipped = []       # 未成卡的材料与原因，供前端空状态展示
    max_cards = int(config.get("daily_generate_limit", 15) or 15)
    per_source = args.limit or 2   # 每源最多生成最新 2 篇（增量过滤后），保证各源都能被覆盖
    for source in config.get("sources", []):
        if total_generated >= max_cards:
            print(f"\n⏹️  已达每日生成上限 {max_cards} 张，停止。")
            break
        print(f"\n📡 抓取源：{source['name']} ({source.get('category', '')})")
        try:
            articles = fetch_rss(source, limit=args.limit or 50)  # 拉足量再增量过滤
        except Exception as e:
            print(f"  ❌ 抓取失败：{e}")
            continue

        print(f"  抓到 {len(articles)} 篇")
        if args.fetch_only:
            continue
        # 全局增量 + 跨源内容去重：只处理没见过的（SimHash 识别改写转载）
        articles = filter_fresh(articles, dry=False)
        if not articles:
            print("  无新增条目，跳过。")
            continue
        print(f"  新增 {len(articles)} 篇（增量过滤后取前 {per_source} 篇）")
        for art in articles[:per_source]:
            if total_generated >= max_cards:
                break
            print(f"  📄 {art['title'][:50]}")
            # 跨源查重（B3 含 SimHash 内容指纹）：重复时一手域名优先替换旧卡，否则跳过
            dup = db.find_similar_title(art["title"], summary=art.get("summary", ""))
            if dup:
                old = db.get_card(dup)
                if _is_preferred(art["url"]) and not _is_preferred((old or {}).get("source_url") or ""):
                    print(f"    ↪️  与已有卡重复且本来源更权威（{dup[:36]}…），替换旧卡")
                    db.delete_card(dup)
                else:
                    print(f"    ⏭️  跨源重复（已有 {dup[:36]}…），跳过")
                    continue
            if args.legacy:
                card = generate_card(client, source, art, "t2_reading")
                saved = bool(card) and save_card(card)
                why = None if saved else "旧路径生成失败"
            else:
                card, why = generate_card_evidenced(provider, source, art, config)
                saved = bool(card) and bridge_v1.save(card)
            if saved:
                total_generated += 1
                print(f"    ✅ 已生成卡片：{card.get('title', '')[:40]}")
            else:
                print(f"    ⏭️  未成卡：{why}")
                skipped.append({"title": (art.get("title") or "")[:50], "why": why})

    _write_pipeline_report(total_generated, skipped, config)
    print(f"\n🎉 本轮完成，共生成 {total_generated} 张卡片。")


if __name__ == "__main__":
    main()
