# -*- coding: utf-8 -*-
"""微知 v2 · 确定性证据抽取（CP1）。

职责边界（方案 4.1）：证据片段定位必须由程序完成，不交给模型。
模型可以判断「这条证据值不值得学」，但不能决定「证据存不存在」以及「原文在哪」。

为什么单独一个模块：v1 的 pipeline 是「抓全文 → 整段塞进 prompt → 模型自己找重点」，
事实无法回溯到原文，也没法做数字一致性校验。v2 要的是每条主张都能用
`clean_text[start:end]` 原样取回，所以抽取必须是确定性的、可重放的。

用法::

    from weizhi.produce import evidence
    clean = evidence.clean_text(raw_html_text)
    claims = evidence.extract_claims(clean)
    for c in claims:
        assert clean[c["start"]:c["end"]] == c["text"]   # 偏移永远成立
"""
import hashlib
import re

from weizhi.core import db

# 方案 3.1：至少两条相关核验证据才允许规划候选学习包。
MIN_CLAIMS_FOR_PACK = 2

_EMPTY_LINE = re.compile(r"^[ \t]*$")
_FENCE = re.compile(r"^\s*(```|~~~)")

# 代码特征：缩进、关键字、强符号。中文正文几乎不会同时命中这些。
_CODE_HINT = re.compile(
    r"(def |class |import |from [\w\.]+ import|return |print\(|console\.log|"
    r"npm |pip |git clone|sudo |\$\s|=>|::|!=|==|&&|\|\|)"
)
_CODE_SYMBOL = re.compile(r"[{};]|=>|::|\+=|->|!=")
_CODE_CHARS = "{}[]()<>;=+*/\\|&$#@~`"

# 表格特征：管道符、制表符、纯数字行、多列对齐数字
_TABLE_PIPE = re.compile(r"\|.*\|")
_TABLE_MULTICOL = re.compile(r"^[\w\u4e00-\u9fff\.\-]+\s{2,}[\d\.\-]+(\s{2,}[\d\.\-]+)+$")
_TABLE_NUMS = re.compile(r"^[\d\.\-\s%＋＋]+$")

# 导航/页脚/UI 文案
_NAV_HEAD = re.compile(
    r"^(首页|上一页|下一页|上一篇|下一篇|目录|订阅|关注我们|阅读原文|版权所有|"
    r"相关文章|相关阅读|标签|分类|分享|评论|登录|注册|返回|更多|详情|展开|收起|"
    r"Sign up|Subscribe|Share|Tweet|Follow|Read more|Related|Cookie|Privacy|"
    r"Terms|All rights reserved|©)",
    re.I,
)
_NAV_URL = re.compile(r"^https?://\S+$")
_NAV_DATE = re.compile(r"^[\d\s\.\-/年月]+$")

# 值得核验的数字：带单位、百分比、版本号、年份、日期。
# 单独的「3 个」这种计数不算——核验它对事实准确性没有意义。
_NUM_UNIT = re.compile(
    r"(%|％|倍|个百分点|毫秒|ms|秒|分钟|小时|天|周|GB|MB|KB|TB|亿|万|tokens?|token|层|轮|epoch)",
    re.I,
)
_NUM_VERSION = re.compile(r"\bv?\d+(\.\d+){1,3}\b")
_NUM_YEAR = re.compile(r"(19|20)\d{2}")
_NUM_DATE = re.compile(r"\d{1,2}月\d{1,2}日|\d{4}[-/]\d{1,2}[-/]\d{1,2}")
_DIGIT = re.compile(r"\d")

_DEFINITION = re.compile(r"(是指|指的是|定义为|称为|称作|即指|是一种|是一类|本质上是)")
_STEP = re.compile(r"^(首先|然后|接着|其次|最后|再次|第一步|第[一二三四五六七八九十]步|\d+\.\s)")

_TERMINAL = re.compile(r"[。！？!?；;：:]\s*$")
_LEADING_BROKEN = re.compile(r"^[，,、；;：:）)\]】]")

# 缩进 4 空格是代码的强信号，但嵌套列表也会被缩进——用列表符号排除掉
_LIST_MARK = re.compile(r"^([-*•]|\d+[.)、]|[（(]\d+[）)])\s")


def content_hash(text):
    """清洗后文本的 sha256，用于快照去重与变更检测。"""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()


def _is_code(line, original=None):
    """判代码。`original` 是未 strip 的原始行——缩进信息只在 strip 前存在，
    所以缩进判定必须用它，否则「    缩进的伪代码」这类行会漏进来。"""
    src = original if original is not None else line
    if src.startswith(("    ", "\t")) and not _LIST_MARK.match(line):
        return True
    if _CODE_HINT.search(line):
        return True
    if _CODE_SYMBOL.search(line):
        symbols = sum(1 for ch in line if ch in _CODE_CHARS)
        if len(line) and symbols / len(line) > 0.15:
            return True
    return False


def _is_table(line):
    if _TABLE_PIPE.search(line):
        return True
    if "\t" in line:
        return True
    if _TABLE_MULTICOL.match(line):
        return True
    if len(line) > 8 and _TABLE_NUMS.match(line):
        return True
    return False


def _is_nav(line):
    if _NAV_HEAD.match(line):
        return True
    if _NAV_URL.match(line):
        return True
    if _NAV_DATE.match(line) and len(line) <= 20:
        return True
    return False


def clean_text(raw):
    """清洗原始文本：去代码块、表格行、导航/页脚，归一化空白。

    输出是后续一切偏移的基准——`extract_claims` 的 start/end 只对本函数的
    返回值有效，不对原始 raw 有效。这是有意为之：原始文本里混着 HTML 噪声，
    用它做偏移既不稳也不可读。
    """
    lines = (raw or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    kept = []
    in_fence = False
    for line in lines:
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        stripped = line.strip()
        if not stripped:
            kept.append("")
            continue
        if _is_nav(stripped) or _is_table(stripped) or _is_code(stripped, line):
            continue
        kept.append(stripped)

    text = "\n".join(kept)
    text = re.sub(r"[ \t]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _iter_segments(clean):
    """按段落再按句切分，产出 (start, end, text)，保证 text == clean[start:end]。"""
    pos = 0
    for para in clean.split("\n"):
        if not para.strip():
            pos += len(para) + 1
            continue
        for m in re.finditer(r"[^。！？!?；;]+[。！？!?；;]?", para):
            raw = m.group(0)
            if not raw.strip():
                continue
            lead = len(raw) - len(raw.lstrip())
            start = pos + m.start() + lead
            end = pos + m.start() + len(raw.rstrip())
            yield start, end, raw.strip()
        pos += len(para) + 1


def _is_residual(seg):
    """残句：被截断、半句开头、或短到没有信息量的片段。

    抓全文时最容易产生的就是这类碎片——它们混进 prompt 后会被模型当成事实，
    所以必须在抽取阶段就丢掉，而不是指望模型分辨。
    """
    if len(seg) < 10:
        return True
    if _LEADING_BROKEN.match(seg):
        return True
    if not _TERMINAL.search(seg) and len(seg) < 25:
        return True
    if re.match(r"^[a-z]", seg) and len(seg) < 40:
        return True
    return False


def _has_checkable_number(seg):
    """是否含值得核验的数字（带单位/百分比/版本号/年份/日期）。"""
    if not _DIGIT.search(seg):
        return False
    return bool(
        _NUM_UNIT.search(seg)
        or _NUM_VERSION.search(seg)
        or _NUM_YEAR.search(seg)
        or _NUM_DATE.search(seg)
    )


def classify(seg):
    """给证据片段打类型：number / definition / step / fact。"""
    if _has_checkable_number(seg):
        return "number"
    if _DEFINITION.search(seg):
        return "definition"
    if _STEP.match(seg):
        return "step"
    return "fact"


def extract_claims(clean):
    """从清洗后文本抽取证据片段。确定性：同一输入两次结果完全一致。"""
    claims = []
    for start, end, seg in _iter_segments(clean):
        if _is_residual(seg) or _is_nav(seg) or _is_code(seg) or _is_table(seg):
            continue
        claims.append({
            "claim_idx": len(claims),
            "text": seg,
            "start": start,
            "end": end,
            "kind": classify(seg),
            "usable": True,
            "meta": {"len": len(seg)},
        })
    return claims


def ingest_source(url, raw, title=None, site=None, lang=None, snapshot_path=None,
                  meta=None):
    """清洗 → 抽取 → 落库。返回 (source_id, claims)。

    同 url 重复 ingest 会覆盖（先清 claims 再写），保证重跑不累积、可重放。

    `meta` 用来记这条材料的**来源属性**（来源 id / 等级 / 主题）。
    为什么必须记在 source 上、而不只记在卡上：卡上只留了一个「权威度」标签，
    而「这条材料来自哪个源的哪一级」是判断可信度的原始依据——
    只留结论，事后无法复核。
    """
    clean = clean_text(raw)
    claims = extract_claims(clean)
    source_id = db.save_v2_source(
        url=url,
        title=title,
        site=site,
        lang=lang,
        content_hash=content_hash(clean),
        clean_text=clean,
        snapshot_path=snapshot_path,
        meta=meta,
    )
    db.save_v2_claims(source_id, claims)
    return source_id, claims


# Claim Selector 的权重：带数字和定义的事实信息量最高，步骤与一般陈述次之
_KIND_WEIGHT = {"number": 3, "definition": 3, "fact": 2, "step": 2}

# 太短的片段信息量不足，太长的通常是没切开的长句。
# 下限设 15 而非 25：带硬数字的短句（「准确率达 87.5%」）恰恰是最该保留的证据。
_MIN_CLAIM_LEN = 15
_MAX_CLAIM_LEN = 200

_DEFAULT_MAX_CLAIMS = 24


def _norm_for_dedupe(text):
    return re.sub(r"[\s，。、；：（）()\[\]【】,.;:!！?？\"'“”‘’]", "", (text or "").lower())


def _relevance(claim, terms):
    """与学习目标的重合度：用字符二元组算，不引入分词依赖。"""
    if not terms:
        return 0
    text = claim.get("text") or ""
    hit = sum(1 for t in terms if t in text)
    return min(hit, 3)


# 含这些字的 n-gram 没有区分度（「能说清」「的分层」这类），直接丢掉
_TERM_STOP_CHARS = set("的了和与是在我有能说清判断自己项目场景适用边界完成复述要会可对把被让")


def _goal_terms(goal):
    """从 GoalSpec 抽出用于相关性打分的关键词。

    用滑动窗口取 3-gram / 2-gram，而不是贪婪切 2-4 字——后者会把
    「能说清电商智能体」切成「能说清电|商智能体」，两个都对不上原文。
    """
    if not goal:
        return []
    raw = " ".join(str(goal.get(k) or "") for k in ("capability", "scene", "success_evidence"))
    terms = []
    for chunk in re.findall(r"[\u4e00-\u9fff]{2,}", raw):
        for n in (3, 2):
            for i in range(len(chunk) - n + 1):
                gram = chunk[i:i + n]
                if not (set(gram) & _TERM_STOP_CHARS):
                    terms.append(gram)
    # 长词优先（3-gram 比 2-gram 更有区分度），同长度保持原顺序
    terms.sort(key=lambda t: -len(t))
    return list(dict.fromkeys(terms))[:15]


def select_claims(claims, goal=None, limit=_DEFAULT_MAX_CLAIMS):
    """从候选证据里挑最值得学的若干条，把 prompt 控制在可承受范围。

    为什么必须有这一步：一篇 1.7 万字的文章会抽出 385 条证据，全塞进 prompt
    既不经济（方案 2.4 要求成本适合单用户自托管），也会让模型试图覆盖全部内容，
    把一张卡写成一篇综述。方案 4.2 把「判断哪些证据值得学」交给 Claim Selector，
    M1 阶段先用确定性规则选候选集，语义判断留到 M2。

    选完按 claim_idx 排回原文顺序——打乱顺序会让模型产出逻辑混乱的卡片。
    """
    terms = _goal_terms(goal)
    scored = []
    seen = set()
    for c in claims or []:
        if not c.get("usable", True):
            continue
        text = (c.get("text") or "").strip()
        if not (_MIN_CLAIM_LEN <= len(text) <= _MAX_CLAIM_LEN):
            continue
        key = _norm_for_dedupe(text)
        if not key or key in seen:
            continue
        seen.add(key)
        score = _KIND_WEIGHT.get(c.get("kind"), 1) * 2 + _relevance(c, terms)
        scored.append((score, c))

    scored.sort(key=lambda x: (-x[0], x[1].get("claim_idx", 0)))
    # 尊重调用方给定的 limit。是否够组成一个学习包由调用方判断
    # （write_card 会用 MIN_CLAIMS_FOR_PACK 拦），这里不替它做决定。
    picked = [c for _s, c in scored[:limit]]
    picked.sort(key=lambda c: c.get("claim_idx", 0))
    return picked


def can_plan_pack(source_id):
    """是否满足「至少两条可用证据」的规划前置条件（方案 3.1）。"""
    return db.count_v2_claims(source_id) >= MIN_CLAIMS_FOR_PACK
