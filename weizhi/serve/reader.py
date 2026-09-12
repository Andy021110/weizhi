#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
微知 · 碎片阅读器（本地服务器）

用 Python 标准库（http.server + json + os）启动一个本地服务器：
  - 服务 reader.html（根路径 "/"）
  - 提供 /api/dates  返回 cards/ 下所有日期（倒序）
  - 提供 /api/cards  返回卡片 JSON，可用 ?date=YYYY-MM-DD 按日期筛选

用法：
    python reader.py
    然后浏览器打开 http://localhost:8000
"""
import json
import os
import time
import traceback
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

import trafilatura
from openai import OpenAI

from weizhi.core import db

try:                     # v2 侧模块。缺失时不致命——v1 的老路径仍要能跑
    from weizhi.serve import review_flow
except ImportError:      # pragma: no cover
    review_flow = None

from weizhi.core import notifications
from weizhi.core import paths

PORT = int(os.environ.get("PORT", 8000))
# 数据（配置 / 质量报告 / 备份）在部署根；前端资源在代码包里。两者分开之后，
# 整理代码目录不会再碰到真实数据。
REPORTS_DIR = os.path.join(paths.data_dir(), "quality_reports")
BACKUP_DIR = os.path.join(REPORTS_DIR, "backups")

DEEPSEEK_BASE_URL = "https://api.deepseek.com"


def load_api_key():
    """从 config.json 读取 DeepSeek API key。"""
    config_path = os.path.join(paths.data_dir(), "config.json")
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                cfg = json.load(f)
            return cfg.get("deepseek_api_key", "")
        except (json.JSONDecodeError, OSError):
            pass
    return ""


def load_config():
    """从 config.json 读取全部配置，失败返回空 dict。"""
    config_path = os.path.join(paths.data_dir(), "config.json")
    if os.path.exists(config_path):
        try:
            with open(config_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def _auto_template(api_key, topic, fallback="t2_reading"):
    """按主题自动判断内容构成。

    范围决策把「六种固定卡片类型」从用户界面下线了——它们不是六种学习目标，
    只是内容表现形式。所以用户不再选类型，由服务端判断；识别失败就用兜底值，
    不能让一次分类失败把建卡整个卡住。
    """
    topic = (topic or "").strip()
    if not topic or len(topic) < 2:
        return fallback
    try:
        result = classify_topic(api_key, topic)
    except Exception:  # noqa: BLE001
        traceback.print_exc()
        return fallback
    return result.get("template") or fallback


def _int_arg(value, default=-1):
    """把请求里的整数字段转成 int，缺省或非法时给 default。

    **不能用 `value or default`**：0 是合法值（第 1 题、选项 A），
    但它 falsy，会被换成 default，于是永远越界。
    """
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _sealed(cards):
    """批量密封卡片题库。`review_flow` 缺失时原样返回（v1 老路径不能断）。"""
    if not review_flow:
        return cards
    return review_flow.seal_cards(cards)


def _sealed_one(card):
    """单卡密封，返回可合并进响应 dict 的字段。"""
    if not review_flow or not card:
        return {}
    sealed = review_flow.study_card(card)
    return {k: v for k, v in sealed.items()
            if k in ("questions", "layers", "question_count",
                     "open_question", "has_open_question")}


# 单卡详情下发给前端的字段白名单。**不要**加入 _gen_input / _bridge 这类
# 内部数据——这个响应是直接给浏览器的。
CARD_DETAIL_KEYS = (
    "source_url", "title", "template", "summary", "body", "difficulty",
    "timeliness", "credibility", "published", "author", "source",
    "think_question", "think_answer", "core_points", "_meta",
    # 以下五项缺了都不会报错，只会「界面少一块」：
    # figures → 配图在列表里点开就消失；favorite → 收藏按钮状态错；
    # _date → 顶部日期徽标空白；category → 分类徽标变成兜底文字；
    # reading_minutes → 详情页少了「约 N 分钟」。
    "figures", "favorite", "_date", "category", "reading_minutes",
)


def card_detail(card):
    """单卡详情（发现层打开、badcase 展开）要用的字段 + 密封后的题库。

    和 `/api/cards` 走同一套密封，避免两个接口一个封一个不封。
    """
    if not card:
        return None
    picked = {k: card.get(k) for k in CARD_DETAIL_KEYS if k in card}
    picked.update(_sealed_one(card))
    return picked


def load_access_token():
    """从 config.json 读取访问 token（为空则不鉴权）。"""
    return load_config().get("access_token", "")


# ===== 访问角色 =====
#
# 为什么要有第二个 token：这个实例要放出去给人看（作品集/面试），
# 但『能看』和『能改』必须是两件事。演示 token 是**只读**的：
# 每条 GET 都能走，每条写操作都被挡在门外。
#
# 为什么不复用同一个 token 加个 ?demo=1 参数：那个参数随手就能删掉，
# 而权限不该由客户端说了算。两个 token 一分，服务端说了算，
# 顺带还能把「我自己用」和「别人用」在访问日志里分开。
ROLE_OWNER = "owner"
ROLE_DEMO = "demo"


def load_tokens():
    """token -> 角色。空 token 不要（那会变成「谁都能进」的后门）。"""
    cfg = load_config()
    out = {}
    for key, role in (("access_token", ROLE_OWNER), ("demo_token", ROLE_DEMO)):
        t = (cfg.get(key) or "").strip()
        if t:
            out[t] = role
    return out


def resolve_role(token, tokens=None):
    """token 对应的角色；不认识返回 None。

    一个容易踩的分支：**一个 token 都没配**时一律当 owner。
    那是老部署的既有行为（原来完全不鉴权），不能因为新加了演示口令
    就把自己锁在门外——上线顺序一旦搞反，表现是「自己也进不去」。
    """
    tokens = load_tokens() if tokens is None else tokens
    if not tokens:
        return ROLE_OWNER
    return tokens.get((token or "").strip())


def demo_can_write(path):
    """演示 token 能不能写这个路径。

    目前是「一条都不许」，只有一个例外：/api/verify 本身不是写操作，
    它是前端用来确认口令的地方，挡掉的话演示口令根本进不去。
    规则宁可简单到能背下来——「演示 = 零写入」——
    也不要留一串例外，例外会随接口增加而腐化。
    """
    return path == "/api/verify"


def load_push_limit():
    """从 config.json 读取每日推送上限，默认 15。"""
    try:
        return int(load_config().get("daily_push_limit", 15) or 15)
    except (TypeError, ValueError):
        return 15


def load_review_limit():
    """从 config.json 读取每日复习上限，默认 30。"""
    try:
        return int(load_config().get("review_limit", 30) or 30)
    except (TypeError, ValueError):
        return 30


def load_latest_report():
    """读最新质量巡检报告 JSON；无报告返回 None。"""
    if not os.path.isdir(REPORTS_DIR):
        return None
    try:
        files = sorted(f for f in os.listdir(REPORTS_DIR) if f.endswith(".json"))
    except OSError:
        return None
    if not files:
        return None
    try:
        with open(os.path.join(REPORTS_DIR, files[-1]), "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def _load_reports(days=7):
    """读最近 N 天质检报告（按日期倒序），用于仪表盘趋势与修复历史。"""
    if not os.path.isdir(REPORTS_DIR):
        return []
    try:
        files = sorted(f for f in os.listdir(REPORTS_DIR) if f.endswith(".json"))
    except OSError:
        return []
    reports = []
    for f in files[-days:]:
        try:
            with open(os.path.join(REPORTS_DIR, f), "r", encoding="utf-8") as fp:
                reports.append(json.load(fp))
        except (OSError, json.JSONDecodeError):
            continue
    return reports


def notification_scope(qs):
    """解析通知的范围参数，返回 (scope, since_date)。

    默认 `today`：历史通知攒着不清会把「今天要做什么」淹掉——打开就是
    几十条旧消息，久了就学会整个无视徽标。历史没有被删，`scope=all`
    就能翻到，只是不再默认糊在脸上。

    除 `all` 以外的一律按 today 处理（拼错的参数不该静默变成「全部」）。
    """
    scope = (qs.get("scope") or ["today"])[0]
    if scope != "all":
        scope = "today"
    since = None if scope == "all" else datetime.now().strftime("%Y-%m-%d")
    return scope, since


def build_discover(limit=3):
    """发现层：把「不再推送」的内容归到一处，由用户主动来逛。

    两块内容来源不同，但性质相同——都是「不该打扰用户、但值得被看到」的东西：

    - **picks**：daily_agent 每天从新卡里挑的 Top 3 荐读。范围决策把它从推送
      里拿了下来（内容发现应该是用户主动去逛），但当时没给它留展示位置，
      数据只能躺在 `user_state.daily_picks` 里——等于这个能力悄悄消失了。
      这个接口就是它的出口。
    - **lab**：v2 的最近产出（影子卡）。v2 还没接管推送，这里是它唯一的入口；
      没有它，评审要记得去翻历史日期才能看到 v2 到底做了什么。

    两者都只读，不产生任何写入。
    """
    picks = []
    raw = db.get_user_state("daily_picks")
    if raw:
        try:
            parsed = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            parsed = []
        for p in (parsed or [])[:limit]:
            if not isinstance(p, dict) or not p.get("source_url"):
                continue
            picks.append({
                "source_url": p.get("source_url"),
                "title": str(p.get("title") or "")[:40],
                "why": str(p.get("why") or "")[:80],
                "from": "agent",
            })

    # daily_agent 被明确要求「没有好卡就不给」，所以它经常什么都挑不出来。
    # 但发现层是「主动来逛」的地方，空着一块会被读成坏了——退化成如实列出
    # 今日新卡。这不叫替 Agent 硬凑推荐：推荐语换成了卡片自己的摘要，
    # 前端也按 `from` 用不同文案呈现。
    if not picks:
        from weizhi.ops import daily_agent
        for p in daily_agent.today_picks()[:limit]:
            if not p.get("source_url"):
                continue
            picks.append({
                "source_url": p.get("source_url"),
                "title": str(p.get("title") or "")[:40],
                "why": str(p.get("summary") or "")[:80],
                "from": "today",
            })

    lab = []
    for c in db.shadow_cards(limit=limit):
        lab.append({
            "source_url": c.get("source_url"),
            "title": c.get("title") or "",
            "summary": c.get("summary") or "",
            "date": c.get("_date") or c.get("date") or "",
            "figures": len(c.get("figures") or []),
            "questions": len(c.get("quiz") or []) + len(c.get("review_quiz") or []),
        })
    return {"picks": picks, "lab": lab}


def build_dashboard():
    """学习仪表盘聚合：统计 + 内容块分布 + 质量趋势/问题 + 画像 + 修复记录。

    全部数据来自现有库与报告文件，无额外成本。前端「统计」页渲染。
    """
    report = load_latest_report() or {}
    sm = report.get("summary") or {}
    profile = db.get_profile()
    repairs = []
    for r in _load_reports(days=7):
        date = r.get("date", "")
        for a in r.get("auto_actions") or []:
            if not a.get("success"):
                continue
            repairs.append({
                "date": date,
                "title": (a.get("title") or "")[:30],
                "source_url": a.get("source_url", ""),
                "change": (a.get("reason") or "自动修复")[:24],
            })
    return {
        "stats": db.stats(),
        # 掌握度构成 + 知识地图：这两项是「学习者视角」的图，和下面的
        # 内容块分布（维护者视角）不同——它们回答的是「我会什么」，
        # 而不是「我的卡里有什么」。
        "mastery_dist": db.mastery_dist(),
        "knowledge_map": db.knowledge_map(),
        # 内容块分布取代模板分布（类型已从界面下线）
        "block_dist": db.block_dist(),
        "quality": {
            "pass_rate": report.get("pass_rate"),
            "avg_score": report.get("avg_score"),
            "checked": report.get("checked"),
            "trend": sm.get("trend") or [],
            "issues": sm.get("issues") or [],
            "repair": sm.get("repair"),
        },
        "profile": {
            "topics": (profile.get("topics") or [])[:6],
            "weak": (profile.get("weak") or [])[:5],
            "accuracy": profile.get("accuracy"),
        },
        "repairs": repairs[:8],
    }


GRADE_SYSTEM = """你是一位严格的评分老师，负责给学生的简答题打分。
根据参考答案和评分要点，客观评估学生的回答。

评分规则：
1. 按评分要点逐条判断学生是否答到，答到一条给相应分值。
2. 满分 10 分，按答到的要点比例给分。
3. 宽容看待表达差异，只要意思对了就算答到，不要求字句一致。
4. 只输出合法的 JSON，不要输出 JSON 以外的文字。
"""

GRADE_USER = """请给下面的学生回答评分。

【题目】
{question}

【参考答案】
{reference_answer}

【评分要点】
{grading_points}

【学生回答】
{answer}

输出 JSON（字段名必须一致）：
{{
  "score": 0到10的整数,
  "comment": "总体评语，30字以内",
  "feedback": "具体反馈：指出学生答到了哪些要点、漏了哪些要点，60字以内"
}}
"""


def grade_answer(api_key, question, reference_answer, grading_points, answer):
    """调 DeepSeek 给简答题评分，返回 dict，失败返回 None。"""
    if not api_key:
        return {"error": "未配置 API key"}
    try:
        client = OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL, timeout=60)
        points = "\n".join(f"- {p}" for p in (grading_points or []))
        user_prompt = GRADE_USER.format(
            question=question,
            reference_answer=reference_answer,
            grading_points=points,
            answer=answer,
        )
        resp = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": GRADE_SYSTEM},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.3,
            response_format={"type": "json_object"},
        )
        raw = resp.choices[0].message.content
        return json.loads(raw)
    except Exception as e:
        traceback.print_exc()
        return {"error": str(e)}


def save_card(card):
    """把卡片存入 SQLite（source_url 去重）。"""
    return db.save_card(card)


def fix_vocab_terms(card):
    """对 T1 词汇卡：若词是 AI 领域术语/缩写，用术语表强制覆盖释义，避免被当普通英文词翻译（如 RAG→碎布）。"""
    from weizhi.core.prompts import AI_TERMS
    word = (card.get("word") or "").strip().lower()
    if word in AI_TERMS:
        card["definition_cn"] = AI_TERMS[word]["definition"]
        card["title"] = card.get("word") or word
        card["pos"] = card.get("pos") or "n."
        card["pos_label"] = card.get("pos_label") or "名词"
    return card


def create_card(api_key, topic=None, url=None, template="t2_reading", source_url_override=None):
    """在阅读器内新建知识卡。template 支持 t1_vocab/t2_reading/t3_math/t4_trivia/t5_skill。
    source_url_override：从「阅读来的概念」生成卡时，用 ledger:{norm_id} 关联台账。"""
    if not api_key:
        return {"error": "未配置 API key"}

    from weizhi.core.prompts import TEMPLATES  # 延迟导入，避免循环
    tpl = TEMPLATES.get(template, TEMPLATES["t2_reading"])

    if url:
        try:
            html = trafilatura.fetch_url(url)
            content = trafilatura.extract(html, include_links=False, include_images=False) if html else ""
        except Exception:
            content = ""
        content = (content or "").strip()
        if len(content) < 100:
            return {"error": "无法抓取该链接的正文，请确认链接可访问"}
        title = topic or url
    else:
        content = (topic or "").strip()
        if len(content) < 2:
            return {"error": "请输入主题或链接"}
        title = topic

    content = content[:8000]
    # 按模板构造 user_prompt
    if template == "t1_vocab":
        user_prompt = tpl["user"].format(input=content)
    elif template in ("t3_math", "t4_trivia", "t5_skill", "t6_code"):
        user_prompt = tpl["user"].format(topic=content)
    else:
        user_prompt = tpl["user"].format(source="用户创建", title=title, content=content, url=url or "")
    client = OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL, timeout=60)
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
        card = json.loads(resp.choices[0].message.content)
    except Exception as e:
        traceback.print_exc()
        return {"error": str(e)}

    if card.get("skip"):
        return {"error": card.get("reason", "AI 判定该内容无法生成卡片")}

    # topic 模式下模板产出的 source_url 为空，需补唯一 key 才能正常入库去重
    if source_url_override:
        card["source_url"] = source_url_override
    elif not card.get("source_url"):
        card["source_url"] = "custom:{ts}:{title}".format(
            ts=int(time.time()), title=(title or "topic")[:40]
        )
    card.setdefault("source", "用户创建")
    card["_meta"] = {
        "generated_at": datetime.now().isoformat(),
        "template": template,
        "category": {
            "t1_vocab": "词汇",
            "t2_reading": "自建",
            "t3_math": "数学",
            "t4_trivia": "通识",
            "t5_skill": "技能",
        }.get(template, "自建"),
    }

    if template == "t1_vocab":
        fix_vocab_terms(card)

    card["_gen_input"] = content  # 供 badcase 重生成重建输入
    if not save_card(card):
        return {"error": "该卡片已存在"}
    return {"success": True, "card": card}


def generate_outline(api_key, topic, template="t4_trivia", size=20):
    """调 AI 拆解学习大纲（按类型 + 规模档）。返回 {title, outline} 或 {error}。"""
    if not api_key:
        return {"error": "未配置 API key"}
    from weizhi.core.prompts import PLAN_OUTLINE_SYSTEM, PLAN_OUTLINE_USER, TEMPLATE_META
    meta = TEMPLATE_META.get(template, TEMPLATE_META["t4_trivia"])
    client = OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL, timeout=60)
    try:
        resp = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": PLAN_OUTLINE_SYSTEM},
                {"role": "user", "content": PLAN_OUTLINE_USER.format(
                    template_label=meta["label"], topic=topic, size=size)},
            ],
            temperature=0.5,
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content)
        outline = data.get("outline", []) or []
        # 补 index
        for i, item in enumerate(outline, 1):
            if not item.get("index"):
                item["index"] = i
        return {"title": data.get("title", topic), "outline": outline}
    except Exception as e:
        traceback.print_exc()
        return {"error": str(e)}


def disambiguate_topic(api_key, topic, template="t4_trivia"):
    """判断主题是否有多个合理方向（供建计划前确认）。返回 {has_ambiguity, interpretations, reason} 或 {error}。"""
    if not api_key:
        return {"error": "未配置 API key"}
    if not topic or len(topic.strip()) < 2:
        return {"has_ambiguity": False, "interpretations": [], "reason": "主题过短"}
    from weizhi.core.prompts import DISAMBIGUATE_SYSTEM, DISAMBIGUATE_USER, TEMPLATE_META
    meta = TEMPLATE_META.get(template, TEMPLATE_META["t4_trivia"])
    client = OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL, timeout=60)
    try:
        resp = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": DISAMBIGUATE_SYSTEM},
                {"role": "user", "content": DISAMBIGUATE_USER.format(template_label=meta["label"], topic=topic)},
            ],
            temperature=0.3,
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content)
        return {
            "has_ambiguity": bool(data.get("has_ambiguity")),
            "interpretations": (data.get("interpretations") or [])[:3],
            "reason": data.get("reason", ""),
        }
    except Exception as e:
        traceback.print_exc()
        return {"error": str(e)}


def classify_topic(api_key, topic):
    """AI 自动判断主题类型 + 方向歧义（一次调用，替代用户手动选类型）。
    返回 {template, label, reason, has_ambiguity, interpretations} 或 {error}。"""
    if not api_key:
        return {"error": "未配置 API key"}
    if not topic or len(topic.strip()) < 2:
        return {"error": "主题过短"}
    from weizhi.core.prompts import CLASSIFY_SYSTEM, CLASSIFY_USER, TEMPLATE_META
    client = OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL, timeout=60)
    try:
        resp = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": CLASSIFY_SYSTEM},
                {"role": "user", "content": CLASSIFY_USER.format(topic=topic)},
            ],
            temperature=0.2,
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content)
    except Exception as e:
        traceback.print_exc()
        return {"error": str(e)}
    template = (data.get("template") or "").strip()
    if template not in TEMPLATE_META:
        template = "t4_trivia"  # 非法值兜底
    return {
        "template": template,
        "label": TEMPLATE_META[template]["label"],
        "reason": data.get("reason", ""),
        "has_ambiguity": bool(data.get("has_ambiguity")),
        "interpretations": (data.get("interpretations") or [])[:3],
    }


def _group_size(template):
    """每组的卡数：词汇 10 词一组，其余 5 个一组。"""
    return 10 if template == "t1_vocab" else 5


def _validate_card(card, template):
    """质量门禁：校验卡片关键指标，返回 (ok, issues)。不达标则重生成。"""
    issues = []
    if not card.get("think_question") or not card.get("think_answer"):
        issues.append("缺思考题或AI回答")
    elif len(card.get("think_answer") or "") < 150:
        issues.append("AI回答过短(%d字<150)" % len(card.get("think_answer") or ""))
    quiz_min = 2 if template in ("t1_vocab", "t4_trivia", "t5_skill") else 3
    if len(card.get("quiz") or []) < quiz_min:
        issues.append("quiz不足(%d<%d)" % (len(card.get("quiz") or []), quiz_min))
    if len(card.get("review_quiz") or []) < 3:
        issues.append("review_quiz不足(%d<3)" % len(card.get("review_quiz") or []))
    if template in ("t2_reading", "t3_math", "t5_skill", "t6_code") and not card.get("open_question"):
        issues.append("缺简答题")
    return (len(issues) == 0, issues)


def _generate_plan_cards(api_key, plan_id, outline, template, base_index, batch_index, total):
    """按大纲批量生成卡（create_plan / continue_plan 共用）。带质量门禁：不达标重试。返回生成张数。"""
    from weizhi.core.prompts import TEMPLATES
    tpl = TEMPLATES.get(template, TEMPLATES["t4_trivia"])
    client = OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL, timeout=60)
    group_size = _group_size(template)

    generated = 0
    for i, item in enumerate(outline):
        idx = base_index + i  # 全局 plan_index
        point_title = (item.get("title") or "").strip()
        focus = (item.get("focus") or "").strip()
        if not point_title:
            continue
        if template == "t1_vocab":
            user_prompt = tpl["user"].format(input=point_title)  # 词汇卡：输入单词本身
            gen_input = point_title
        elif template in ("t3_math", "t4_trivia", "t5_skill", "t6_code"):
            topic_input = point_title if not focus else f"{point_title}（{focus}）"
            user_prompt = tpl["user"].format(topic=topic_input)
            gen_input = topic_input
        else:  # t2_reading
            topic_input = point_title if not focus else f"{point_title}（{focus}）"
            user_prompt = tpl["user"].format(source="学习计划", title=point_title, content=topic_input, url="")
            gen_input = topic_input
        card = None
        for attempt in range(3):  # 质量门禁：最多重试 2 次
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
                card = json.loads(resp.choices[0].message.content)
            except Exception as e:
                traceback.print_exc()
                card = None
                break
            if card.get("skip"):
                break
            ok, issues = _validate_card(card, template)
            if ok:
                break
            if attempt < 2:
                user_prompt += "\n注意：上次生成不符合要求（%s），请修正后重新输出完整 JSON。" % "；".join(issues)
        if not card or card.get("skip"):
            continue
        if not card.get("source_url"):
            card["source_url"] = f"plan:{plan_id}:{idx}:{point_title[:30]}:{int(time.time())}"
        card.setdefault("source", "学习计划")
        card["plan_id"] = plan_id
        card["plan_index"] = idx
        card["plan_total"] = total
        card["group_index"] = ((idx - 1) // group_size) + 1
        card["batch_index"] = batch_index
        card["_meta"] = {
            "generated_at": datetime.now().isoformat(),
            "template": template,
            "category": {
                "t1_vocab": "词汇",
                "t2_reading": "精读",
                "t3_math": "数学",
                "t4_trivia": "通识",
                "t5_skill": "技能",
            }.get(template, "计划"),
        }
        if template == "t1_vocab":
            fix_vocab_terms(card)
        card["_gen_input"] = gen_input  # 供 badcase 重生成重建输入
        if save_card(card):
            generated += 1
    return generated


def _generate_plan_async(api_key, plan_id, outline, template, total):
    """后台线程：逐卡生成计划卡，完成后把计划状态从 generating 改为 active。"""
    try:
        _generate_plan_cards(api_key, plan_id, outline, template,
                             base_index=1, batch_index=1, total=total)
        plan = db.get_plan(plan_id)
        if plan:
            plan_update = dict(plan)
            plan_update["status"] = "active"
            db.save_plan(plan_update)
    except Exception:
        traceback.print_exc()


def _generate_card(api_key, old):
    """按原卡的 template / _gen_input 生成一份**新内容**，不写库。

    失败时返回 {"_error": ...}，成功返回卡片 dict。抽出来是为了让
    「重生成」与「提出修订候选」共用同一段生成逻辑——两份实现必然漂移。
    """
    template = old.get("template") or (old.get("_meta") or {}).get("template") or "t2_reading"
    gen_input = old.get("_gen_input") or old.get("title") or ""
    source_url = old.get("source_url") or ""

    from weizhi.core.prompts import TEMPLATES
    tpl = TEMPLATES.get(template, TEMPLATES["t2_reading"])
    client = OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL, timeout=60)
    card = None
    user_prompt = None
    for attempt in range(3):  # 质量门禁：最多重试 2 次
        try:
            if template == "t1_vocab":
                user_prompt = tpl["user"].format(input=gen_input)
            elif template in ("t3_math", "t4_trivia", "t5_skill", "t6_code"):
                user_prompt = tpl["user"].format(topic=gen_input)
            else:
                user_prompt = tpl["user"].format(
                    source="学习计划", title=old.get("title") or gen_input,
                    content=gen_input, url="")
            resp = client.chat.completions.create(
                model=tpl["model"],
                messages=[
                    {"role": "system", "content": tpl["system"]},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=tpl["temperature"],
                response_format={"type": "json_object"},
            )
            card = json.loads(resp.choices[0].message.content)
        except Exception as e:
            traceback.print_exc()
            card = None
            break
        if card.get("skip"):
            break
        ok, issues = _validate_card(card, template)
        if ok:
            break
        if attempt < 2:
            user_prompt += "\n注意：上次生成不符合要求（%s），请修正后重新输出完整 JSON。" % "；".join(issues)

    if not card or card.get("skip"):
        return {"_error": (card.get("reason") if card else None) or "重生成失败"}

    return card


def regen_card(api_key, source_url, apply=True):
    """重生成一张卡。

    `apply=False` 时**只生成内容、不写库**——修订候选（revisions.py）需要它：
    候选在人工采纳前不能改动已发布的卡。

    写回时用 `db.apply_card_revision` **原地更新**，不再「存新卡 + 删旧卡」：
    后者会连带删掉 progress，等于"修卡"顺手抹掉用户的学习历史。
    """
    if not api_key:
        return {"error": "未配置 API key"}
    if not source_url:
        return {"error": "缺少 source_url"}
    old = db.get_card(source_url)
    if not old:
        return {"error": "卡片不存在"}
    if old.get("_regen_at"):
        return {"error": "该卡正在重新生成中，请稍候"}
    gen_input = old.get("_gen_input") or old.get("title") or ""
    if not gen_input:
        return {"error": "该卡缺少生成输入，无法重生成"}

    card = _generate_card(api_key, old)
    if isinstance(card, dict) and card.get("_error"):
        return {"error": card["_error"]}

    if not apply:
        return {"success": True, "card": card, "applied": False}

    db.update_card_extra(source_url, "_regen_at", datetime.now().isoformat())
    if not db.apply_card_revision(source_url, card):
        db.update_card_extra(source_url, "_regen_at", None)
        return {"error": "写回失败，请重试"}
    db.update_card_extra(source_url, "_regen_at", None)
    return {"success": True, "card": card, "applied": True}


def find_backup(source_url):
    """按 source_url 找最新备份文件（backups/日期/md5.json），找不到返回 None。"""
    import hashlib
    if not source_url:
        return None
    h = hashlib.md5(source_url.encode("utf-8")).hexdigest()
    if not os.path.isdir(BACKUP_DIR):
        return None
    try:
        for d in sorted(os.listdir(BACKUP_DIR), reverse=True):
            p = os.path.join(BACKUP_DIR, d, h + ".json")
            if os.path.isfile(p):
                return p
    except OSError:
        return None
    return None


def rollback_card(source_url):
    """回滚自动重生成：删新卡 + 恢复备份的旧卡与完成记录。返回 {success} 或 {error}。"""
    backup_path = find_backup(source_url)
    if not backup_path:
        return {"error": "未找到该卡的备份，无法回滚"}
    try:
        with open(backup_path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError):
        return {"error": "备份文件读取失败"}
    card = data.get("card") or {}
    if not card.get("source_url"):
        return {"error": "备份内容异常"}
    # 删新卡：只对「旧实现」产生的备份有意义（那时重生成会换 source_url）。
    # 现在的重生成是原地更新，url 不变，这一支不会命中。
    new_src = data.get("new_source_url")
    if new_src and new_src != card["source_url"]:
        db.delete_card(new_src)
    # 恢复旧内容：**原地写回**，不再 delete + save。
    # 后者会连带删掉 progress——回滚不该把用户在学习期间攒下的记录抹掉。
    date = data.get("card_date") or card.get("date")
    if db.get_card(card["source_url"]):
        db.apply_card_revision(card["source_url"], card, date=date)
    else:
        db.save_card(card, date=date)
    for d in (data.get("progress_dates") or []):
        db.restore_progress(d, card["source_url"])
    return {"success": True, "card": card}


def create_plan(api_key, title, topic, outline, cards_per_day=3, template="t4_trivia", scale=None, pace=None):
    """建计划 + 后台异步生成卡：立即返回（不阻塞），卡片在后台线程逐张生成。
    大档（如 100 卡）不会卡住请求；前端用 /api/plan/progress 轮询进度。"""
    if not api_key:
        return {"error": "未配置 API key"}
    if not outline:
        return {"error": "大纲为空"}

    total = len(outline)
    scale = scale or total or 20
    pace = pace or cards_per_day or 3
    plan_id = db.save_plan({
        "title": title or topic,
        "topic": topic,
        "source": "user",
        "duration": "long",
        "total_cards": total,
        "cards_per_day": pace,
        "scale": scale,
        "pace": pace,
        "batch": 1,
        "template": template,
        "status": "generating",
    })

    import threading
    t = threading.Thread(target=_generate_plan_async,
                         args=(api_key, plan_id, outline, template, total),
                         daemon=True)
    t.start()

    return {"plan": db.get_plan(plan_id), "generating": True, "total": total}


def _continue_plan_async(api_key, plan_id, outline, template, base_index, batch_index, new_total):
    """后台线程：续学生成新一批卡，完成后把计划状态改回 active。"""
    try:
        _generate_plan_cards(api_key, plan_id, outline, template,
                             base_index=base_index, batch_index=batch_index, total=new_total)
        plan = db.get_plan(plan_id)
        if plan:
            plan_update = dict(plan)
            plan_update["status"] = "active"
            db.save_plan(plan_update)
    except Exception:
        traceback.print_exc()


def continue_plan(api_key, plan_id):
    """续学：为大主题生成下一批卡（异步）。先出大纲（快），立即返回；卡在后台生成。"""
    if not api_key:
        return {"error": "未配置 API key"}
    plan = db.get_plan(plan_id)
    if not plan:
        return {"error": "计划不存在"}
    template = plan.get("template") or "t4_trivia"
    scale = plan.get("scale") or 20
    batch = plan.get("batch") or 1
    new_batch = batch + 1

    from weizhi.core.prompts import PLAN_OUTLINE_SYSTEM, PLAN_OUTLINE_USER, TEMPLATE_META
    meta = TEMPLATE_META.get(template, TEMPLATE_META["t4_trivia"])
    client = OpenAI(api_key=api_key, base_url=DEEPSEEK_BASE_URL, timeout=60)
    topic_prompt = f"{plan.get('topic', '')}（续：第 {new_batch} 批，接续前 {batch} 批，避免重复）"
    try:
        resp = client.chat.completions.create(
            model="deepseek-chat",
            messages=[
                {"role": "system", "content": PLAN_OUTLINE_SYSTEM},
                {"role": "user", "content": PLAN_OUTLINE_USER.format(
                    template_label=meta["label"], topic=topic_prompt, size=scale)},
            ],
            temperature=0.5,
            response_format={"type": "json_object"},
        )
        data = json.loads(resp.choices[0].message.content)
        outline = data.get("outline", []) or []
    except Exception as e:
        traceback.print_exc()
        return {"error": str(e)}

    if not outline:
        return {"error": "续学大纲生成失败"}

    base_index = (batch - 1) * scale + 1
    new_total = (plan.get("total_cards") or 0) + len(outline)

    # 先更新计划信息 + 标记 generating，再后台生成
    plan_update = dict(plan)
    plan_update["total_cards"] = new_total
    plan_update["batch"] = new_batch
    plan_update["status"] = "generating"
    db.save_plan(plan_update)

    import threading
    t = threading.Thread(target=_continue_plan_async,
                         args=(api_key, plan_id, outline, template, base_index, new_batch, new_total),
                         daemon=True)
    t.start()

    return {"plan": db.get_plan(plan_id), "generating": True, "total": new_total, "batch": new_batch}


def list_dates():
    """返回所有日期（YYYY-MM-DD），倒序。"""
    return db.list_dates()


# ===== A3 推荐排序：新鲜度 × 源权威 × 兴趣（已读沉底）=====

CRED_WEIGHT = {
    "官方": 1.2,
    "权威媒体": 1.0,
    "专业机构": 1.0,
    "专业博客": 0.9,
    "自媒体": 0.7,
}


def _freshness_score(c):
    """时效分（0-1）：event 2 周内满分、fast 半年内高分、evolving 2 年内中等，stable 稳定分。"""
    t = c.get("timeliness")
    if t == "trending":
        t = "evolving"
    days = None
    pub = c.get("published")
    if pub:
        try:
            days = (datetime.now() - datetime.strptime(str(pub)[:10], "%Y-%m-%d")).days
        except ValueError:
            days = None
    if t == "event":
        return 1.0 if (days is not None and days <= 14) else 0.2
    if t == "fast":
        return 0.9 if (days is not None and days <= 180) else 0.3
    if t == "evolving":
        return 0.8 if (days is not None and days <= 730) else 0.4
    if t == "stable":
        return 0.6
    return 0.4  # 无时效标注


def _authority_score(c):
    """权威权重：从 credibility 映射（策略文档 A/B/C/D 级）。"""
    cred = c.get("credibility") or ""
    for k, w in CRED_WEIGHT.items():
        if k in cred:
            return w
    return 0.8


def load_cards(date=None):
    """读取卡片。date 为空则读全部；否则只读指定日期。
    全部视图按「推荐分」排序：新鲜度 × 源权威 × (1+兴趣/10) × 画像主题加成，已读卡沉底（×0.3）。"""
    cards = db.load_cards(date)
    if not date:
        done = set()
        for urls in db.get_done_dates().values():
            done.update(urls)
        topics = (db.get_profile().get("topics") or [])  # 画像兴趣主题
        def interest_boost(c):
            if not topics:
                return 1.0
            text = ((c.get("title") or "") + " " + (c.get("summary") or "")).lower()
            hits = sum(1 for t in topics if t in text)
            return 1.0 + min(hits, 3) * 0.15  # 命中画像主题最多 +45%
        def sort_key(c):
            base = _freshness_score(c) * _authority_score(c) * (1 + db.card_interest(c.get("source_url")) / 10.0)
            base *= interest_boost(c)
            if c.get("source_url") in done:
                base *= 0.3  # 已读沉底，让「今天读什么」优先
            return base
        cards.sort(key=sort_key, reverse=True)
    return cards


class ReaderHandler(BaseHTTPRequestHandler):
    def _role(self, qs):
        """本次请求的角色。token 从 URL ?key= 或 header X-Auth-Token 读。"""
        provided = (qs.get("key") or [None])[0] or self.headers.get("X-Auth-Token", "")
        return resolve_role(provided)

    def _forbidden(self, payload=None):
        body = json.dumps(payload or {"error": "forbidden"},
                          ensure_ascii=False).encode("utf-8")
        self.send_response(403)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _client_ip(self):
        """真实来访 IP。服务在 nginx 后面，直连地址永远是 127.0.0.1——
        只看 self.client_address 会把所有访客记成同一个人。"""
        fwd = self.headers.get("X-Forwarded-For", "")
        if fwd:
            return fwd.split(",")[0].strip()
        return (self.headers.get("X-Real-IP")
                or (self.client_address[0] if self.client_address else ""))

    def _log_demo_access(self, method, path, blocked):
        """只记演示角色的访问，用来区分「我自己用」和「别人用」。"""
        try:
            db.record_demo_access(self._client_ip(), method, path, blocked,
                                  self.headers.get("User-Agent", "")[:80])
        except Exception:  # noqa: BLE001 - 记日志失败绝不能影响正常响应
            pass

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        role = self._role(qs)
        if path.startswith("/api/") and role is None:
            self._forbidden()
            return
        if role == ROLE_DEMO and path.startswith("/api/"):
            self._log_demo_access("GET", path, False)

        if path == "/api/me":
            # 前端靠它决定要不要切成只读界面。放在最前面：
            # 它必须永远可用，否则演示模式的前端拿不到自己的角色。
            self._send_json({"role": role or ROLE_OWNER})
            return

        if path == "/api/demo/summary":
            # 只给自己看：演示用户的访问流水。演示角色当然不该看到
            # 「有几个访客在看我」——那不是给他看的东西。
            if role != ROLE_OWNER:
                self._forbidden()
                return
            self._send_json(db.demo_access_summary())
            return

        if path == "/api/dates":
            self._send_json({"dates": list_dates()})
            return

        if path == "/api/cards":
            date = (qs.get("date") or [None])[0]
            cards = load_cards(date)
            # 推送上限：仅对「今天」限制，历史日期可看全部
            if date == datetime.now().strftime("%Y-%m-%d"):
                limit = load_push_limit()
                if len(cards) > limit:
                    cards = cards[:limit]
            # 下发前密封题库。此前每道题的 answer 与简答题参考答案都在
            # 这份响应里，打开开发者工具就能看到——自测就不成立了。
            self._send_json({"cards": _sealed(cards)})
            return

        if path == "/api/state":
            self._send_json({
                "done": db.get_done_dates(),
                "user_state": db.get_all_state(),
            })
            return

        if path == "/api/plans":
            plans = db.load_plans()
            for p in plans:
                p["done_count"] = db.count_plan_done(p["id"])
            self._send_json({"plans": plans})
            return

        if path == "/api/plan/cards":
            plan_id = int((qs.get("id") or [0])[0])
            self._send_json({
                "plan": db.get_plan(plan_id),
                "cards": _sealed(db.load_plan_cards(plan_id)),
            })
            return

        if path == "/api/review/queue":
            today = datetime.now().strftime("%Y-%m-%d")
            cards = db.get_due_reviews(today, limit=load_review_limit())
            # 下发前密封题库。此前每道题的 answer 随卡片一起发到浏览器，
            # 前端自己比对——打开开发者工具就能看到答案，"复习"就不成立了。
            self._send_json({"reviews": [review_flow.queue_card(c) for c in cards]})
            return

        if path == "/api/review/daily":
            goal_key = (qs.get("goal") or [None])[0]
            self._send_json(review_flow.daily_summary(goal_key))
            return

        if path == "/api/search":
            q = (qs.get("q") or [""])[0]
            self._send_json({"cards": db.search_cards(q)})
            return

        if path == "/api/card":
            # 单卡详情（badcase 展开、发现层打开用）：按 source_url 取完整内容
            src = (qs.get("source_url") or [""])[0]
            card = db.get_card(src) if src else None
            # 字段白名单与密封都在 card_detail 一处实现，避免这个接口和
            # /api/cards 各维护一份——两份清单迟早漂移，而漂移不报错。
            self._send_json({"card": card_detail(card)})
            return

        if path == "/api/revisions":
            # 待审修订候选。**这些候选还没有生效**，卡片保持原样，
            # 采纳与否由这里决定（范围决策：不允许自愈静默改已发布的卡）。
            from weizhi.ops import revisions
            self._send_json({
                "revisions": revisions.pending(limit=50),
                "pending": revisions.count_pending(),
            })
            return

        if path == "/api/favorites":
            self._send_json({"cards": db.load_favorites()})
            return

        if path == "/api/discover":
            self._send_json(build_discover())
            return

        if path == "/api/stats":
            self._send_json(db.stats())
            return

        if path == "/api/dashboard":
            self._send_json(build_dashboard())
            return

        if path == "/api/report/latest":
            self._send_json({"report": load_latest_report()})
            return

        if path == "/api/notifications":
            unread = (qs.get("unread") or ["0"])[0] in ("1", "true")
            # 范围：默认只看今天。历史通知攒着不清会把「今天要做什么」淹掉——
            # 打开就是一堆旧消息，久了就学会无视徽标。历史没有消失，
            # scope=all 就能看到，只是不再默认糊在脸上。
            scope, since = notification_scope(qs)
            # 只放行三类通知（范围决策）。列表、未读数、范围共用同一份口径，
            # 否则徽标会显示一堆点进去看不到的通知。
            kinds = sorted(notifications.KEEP)
            self._send_json({
                "notifications": db.list_notifications(
                    limit=50, unread_only=unread, types=kinds, since_date=since),
                "unread_count": db.count_unread_notifications(types=kinds, since_date=since),
                "scope": scope,
            })
            return

        if path == "/api/profile":
            self._send_json({"profile": db.get_profile()})
            return

        if path == "/api/ledger/mastery":
            ids = (qs.get("ids") or [""])[0]
            norm_ids = [x.strip() for x in ids.split(",") if x.strip()]
            self._send_json({"mastery": db.ledger_mastery(norm_ids)})
            return

        if path == "/api/ledger/from-reading":
            limit = int((qs.get("limit") or ["20"])[0])
            self._send_json({"items": db.load_ledger_from_reading(limit)})
            return

        self._serve_static(path)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            req = json.loads(body) if body else {}
        except json.JSONDecodeError:
            self._send_json({"error": "无效的 JSON"})
            return

        # /api/verify 例外：它是「拿口令换角色」的入口本身，不该要求先有角色
        if path == "/api/verify":
            role = resolve_role(req.get("token", ""))
            self._send_json({"ok": role is not None, "role": role})
            return

        role = self._role(qs)
        if path.startswith("/api/") and role is None:
            self._forbidden()
            return

        # 演示角色：零写入。挡在这里而不是逐个接口里判——
        # 逐接口判的话，以后新增一个写接口忘了加判断，
        # 表现是「演示用户也能改数据」，而且不会报错。
        if role == ROLE_DEMO and not demo_can_write(path):
            self._log_demo_access("POST", path, True)
            self._forbidden({"error": "readonly",
                             "message": "演示模式：可以随意翻看，但不会改动任何数据。"})
            return
        if role == ROLE_DEMO:
            self._log_demo_access("POST", path, False)

        if path == "/api/ledger/encounter":
            concepts = req.get("concepts") or []
            source = req.get("source") or {}
            added = updated = 0
            for c in concepts:
                r = db.upsert_ledger_concept(c, source)
                if r == "added":
                    added += 1
                elif r == "updated":
                    updated += 1
            self._send_json({"success": True, "added": added, "updated": updated})
            return

        if path == "/api/ledger/make-card":
            # 从「阅读来的概念」生成 T1 词汇卡，source_url 用 ledger:{norm_id} 关联台账
            norm_id = (req.get("norm_id") or "").strip()
            term = (req.get("term") or "").strip()
            if not norm_id or not term:
                self._send_json({"error": "缺少 norm_id 或 term"})
                return
            result = create_card(
                load_api_key(),
                topic=term,
                template="t1_vocab",
                source_url_override="ledger:" + norm_id,
            )
            self._send_json(result)
            return

        if path == "/api/grade":
            # 参考答案由服务端按 card_key 自己取，不再从前端收。
            # 此前前端把 reference_answer 一起传上来，等于把它先发给了浏览器。
            question, reference, points = (req.get("question", ""),
                                           req.get("reference_answer", ""),
                                           req.get("grading_points") or [])
            card_key = req.get("card_key") or ""
            if card_key:
                card = db.get_card(card_key)
                oq = review_flow.open_question_of(card) if (card and review_flow) else None
                if not oq:
                    self._send_json({"error": "这张卡没有可评分的简答题"})
                    return
                question = oq.get("question") or question
                reference = oq.get("reference_answer") or reference
                points = oq.get("grading_points") or points
            result = grade_answer(load_api_key(), question, reference, points,
                                  req.get("answer", ""))
            if isinstance(result, dict) and reference:
                # 参考答案**判分后**才回传：提交前不发，提交后要发，
                # 否则用户看不到可以对照的标准是什么。
                result = dict(result, reference_answer=reference)
            self._send_json(result)
            return

        if path == "/api/create":
            api_key = load_api_key()
            # 用户不再选类型：给了就用（内部调用），没给就按主题自己判断
            template = req.get("template") or _auto_template(
                api_key, req.get("topic") or "")
            result = create_card(
                api_key,
                topic=req.get("topic"),
                url=req.get("url"),
                template=template,
            )
            self._send_json(result)
            return

        if path == "/api/done":
            db.mark_done(req.get("date", ""), req.get("source_url", ""))
            self._send_json({"success": True})
            return

        if path == "/api/delete":
            ok = db.delete_card(req.get("source_url", ""))
            self._send_json({"success": ok})
            return

        if path == "/api/state":
            key = req.get("key", "")
            if key:
                db.set_user_state(key, req.get("value", ""))
            self._send_json({"success": True})
            return

        if path == "/api/event":
            db.record_event(
                req.get("date") or datetime.now().strftime("%Y-%m-%d"),
                req.get("event", ""),
                req.get("source_url", ""),
            )
            self._send_json({"success": True})
            return

        if path == "/api/notifications/read":
            ids = req.get("ids")
            db.mark_notifications_read([int(i) for i in ids] if isinstance(ids, list) and ids else None)
            self._send_json({"success": True})
            return

        if path == "/api/plan/outline":
            result = generate_outline(
                load_api_key(),
                req.get("topic", ""),
                req.get("template", "t4_trivia"),
                int(req.get("size", 20) or 20),
            )
            self._send_json(result)
            return

        if path == "/api/plan/disambiguate":
            result = disambiguate_topic(
                load_api_key(),
                req.get("topic", ""),
                req.get("template", "t4_trivia"),
            )
            self._send_json(result)
            return

        if path == "/api/plan/classify":
            result = classify_topic(load_api_key(), req.get("topic", ""))
            self._send_json(result)
            return

        if path == "/api/plan/create":
            result = create_plan(
                load_api_key(),
                req.get("title", ""),
                req.get("topic", ""),
                req.get("outline", []),
                req.get("cards_per_day", 3),
                req.get("template", "t4_trivia"),
                req.get("scale"),
                req.get("pace"),
            )
            self._send_json(result)
            return

        if path == "/api/plan/progress":
            plan_id = int(req.get("id", 0) or 0)
            plan = db.get_plan(plan_id)
            if not plan:
                self._send_json({"error": "计划不存在"})
                return
            cards = db.load_plan_cards(plan_id)
            self._send_json({
                "plan_id": plan_id,
                "generated": len(cards),
                "total": plan.get("total_cards") or 0,
                "status": plan.get("status"),
            })
            return

        if path == "/api/plan/continue":
            result = continue_plan(load_api_key(), req.get("id"))
            self._send_json(result)
            return

        if path == "/api/plan/update":
            fields = {k: req.get(k) for k in ("title", "cards_per_day", "status") if k in req}
            ok = db.update_plan(req.get("id"), fields)
            self._send_json({"success": ok})
            return

        if path == "/api/plan/delete":
            db.delete_plan(req.get("id"))
            self._send_json({"success": True})
            return

        if path == "/api/edit":
            source_url = req.get("source_url", "")
            fields = {k: req.get(k) for k in ("title", "summary", "body", "core_points", "quiz") if k in req}
            ok = db.update_card(source_url, fields)
            self._send_json({"success": ok})
            return

        if path == "/api/review":
            result = db.schedule_review(req.get("source_url", ""), req.get("quality", 1))
            self._send_json({"success": bool(result), "state": result})
            return

        if path == "/api/question/answer":
            # 服务端判卷。这是唯一一处知道答案的地方，返回的反馈里带
            # 「错误原因」与掌握度变化（方案 M4 要求用户能看到错在哪）。
            # 注意不能用 `int(x or -1)`：`0 or -1` 是 -1，第 1 题与选项 A
            # 永远会被判成越界。这个坑真踩过（e2e 抓到的）。
            feedback, err = review_flow.answer_question(
                req.get("card_key", ""), _int_arg(req.get("index")),
                _int_arg(req.get("chosen")),
                elapsed_ms=req.get("elapsed_ms"),
                hint_used=bool(req.get("hint_used")),
            )
            if err:
                self._send_json({"success": False, "error": err})
                return
            self._send_json({"success": True, "feedback": feedback})
            return

        if path == "/api/revision/apply":
            from weizhi.ops import revisions
            result, err = revisions.apply(_int_arg(req.get("id")), note=req.get("note"))
            if err:
                self._send_json({"success": False, "error": err})
                return
            self._send_json({"success": True, "result": result})
            return

        if path == "/api/revision/reject":
            from weizhi.ops import revisions
            result, err = revisions.reject(_int_arg(req.get("id")), note=req.get("note"))
            if err:
                self._send_json({"success": False, "error": err})
                return
            self._send_json({"success": True, "result": result})
            return

        if path == "/api/review/finish":
            result, err = review_flow.finish_card(
                req.get("card_key", ""),
                req.get("correct", 0), req.get("total", 0))
            if err:
                self._send_json({"success": False, "error": err})
                return
            self._send_json({"success": True, "result": result})
            return

        if path == "/api/review/master":
            db.mark_mastered(req.get("source_url", ""))
            self._send_json({"success": True})
            return

        if path == "/api/review/skip":
            db.skip_review_today(req.get("source_url", ""))
            self._send_json({"success": True})
            return

        if path == "/api/favorite":
            ok = db.toggle_favorite(req.get("source_url", ""), req.get("favorite", True))
            self._send_json({"success": ok})
            return

        if path == "/api/regen":
            result = regen_card(load_api_key(), req.get("source_url", ""))
            self._send_json(result)
            return

        if path == "/api/rollback":
            result = rollback_card(req.get("source_url", ""))
            self._send_json(result)
            return

        self.send_error(404)

    def _serve_static(self, path):
        # URL 保持根路径（/sw.js、/manifest.json），但文件实际在代码包的
        # serve/web/ 下。Service Worker 的作用域按 URL 算，所以 URL 不能跟着
        # 目录一起改，只换文件系统根。
        web_root = paths.web_dir()
        if path in ("/", "/index.html"):
            file_path = os.path.join(web_root, "reader.html")
        else:
            rel = path.lstrip("/")
            file_path = os.path.normpath(os.path.join(web_root, rel))
            if not file_path.startswith(web_root):
                self.send_error(403)
                return

        if not os.path.isfile(file_path):
            self.send_error(404)
            return

        content_type = "text/html; charset=utf-8"
        if file_path.endswith(".js"):
            content_type = "application/javascript; charset=utf-8"
        elif file_path.endswith(".css"):
            content_type = "text/css; charset=utf-8"
        elif file_path.endswith(".json"):
            content_type = "application/json; charset=utf-8"
        elif file_path.endswith(".svg"):
            content_type = "image/svg+xml"
        elif file_path.endswith(".png"):
            content_type = "image/png"
        elif file_path.endswith(".ico"):
            content_type = "image/x-icon"

        with open(file_path, "rb") as f:
            body = f.read()

        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _send_json(self, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        # 静默日志，保持终端输出干净
        pass


def main():
    db.init_db()
    fixed = db.auto_complete_plans()  # 修复存量：学完未归档的计划
    if fixed:
        print(f"  已自动归档 {fixed} 个学完未消失的任务")
    server = ThreadingHTTPServer(("127.0.0.1", PORT), ReaderHandler)
    print("=" * 44)
    print("  微知 · 碎片阅读器已启动")
    print(f"  请在浏览器打开： http://localhost:{PORT}")
    print(f"  数据库： {db.DB_PATH}")
    print(f"  可用日期： {', '.join(list_dates()) or '（无）'}")
    print("  按 Ctrl+C 停止")
    print("=" * 44)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止。")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
