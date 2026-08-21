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
import json
import os
import sys
import time
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
    """抓取一个 RSS 源，返回文章列表 [{title, url, summary}]。"""
    feed = feedparser.parse(source["rss"])
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
    args = parser.parse_args()

    config = load_config()

    client = None
    if not args.fetch_only:
        api_key = config.get("deepseek_api_key", "")
        if not api_key or api_key.startswith("sk-你的"):
            print("❌ config.json 里没有有效的 DeepSeek API key。")
            sys.exit(1)
        client = OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL)

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

    total_generated = 0
    max_cards = int(config.get("daily_generate_limit", 15) or 15)
    per_source = args.limit or 2   # 每源最多处理最新 2 篇：保证各源（含官方源）都能被覆盖，避免前面的源吃满额度
    for source in config.get("sources", []):
        if total_generated >= max_cards:
            print(f"\n⏹️  已达每日生成上限 {max_cards} 张，停止。")
            break
        print(f"\n📡 抓取源：{source['name']} ({source.get('category', '')})")
        try:
            articles = fetch_rss(source, limit=per_source)
        except Exception as e:
            print(f"  ❌ 抓取失败：{e}")
            continue

        print(f"  抓到 {len(articles)} 篇")
        for art in articles:
            if total_generated >= max_cards:
                break
            print(f"  📄 {art['title'][:50]}")
            if args.fetch_only:
                continue
            card = generate_card(client, source, art, "t2_reading")
            if card and save_card(card):
                total_generated += 1
                print(f"    ✅ 已生成卡片：{card.get('title', '')[:40]}")

    print(f"\n🎉 本轮完成，共生成 {total_generated} 张卡片。")


if __name__ == "__main__":
    main()
