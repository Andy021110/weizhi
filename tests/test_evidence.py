"""CP1 证据抽取测试：噪声过滤、偏移可回溯、数字标注、确定性、规划前置条件。"""
import evidence

SAMPLE = """
首页 | 订阅 | 阅读原文

Agent Harness 是一种把模型、工具与循环组织起来的执行框架。

```python
def run(agent, task):
    return agent.loop(task)
```

它的核心循环由上下文组装、模型调用、工具执行与状态回写四个阶段组成。

    缩进的伪代码也应该被丢掉
from openai import OpenAI

| 参数 | 默认值 | 说明 |
| --- | --- | --- |
| temperature | 0.6 | 采样温度 |

在 2025 年 3 月的评测中，该框架的准确率达到 87.5%，比基线高出 12 个百分点。

模型版本 v3.1.4 引入了并行工具调用。

首先，运行时会组装上下文；然后，模型决定调用哪个工具。

相关内容
https://example.com/related-post

这是一个被截断的半句，没有句号结尾所以应该

上一页 下一页 目录
"""


def test_clean_drops_code_table_nav():
    clean = evidence.clean_text(SAMPLE)
    assert "def run" not in clean
    assert "from openai import OpenAI" not in clean
    assert "缩进的伪代码" not in clean
    assert "| temperature |" not in clean
    assert "首页" not in clean
    assert "上一页" not in clean
    assert "https://example.com" not in clean
    # 正文该留的都在
    assert "执行框架" in clean
    assert "87.5%" in clean


def test_claims_offset_roundtrip():
    """每条 claim 都必须能用 start/end 从 clean_text 原样取回。"""
    clean = evidence.clean_text(SAMPLE)
    claims = evidence.extract_claims(clean)
    assert claims, "样本应能抽出证据"
    for c in claims:
        assert clean[c["start"]:c["end"]] == c["text"], "偏移不可回溯: %r" % c["text"]


def test_noise_never_becomes_claim():
    clean = evidence.clean_text(SAMPLE)
    texts = [c["text"] for c in evidence.extract_claims(clean)]
    joined = "\n".join(texts)
    for bad in ("def run", "temperature", "首页", "上一页", "被截断的半句"):
        assert bad not in joined, "噪声混进了证据: %s" % bad


def test_number_claims_are_flagged():
    clean = evidence.clean_text(SAMPLE)
    kinds = {c["text"]: c["kind"] for c in evidence.extract_claims(clean)}
    hit = [t for t, k in kinds.items() if k == "number"]
    assert any("87.5%" in t for t in hit), "带百分比与年份的句子应标为 number"
    assert any("v3.1.4" in t for t in hit), "版本号应标为 number"


def test_definition_and_step_are_flagged():
    clean = evidence.clean_text(SAMPLE)
    kinds = [c["kind"] for c in evidence.extract_claims(clean)]
    assert "definition" in kinds
    assert "step" in kinds


def test_extraction_is_deterministic():
    clean = evidence.clean_text(SAMPLE)
    a = evidence.extract_claims(clean)
    b = evidence.extract_claims(clean)
    assert a == b


def test_ingest_and_plan_gate(tmp_db):
    """至少两条可用证据才允许规划学习包（方案 3.1）。"""
    sid, claims = evidence.ingest_source("https://example.com/a", SAMPLE, title="Agent Harness")
    assert sid > 0
    assert len(claims) == len(evidence.extract_claims(evidence.clean_text(SAMPLE)))
    assert db_claim_count(sid) == len(claims)
    assert evidence.can_plan_pack(sid) is True

    # 只有一条证据的短新闻不足以成包
    sid2, _ = evidence.ingest_source("https://example.com/b", "某公司于 2026 年发布了新模型。")
    assert evidence.can_plan_pack(sid2) is False


def db_claim_count(sid):
    import db
    return db.count_v2_claims(sid)


def test_select_claims_caps_prompt_size():
    """1.7 万字的文章会抽出几百条证据，不设上限 prompt 会爆。"""
    many = [{"claim_idx": i, "text": "第 %d 条关于 Agent 循环机制的证据说明内容足够长。" % i,
             "kind": "fact"} for i in range(200)]
    picked = evidence.select_claims(many, limit=24)
    assert len(picked) <= 24
    assert len(picked) >= evidence.MIN_CLAIMS_FOR_PACK


def test_select_claims_keeps_original_order():
    """选完要排回原文顺序，否则模型产出的卡片逻辑是乱的。"""
    many = [{"claim_idx": i, "text": "证据内容 %d，长度足够通过筛选条件。" % i, "kind": "fact"}
            for i in range(30)]
    picked = evidence.select_claims(many, limit=10)
    idxs = [c["claim_idx"] for c in picked]
    assert idxs == sorted(idxs)


def test_select_claims_prefers_informative_kinds():
    claims = [
        {"claim_idx": 0, "text": "一句普通陈述，长度足够通过筛选条件。", "kind": "fact"},
        {"claim_idx": 1, "text": "该框架在 2025 年评测中准确率达到 87.5%。", "kind": "number"},
        {"claim_idx": 2, "text": "Agent Harness 是指一种执行框架。", "kind": "definition"},
    ] + [{"claim_idx": i + 3, "text": "填充用的普通陈述内容，长度足够通过筛选。" % (),
          "kind": "fact"} for i in range(20)]
    picked = evidence.select_claims(claims, limit=2)
    kinds = {c["kind"] for c in picked}
    assert kinds <= {"number", "definition"}, "信息量高的证据应优先入选，实际: %s" % kinds


def test_select_claims_dedupes_near_identical():
    dup = [
        {"claim_idx": 0, "text": "核心循环由上下文组装与状态回写两个阶段共同组成，缺一不可。", "kind": "fact"},
        {"claim_idx": 1, "text": "核心循环由上下文组装与状态回写两个阶段共同组成，缺一不可。", "kind": "fact"},
    ]
    picked = evidence.select_claims(dup, limit=10)
    assert len(picked) == 1


def test_select_claims_uses_goal_terms():
    goal = {"capability": "能说清电商智能体的分层结构", "scene": "", "success_evidence": ""}
    claims = [
        {"claim_idx": 0, "text": "天气预报告诉我们明天下雨的概率比较大，出门记得带伞。", "kind": "fact"},
        {"claim_idx": 1, "text": "电商智能体在架构上分为接入层、编排层与工具层三个层次。", "kind": "definition"},
    ]
    picked = evidence.select_claims(claims, goal, limit=1)
    assert "电商智能体" in picked[0]["text"]
