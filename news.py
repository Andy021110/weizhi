# -*- coding: utf-8 -*-
"""微知 v2 · 资讯发现与生产闭环（CP13，方案 M5）。

方案 M5 的目标是「把资讯接进微知，但只把**与用户目标相关的高价值事件**转成
学习内容」。它列了四条要求，全部是确定性的：

1. **跨源去重**：同一事件多来源不重复生成卡片；
2. **来源优先级**：一手来源优先，二手内容要能回溯到一手；
3. **冲突不得静默合并**：两个来源对同一版本/数字说法不一致时，如实并列展示；
4. **快变内容要有效期**：时点事实必须带发布日期与重新核验时间。

还有一条容易被忽略但很重要：**没有高价值事件时允许当天不生产**。
资讯系统的默认失败模式是「为了不断更而硬产」，那会直接污染学习预算。

所以本模块不做任何语义判断，只做归一、去重、排序、时效计算。
「这个事件值不值得学」交给 `relevance()` 与阈值门禁。

用法::

    import news
    merged = news.merge(raw_events)          # 跨源去重 + 来源优先级
    picked = news.select_for_production(merged, spec, claims_index)
    if picked["status"] == "skip_day":
        ...                                   # 今天不生产，这是合法结果
"""
import hashlib
import re

from datetime import datetime, timedelta

# 来源优先级：数字越小越权威。一手来源优先（方案 M5）
SOURCE_TIERS = {
    "official": 0,      # 官方博客 / 发布说明 / 论文原文
    "paper": 0,
    "primary": 0,
    "media": 1,         # 权威媒体
    "analyst": 2,       # 专业机构 / 分析
    "blog": 3,          # 专业博客（含二手翻译）
    "social": 4,        # 自媒体 / 社交
}
DEFAULT_TIER = 3

# 时效窗口（天）。快变内容过期后必须重新核验，不能拿旧状态当现状讲。
VALIDITY_DAYS = {"fast": 7, "event": 30, "evolving": 180, "stable": None}
REVERIFY_DAYS = {"fast": 3, "event": 14, "evolving": 60, "stable": None}

_NORM_RE = re.compile(r"[\s，。、；：（）()\[\]【】,.;:!！?？\"'“”‘’\-—_/|]")
_TITLE_NOISE = re.compile(r"(选自|编译|转载|原文|全文|解读|独家|\|.*$)")

# 提取可比较的事实片段：数字 + 紧随其后的单位/词，用于跨源冲突检测
_FACT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([A-Za-z%％]{0,6}|[\u4e00-\u9fff]{0,3})")


def _norm(text):
    return _NORM_RE.sub("", (text or "").lower())


def _clean_title(title):
    """去掉来源标记与「选自/编译」这类转载噪音，否则同一事件会算出不同 key。"""
    return _TITLE_NOISE.sub("", title or "").strip()


def _published(event):
    raw = event.get("published_at") or event.get("published")
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw
    for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(str(raw)[:19], fmt)
        except ValueError:
            continue
    return None


def canonical_key(event):
    """事件的规范键：同一事件在不同来源必须算出同一个 key。

    用「清洗后的标题 + 主实体」而不是 URL——URL 天然每条都不同，
    拿它做键等于没有去重。
    """
    title = _norm(_clean_title(event.get("title")))
    entity = _norm(event.get("primary_entity") or "")
    # 标题前 40 字足够区分事件，也容忍后段的补充说明
    return hashlib.sha1(("%s|%s" % (title[:40], entity)).encode("utf-8")).hexdigest()[:16]


def tier_of(event):
    return SOURCE_TIERS.get((event.get("source_tier") or "").lower(), DEFAULT_TIER)


def facts(event):
    """抽出事件里的可比较事实（数字+单位），用于跨源冲突检测。"""
    text = "%s %s" % (event.get("title") or "", event.get("summary") or "")
    out = set()
    for num, unit in _FACT_RE.findall(text):
        out.add("%s%s" % (num, unit.lower()))
    return out


def normalize(event):
    """补默认值并算好规范键。不改动原始字段，只做加法。"""
    out = dict(event)
    out["canonical_key"] = event.get("canonical_key") or canonical_key(event)
    out["source_tier"] = (event.get("source_tier") or "blog").lower()
    out["tier_rank"] = tier_of(event)
    pub = _published(event)
    out["published_at"] = pub.isoformat(timespec="seconds") if pub else None
    out["kind"] = (event.get("kind") or "evolving").lower()
    if out["kind"] not in VALIDITY_DAYS:
        out["kind"] = "evolving"
    return out


def merge(events):
    """跨源去重：同一事件合并成一条，来源按权威度排序。

    方案 M5：「同一事件多来源不重复生成卡片」。同时保留全部来源，
    因为冲突要并列展示，而并列的前提是来源都还在。
    """
    groups = {}
    for raw in events or []:
        ev = normalize(raw)
        groups.setdefault(ev["canonical_key"], []).append(ev)

    merged = []
    for key, items in groups.items():
        items.sort(key=lambda e: (e["tier_rank"], e.get("published_at") or ""))
        primary = items[0]
        merged.append({
            "canonical_key": key,
            "title": primary.get("title"),
            "kind": primary["kind"],
            "published_at": primary["published_at"],
            "primary_source": {
                "name": primary.get("source"),
                "url": primary.get("url"),
                "tier": primary["source_tier"],
            },
            "sources": [
                {"name": i.get("source"), "url": i.get("url"), "tier": i["source_tier"],
                 "published_at": i["published_at"]}
                for i in items
            ],
            "source_count": len(items),
            # 一手可回溯性：来源里有没有 tier 0
            "traceable_to_primary": any(i["tier_rank"] == 0 for i in items),
            "conflicts": detect_conflicts(items),
        })
    merged.sort(key=lambda e: e.get("published_at") or "", reverse=True)
    return merged


def detect_conflicts(items):
    """同一事件内跨源的事实冲突。方案 M5 要求「同时展示冲突，不由模型静默合并」。

    判据是「可比较事实的集合不一致」。这里只做集合比对，不做语义判断——
    「2026-09-08 发布」与「2026 年 9 月 8 日发布」归一后应当一致。
    """
    if len(items) < 2:
        return []
    base = facts(items[0])
    conflicts = []
    for other in items[1:]:
        diff = (facts(other) - base) | (base - facts(other))
        if diff:
            conflicts.append({
                "a": items[0].get("source"),
                "b": other.get("source"),
                "differing_facts": sorted(diff),
            })
    return conflicts


def validity(event, now=None):
    """时点事实的有效期与重新核验时间（方案 M5 第 3 条）。

    `stable` 类（原理、定理）没有有效期——它们不会因为时间而失真，
    给它们安一个失效期限反而是噪音。
    """
    now = now or datetime.now()
    kind = event.get("kind") or "evolving"
    days = VALIDITY_DAYS.get(kind)
    pub = _published(event)
    out = {"kind": kind,
           "published_at": pub.isoformat(timespec="seconds") if pub else None,
           "valid_days": days,
           "expires_at": None,
           "reverify_at": None,
           "expired": False}
    if days is None or pub is None:
        return out
    expire = pub + timedelta(days=days)
    reverify = pub + timedelta(days=REVERIFY_DAYS.get(kind, days))
    out["expires_at"] = expire.isoformat(timespec="seconds")
    out["reverify_at"] = reverify.isoformat(timespec="seconds")
    out["expired"] = now > expire
    return out


def needs_reverify(event, now=None):
    """是否该重新核验了。过期内容不该拿旧状态当现状讲。"""
    info = validity(event, now)
    if not info["reverify_at"]:
        return False
    return (now or datetime.now()) >= datetime.fromisoformat(info["reverify_at"])


def select_for_production(events, spec=None, claims_index=None,
                          min_relevance=2, limit=3, now=None):
    """选出值得转成学习内容的事件。**允许一条都不选**。

    方案 M5：「没有高价值事件时允许当天不生产资讯包」。
    资讯系统最常见的失败模式是为了不断更而硬产，那会直接吃掉学习预算。

    `claims_index`: {url: [claims]}，用来算与当前目标的能力相关性。
    """
    import planner

    scored = []
    skipped = []
    for ev in merge(events):
        info = validity(ev, now)
        if info["expired"]:
            skipped.append({"title": ev.get("title"), "why": "已超出有效期，需要重新核验"})
            continue
        rel = 0
        if spec and claims_index:
            claims = claims_index.get(ev["primary_source"]["url"]) or []
            if claims:
                rel = max(planner.relevance(c, spec.get("milestones", [{}])[0], spec)
                          for c in claims)
        ev["relevance"] = rel
        if spec and rel < min_relevance:
            skipped.append({"title": ev.get("title"),
                            "why": "与当前目标相关性 %d，低于门槛 %d" % (rel, min_relevance)})
            continue
        scored.append(ev)

    scored.sort(key=lambda e: (-e.get("relevance", 0), e.get("published_at") or ""))
    picked = scored[:limit]

    if not picked:
        return {"status": "skip_day", "events": [], "skipped": skipped,
                "reason": "今天没有够得上门槛的资讯事件，不生产资讯包"
                          "（避免为了不断更挤占学习预算）"}
    return {
        "status": "ok",
        "events": picked,
        "skipped": skipped,
        "reason": ("从 %d 个事件里选出 %d 个（去重后 %d 个规范事件）"
                   % (len(events or []), len(picked), len(scored) + len(skipped))),
    }


def render_brief(events):
    """把事件渲染成给模型看的材料，冲突与时效信息都要带进去。"""
    lines = []
    for ev in events:
        info = validity(ev)
        lines.append("【%s】" % ev.get("title"))
        lines.append("来源：%s（权威度 %s）｜发布：%s"
                     % (ev["primary_source"]["name"], ev["primary_source"]["tier"],
                        info["published_at"] or "未标注"))
        if not ev.get("traceable_to_primary"):
            lines.append("注意：这是二手内容，没有找到一手来源。")
        if info["valid_days"]:
            lines.append("时效：这类内容 %d 天后失效，核验时间 %s"
                         % (info["valid_days"], info["reverify_at"]))
        for c in ev.get("conflicts") or []:
            lines.append("⚠️ 来源冲突：%s 与 %s 对 %s 的说法不一致，必须并列展示、不许合并。"
                         % (c["a"], c["b"], "、".join(c["differing_facts"])))
        lines.append("")
    return "\n".join(lines)
