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
from datetime import datetime, timedelta

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


def _entry_published(entry):
    """RSS 条目的发布时间（ISO 字符串）。拿不到时返回 None。

    为什么必须有：时效窗口靠它判断。没有发布时间的条目只能按「刚抓到」算，
    那会把一篇三周前的置顶文章当成今天的新闻——这正是「过时卡」的来源。
    """
    for key in ("published_parsed", "updated_parsed"):
        t = getattr(entry, key, None)
        if t:
            try:
                return datetime(*t[:6]).isoformat()
            except (TypeError, ValueError):
                pass
    return None


def fetch_rss(source, limit=None):
    """抓取一个 RSS 源，返回文章列表 [{title, url, summary, published}]。
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
                    "published": _entry_published(e),
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


# ===== 来源分级 =====
#
# `news.py` 早就定义了 7 档 SOURCE_TIERS，`bridge_v1.CREDIBILITY` 也有
# 档位到标签的映射，但两条产卡路径都没接线：v1 让模型看着正文猜权威度，
# v2 写死 "source_tier": "blog"。结果是 arXiv 论文的卡和一条二手解读
# 标着同一个「专业博客」。
#
# 这里按**域名**解析级别，而不要求 config 里必须有 tier：已有部署的
# config.json 只有 {name, rss, category}，内置默认表让它们不改配置
# 就立刻用上分级；config 里显式写了 tier 则以 config 为准。
SOURCE_TIER_BY_DOMAIN = {
    "openai.com": "official",
    "anthropic.com": "official",
    "deepmind.google": "official",
    "blog.google": "official",
    "ai.meta.com": "official",
    "microsoft.com": "official",
    "apple.com": "official",
    "arxiv.org": "paper",
    "rss.arxiv.org": "paper",
    "huggingface.co": "primary",
    "github.com": "primary",
    "qbitai.com": "media",
    "jiqizhixin.com": "media",
    "importai.substack.com": "analyst",
    "magazine.sebastianraschka.com": "analyst",
    "baoyu.io": "blog",
    "ruanyifeng.com": "blog",
}

# 时效窗口按**内容寿命**分档，不是按源的更新频率分档。
#
# 为什么不是频率：cron 每天跑两次，新文章当天就会进候选，
# 窗口管的从来不是「能不能及时看到」，而是「一条**没处理过的**旧文章
# 还值不值得读」。所以一手快讯（公告/媒体/论文）只读 7 天内的——
# 过期的新闻是噪音；博客与分析师通讯读 30 天内的——三周前的好文章
# 仍然是好文章，而按 7 天算的话，一篇 9 月 2 日的长文在 9 月 12 日
# 就已经过期读不到了。
#
# 坦白说这是拿 tier 当**代理**：tier 本身是可信度轴，和内容寿命相关
# 但不等价（arXiv 是 tier 0 却是每日更新的一次文献，窗口照样只有 7 天）。
# 要更精确就在源上写 lookback_hours，它的优先级最高。
LOOKBACK_HOURS_BY_TIER = {
    "official": 168,    # 官方公告：过期快
    "paper": 168,       # 论文：每日更新，只取新的
    "primary": 168,     # 开源一手：更新频繁
    "media": 168,       # 媒体快讯
    "analyst": 720,     # 分析师通讯：周更/月更，内容寿命长
    "blog": 720,        # 深度博客：更新慢但不过期
    "social": 168,
}


def lookback_table(config):
    """窗口表：代码默认 <- config 覆盖。

    配置写错（值不是数字）时跳过该项而不是让整轮产线炸掉——
    时效窗口是优化项，不该变成单点故障。
    """
    table = dict(LOOKBACK_HOURS_BY_TIER)
    raw = config.get("lookback_hours_by_tier")
    if isinstance(raw, dict):
        for k, v in raw.items():
            try:
                table[str(k).strip().lower()] = int(v)
            except (TypeError, ValueError):
                continue
    return table


def candidate_window(c, hours=168, hours_by_tier=None):
    """这一条候选的时效窗口（小时）：源级 lookback_hours > 级别默认 > 全局。

    逐条算而不是全局一刀切，是因为「一条 20 天前的深度长文」和
    「一条 20 天前的快讯」根本不是一回事。
    """
    table = hours_by_tier or {}
    try:
        fallback = int(hours)
    except (TypeError, ValueError):
        fallback = 168
    limit = c.get("lookback_hours") or table.get(c.get("source_tier"), fallback)
    try:
        return int(limit)
    except (TypeError, ValueError):
        return fallback


def is_expired(c, hours=168, hours_by_tier=None):
    """是否已超出时效窗口（终态，可以记成已见了）。

    没有发布时间的**不算过期**——源不提供时间是常态，不能因此丢掉它，
    但也不能当它很新（pretriage 排序时排在最后）。
    """
    ts = _parse_ts(c.get("published_at"))
    if not ts:
        return False
    return ts < datetime.now() - timedelta(
        hours=candidate_window(c, hours, hours_by_tier))


def resolve_tier(src):
    """源的级别：config 显式 > 域名默认 > blog。

    兜底用 blog 而不是 official：判不出来时**宁可低估**——
    把二手内容标成一手，比反过来危险得多。
    """
    import news
    t = (src.get("tier") or "").strip().lower()
    if t and t in news.SOURCE_TIERS:
        return t
    m = re.search(r"https?://([^/]+)", src.get("rss") or "")
    host = (m.group(1) if m else "").lower().split(":")[0]
    for dom, tier in SOURCE_TIER_BY_DOMAIN.items():
        if host == dom or host.endswith("." + dom):
            return tier
    return "blog"


def _source_id(src):
    """源的稳定 id。用域名而不是名称——名称会改，域名不会。"""
    m = re.search(r"https?://([^/]+)", src.get("rss") or "")
    host = (m.group(1) if m else "unknown").lower().split(":")[0]
    return re.sub(r"[^a-z0-9]+", "-", host).strip("-") or "unknown"


def _tier_of(source):
    """源的级别：候选自带 source_tier 优先，否则按域名解析。

    两条路径都要能走：主产线的候选在收集时已经解析过级别，
    而按源调用时手上只有 config 里的源定义。
    """
    t = (source.get("source_tier") or source.get("tier") or "").strip().lower()
    if t:
        return t
    return resolve_tier(source)


# ===== 候选池与两级筛选 =====
#
# 为什么要把「抓」和「产卡」拆开：原来是「RSS 拉到什么就产什么」，
# 等于让各个源的更新频率决定你今天读什么。候选池把「有什么」和「读什么」
# 分开，中间才插得进筛选——这也是文章 workflow 质量更高的真正原因：
# 它是「从 30 个候选里挑 2 篇」，不是「来几篇产几篇」。
#
# 两级分工：程序筛管**能算出来的**（时效、去重、级别），模型筛管
# **算不出来的**（值不值得读）。前者不花钱，后者花一次调用。

def collect_candidates(cfg, per_source=50):
    """从所有源抓候选。**不产卡，也不写指纹**——产什么由筛选之后决定。

    这里刻意不写增量指纹（曾经写，是错的）：抓到的条目里绝大多数不会被
    挑中，写指纹等于把它们一次性地判成「已处理」，之后永久出局。
    指纹由 mark_seen 在条目有结论时写，见那里的说明。
    """
    out = []
    for src in (cfg.get("sources") or []):
        try:
            articles = fetch_rss(src, limit=per_source)
        except Exception as exc:  # noqa: BLE001
            print(f"  ❌ 抓取失败：{exc}")
            continue
        if not articles:
            print("  无更新")
            continue
        fresh = filter_fresh(articles)
        print("  抓到 %d 篇，新增 %d 篇" % (len(articles), len(fresh)))
        tier = resolve_tier(src)
        for a in fresh:
            url = a.get("url") or ""
            if not url:
                continue
            out.append({
                "id": hashlib.sha1(url.encode("utf-8")).hexdigest()[:12],
                "source_id": _source_id(src),
                "source_name": src.get("name") or "",
                "source_tier": tier,
                "topics": src.get("topics") or [],
                # 源级时效窗口（可空）。带上它，筛的时候才能按源算，
                # 而不是所有源共用一刀切的 168h。
                "lookback_hours": src.get("lookback_hours"),
                # 指纹跟着候选走：mark_seen 时直接写，不必重算
                # （重算有风险——summary 在候选里被截断过就对不上了）。
                "_fp": a.get("_fp"),
                "_sim": a.get("_sim"),
                "title": (a.get("title") or "").strip(),
                "url": url,
                "summary": a.get("summary") or "",
                "published_at": a.get("published"),
                "fetched_at": datetime.now().isoformat(),
            })
    return out


def _parse_ts(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(
            str(s).replace("Z", "+00:00")[:26]).replace(tzinfo=None)
    except (ValueError, TypeError):
        return None


def pretriage(candidates, hours=168, cap=30, hours_by_tier=None):
    """程序筛：时效窗口 + 同题去重 + 按级别排序 + 限量。

    先筛一遍再交给模型，不是为了省钱，是为了**判断质量**：
    30 个候选和 300 个候选对模型是两回事，后者会让它只挑标题顺眼的。

    逐条算窗口：源级 lookback_hours > 级别默认表 > hours 兜底。
    逐条算而不是全局一刀切，是因为「一条 20 天前的深度长文」
    和「一条 20 天前的快讯」根本不是一回事。
    """
    import news
    seen, fresh = set(), []
    for c in candidates:
        if is_expired(c, hours, hours_by_tier):
            continue                              # 超出该源/该级别的时效窗口
        key = (c.get("title") or "").strip().lower()
        if key and key in seen:
            continue                                  # 同题跨源，保留先到的
        if key:
            seen.add(key)
        fresh.append(c)

    def _ts(c):
        t = _parse_ts(c.get("published_at"))
        return t.timestamp() if t else 0.0

    # 一手来源优先；同级按时间倒序。时间缺失的排最后——
    # 不能因为「不知道什么时候发的」就默认它很新。
    fresh.sort(key=lambda c: (news.SOURCE_TIERS.get(c.get("source_tier"),
                                                    news.DEFAULT_TIER), -_ts(c)))
    return fresh[:cap]


def _validate_rank(data):
    if not isinstance(data, dict):
        return ["应为 JSON 对象"]
    picks = data.get("picks")
    if not isinstance(picks, list):
        return ["picks 应为数组（没有值得读的给空数组）"]
    errs = []
    for i, p in enumerate(picks):
        if not isinstance(p, dict):
            errs.append("picks[%d] 应为对象" % i)
        elif not p.get("id"):
            errs.append("picks[%d] 缺 id" % i)
    return errs


def rank_candidates(provider, candidates, limit=5):
    """模型筛：从候选里挑出值得读的。返回 (picked, mode)。

    这就是「用文章 workflow 的方法」那一步——workflow 把「候选主题归并与
    排序」明确列为模型任务，这里把同一步搬到微知的发现层。

    为什么不能只靠 source_tier 排序：级别只解决「谁说的」，不解决
    「值不值得读」。一条官方发布的计费调整，通常不如一篇独立分析。

    **失败要降级**：模型挂了不该导致当天一张卡都没有。筛选是优化，
    不能变成单点故障，所以失败时退回按级别取前 limit。
    """
    if not candidates:
        return [], "empty"
    if provider is None:
        return candidates[:limit], "no_provider"
    block = "\n".join(
        "[%s] (%s / %s) %s | %s" % (
            c["id"], c.get("source_name"), c.get("source_tier"),
            c.get("title"), (c.get("summary") or "")[:110].replace("\n", " "))
        for c in candidates)
    try:
        data = provider.generate_json(
            "candidate_rank", _validate_rank,
            {"n": len(candidates), "limit": limit, "block": block})
    except Exception as exc:  # noqa: BLE001
        print("  ⚠️ 候选筛选失败，按来源级别取前 %d 篇：%s" % (limit, exc))
        return candidates[:limit], "fallback"

    by_id = {c["id"]: c for c in candidates}
    picked = []
    for p in (data.get("picks") or [])[:limit]:
        c = by_id.get(str(p.get("id")))
        if not c:
            continue
        c = dict(c)
        c["why"] = str(p.get("why") or "")[:80]
        picked.append(c)
    if not picked:
        print("  ⚠️ 模型未挑出任何候选，按来源级别取前 %d 篇" % limit)
        return candidates[:limit], "fallback"
    return picked, "model"


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
        title=article.get("title"),
        site=source.get("name") or source.get("source_name"),
        # 来源属性记在材料上，不只记在卡的标签上：「这条来自哪个源的哪一级」
        # 是判断可信度的原始依据，只留一个标签的话事后无法复核。
        meta={"source_id": source.get("source_id"),
              "source_tier": _tier_of(source),
              "topics": source.get("topics") or [],
              "published_at": article.get("published_at")})
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
                  # 来源级别不再写死 blog：它决定卡上的「权威度」标签，
                  # 写死会让 arXiv 论文和一条二手解读标成同一个值。
                  "site": source.get("name") or source.get("source_name"),
                  "source_tier": _tier_of(source),
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


SEEN_CAP = 3000  # 已见指纹库容量。见 _save_seen 的说明。


def _save_seen(seen):
    """保存全局已见指纹。每项 {t: 标题指纹, s: 内容指纹}。

    容量从 300 提到 3000：300 是「一次抓取最大 300 条」时代的数字，
    那时一轮就能把库撑满，等于记忆只有一轮深。修好写指纹的时机之后，
    一轮会写进「过期归档 + 被挑中」两批（实测约 131 条），
    300 仍然只够记两轮——被挤掉的条目会重新变成「没见过」而复活。
    3000 约合 12 天，够覆盖最长 30 天窗口里的大部分条目。
    """
    db.set_user_state(GLOBAL_SEEN_KEY, json.dumps(seen[:SEEN_CAP]))


def mark_seen(items, dry=False):
    """把条目记成「已处理」。这是**唯一**写指纹的入口。

    为什么要把写指纹从 filter_fresh 里搬出来：
    原来「抓到但没处理」也会被写成已见，于是有一次抓取就能把 300 条容量
    撑满，而实际只有几篇被挑中产卡——**其余的永久出局，再也不会被考虑**。
    现象就是「已见集合有洞」：feed 中段一批条目从没被处理过，
    比它新的和比它旧的却都算见过。

    现在的规矩：**这条已经有结论了**才写。两种结论——
    被挑中处理过（成功、被门禁拒、跨源重复，都算），或者已超出时效窗口。
    没被挑中但仍在窗口内的，留着给下一轮，别的源的新文章不能把它挤掉。

    items 每项需带 `_fp`/`_sim`（collect_candidates 会带上），
    没有就按 title/summary 现算。"""
    if dry or not items:
        return 0
    seen = _load_seen()
    known = {i.get("t") for i in seen}
    fresh = []
    for a in items:
        fp = a.get("_fp") or _title_fp(a.get("title") or "")
        if not fp or fp in known:
            continue
        fresh.append({"t": fp, "s": a.get("_sim") or _article_sim(a)})
        known.add(fp)
    if not fresh:
        return 0
    _save_seen(fresh + seen)
    return len(fresh)


def _is_preferred(url):
    """是否一手/官方域名（跨源重复时优先保留这些来源）。"""
    m = re.search(r"https?://([^/]+)", url or "")
    d = (m.group(1) if m else "").lower()
    return any(d == p or d.endswith("." + p) for p in PREFERRED_DOMAINS)


def filter_fresh(articles):
    """全局增量 + 跨源内容去重：返回新增文章列表。
    - 标题指纹已见 → 跳过（增量）
    - 内容指纹（SimHash）与已见汉明距离 ≤3 → 跳过（跨源转载，保留先到的源）

    **这里只读不写**。指纹由 mark_seen 在「这条有结论了」时才写——
    详见 mark_seen 的说明：原来在这里写，等于把抓到的新条目一次性
    全标成已见，后面没被挑中的那些就永久丢了。
    返回的每项会带上 `_fp`/`_sim`，供后续 mark_seen 直接用。"""
    seen = _load_seen()
    fresh = []
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
        a = dict(a)
        a["_fp"] = fp
        a["_sim"] = sim
        fresh.append(a)
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

    max_cards = int(config.get("daily_generate_limit", 15) or 15)
    if args.legacy:
        total_generated, skipped = _run_legacy(config, client, args, max_cards)
    else:
        total_generated, skipped = _run_pick(config, provider, args, max_cards)

    _write_pipeline_report(total_generated, skipped, config)
    print(f"\n🎉 本轮完成，共生成 {total_generated} 张卡片。")


def _run_legacy(config, client, args, max_cards):
    """旧路径：整篇正文塞 prompt，无证据约束。只作应急回退。"""
    generated, skipped = 0, []
    per_source = args.limit or 2
    for source in config.get("sources", []):
        if generated >= max_cards:
            print(f"\n⏹️  已达每日生成上限 {max_cards} 张，停止。")
            break
        print(f"\n📡 抓取源：{source['name']} ({source.get('category', '')})")
        try:
            articles = fetch_rss(source, limit=args.limit or 50)
        except Exception as e:
            print(f"  ❌ 抓取失败：{e}")
            continue
        print(f"  抓到 {len(articles)} 篇")
        if args.fetch_only:
            continue
        articles = filter_fresh(articles)
        if not articles:
            print("  无新增条目，跳过。")
            continue
        print(f"  新增 {len(articles)} 篇（取前 {per_source} 篇）")
        # 只把**真处理过的**记成已见。取前 N 篇之外的那些留在池子里，
        # 由下一轮接手——否则它们会像以前一样被静默丢掉。
        attempted = []
        for art in articles[:per_source]:
            if generated >= max_cards:
                break
            attempted.append(art)
            print(f"  📄 {art['title'][:50]}")
            dup = db.find_similar_title(art["title"], summary=art.get("summary", ""))
            if dup and not _is_preferred(art.get("url") or ""):
                print(f"    ⏭️  跨源重复（已有 {dup[:36]}…），跳过")
                skipped.append({"title": art["title"][:50], "why": "与已有卡重复"})
                continue
            card = generate_card(client, source, art, "t2_reading")
            if card and save_card(card):
                generated += 1
                print(f"    ✅ 已生成卡片：{card.get('title', '')[:40]}")
            else:
                print("    ⏭️  未成卡")
                skipped.append({"title": art["title"][:50], "why": "旧路径生成失败"})
        mark_seen(attempted)
    return generated, skipped


def _run_pick(config, provider, args, max_cards):
    """候选池 → 两级筛选 → 产卡。返回 (generated, skipped)。

    和 _run_legacy 的根本差别：那边是「RSS 拉到什么就产什么」，
    这边是「从候选里挑最值得读的几篇」——
    各源的更新频率不再决定你今天读什么。
    """
    import bridge_v1

    print("\n📡 收集候选")
    candidates = collect_candidates(config, per_source=args.limit or 50)
    print(f"  候选共 {len(candidates)} 条")
    if args.fetch_only:
        for c in candidates[:40]:
            print(f"    · [{c['source_tier']}] {c['title'][:52]}")
        return 0, []
    if not candidates:
        print("  本轮没有新候选。")
        return 0, []

    # 第一级：程序筛（不花模型调用）
    hours = int(config.get("lookback_hours", 168) or 168)
    cap = int(config.get("candidate_limit", 30) or 30)
    table = lookback_table(config)
    # 已过期的候选是终态：不会再被考虑，所以现在就记成已见，
    # 免得每轮都重新评估一遍同一批过期条目。
    expired = [c for c in candidates if is_expired(c, hours, table)]
    mark_seen(expired)
    pool = pretriage(candidates, hours=hours, cap=cap, hours_by_tier=table)
    # 把窗口策略打出来：出卡少的时候，第一个要回答的问题就是
    # 「是没内容，还是窗口把人拦了」——日志里没有这行就得回头猜。
    by_hours = {}
    for tier_name, h in table.items():
        by_hours.setdefault(h, []).append(tier_name)
    desc = "、".join("%dh(%s)" % (h, "/".join(sorted(names)))
                     for h, names in sorted(by_hours.items()))
    print(f"  时效窗口 {desc}；同题去重后 {len(pool)} 条"
          + (f"（另有 {len(expired)} 条过期，已归档）" if expired else ""))
    if not pool:
        print("  候选全部超出时效窗口，本轮不产出。")
        return 0, []

    # 第二级：模型筛（一次调用，决定今天读什么）
    pick_n = min(int(config.get("daily_pick_limit", 5) or 5), max_cards)
    selected, mode = rank_candidates(provider, pool, limit=pick_n)
    print(f"  筛出 {len(selected)} 篇（{mode}）")
    for c in selected:
        print(f"    · [{c['source_tier']}] {c['title'][:46]}")
        if c.get("why"):
            print(f"      {c['why']}")

    generated, skipped = 0, []
    # 只有**真处理过**的才记成已见。被 cap 切掉、或本轮没轮到的，
    # 留在候选池里等下一轮——这正是让「候选池」名副其实的关键：
    # 否则模型只是在「这 12 小时新到的几条」里挑，而不是在窗口内所有
    # 未读过的文章里挑，慢源的好文章会被快源的噪音挤掉。
    attempted = []
    for cand in selected:
        if generated >= max_cards:
            break
        attempted.append(cand)
        title = cand.get("title") or ""
        print(f"\n📄 {title[:50]}")
        # 跨源查重：重复时一手域名优先替换旧卡，否则跳过
        dup = db.find_similar_title(title, summary=cand.get("summary") or "")
        if dup:
            old = db.get_card(dup)
            if _is_preferred(cand.get("url") or "") and not _is_preferred((old or {}).get("source_url") or ""):
                print(f"    ↪️  与已有卡重复且本来源更权威（{dup[:36]}…），替换旧卡")
                db.delete_card(dup)
            else:
                print(f"    ⏭️  跨源重复（已有 {dup[:36]}…），跳过")
                skipped.append({"title": title[:50], "why": "与已有卡重复"})
                continue
        card, why = generate_card_evidenced(provider, cand, cand, config)
        if card and bridge_v1.save(card):
            generated += 1
            print(f"    ✅ 已生成卡片：{card.get('title', '')[:40]}")
        else:
            print(f"    ⏭️  未成卡：{why}")
            skipped.append({"title": title[:50], "why": why})
    # 门禁没过 / 跨源重复也算「有结论」：不然同一篇会每轮都占一个名额重试。
    mark_seen(attempted)
    return generated, skipped


if __name__ == "__main__":
    main()
