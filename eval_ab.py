# -*- coding: utf-8 -*-
"""微知 v2 · M1 的 A/B 评测脚手架（CP5，对应方案第 11 节）。

这一步要验证的是最关键假设：**受证据约束的模型重写，是否真的比现有模板更有学习价值。**

脚本只做三件事：
1. 对同一批证据产出两个版本——规则版（v1 基线，不做教学重写）与模型版（v2）；
2. 随机编号成 A/B，让评审人不知道哪个是哪版；
3. 生成待填评分表。

**脚本不判定胜负。** 方案写得很清楚：只有模型版在事实质量不下降的前提下
显著提高教学评分，才继续进入 M2/M3。这个判断必须人来做。

用法::

    python eval_ab.py --demo                      # 内置样例，离线跑通
    python eval_ab.py --materials my.json --seed 7
    python eval_ab.py --demo --provider deepseek  # 需 config.json 里的 API Key

材料文件格式（JSON 数组）::

    [{"title": "Hugging Face 文本生成教程", "url": "https://...", "text": "原文..."}]

可选按材料指定学习目标，让「一目标一卡」成立（不填则用通用目标）::

    [{"title": "...", "url": "...", "text": "...",
      "capability": "能说清 X 的机制", "scene": "在 Y 场景下判断要不要用",
      "success_evidence": "能不查资料复述并说出一个不适用场景"}]

已知局限（必须看清再用结论）：
默认用 FakeTextProvider，模型版的结构合规但**内容是合成的**，只能验证脚手架
是否跑通，不能用来判断教学价值。真实结论必须用 `--provider deepseek` 跑真模型。
"""
import argparse
import json
import os
import random
import sys
from datetime import datetime

import card_gates
import card_writer
import db
import evidence
import schema_v2
from providers import DeepSeekProvider, FakeTextProvider, ProviderError

# 方案 11 的盲评维度。前四项越高越好，阅读负荷单独看（越低越好）。
DIMENSIONS = [
    ("事实准确", "每个事实都能追溯到材料，没有编造的数字或限定条件"),
    ("解释深度", "讲清了是什么与为什么，不是复述原文"),
    ("同质化", "低分 = 通用模板腔；高分 = 有针对这段材料的独特组织"),
    ("迁移价值", "看完能用到另一个场景，迁移任务不是原文例子换皮"),
    ("阅读负荷", "5 分 = 很轻；1 分 = 很重。这一项越低越好"),
]

# 每项的 1/3/5 分长什么样。没有锚点的话，不同人（或不同天的同一个人）
# 打出来的分不可比，三个人的盲评也汇总不出结论。
ANCHORS = {
    "事实准确": {
        5: "每个事实都能在材料里定位到，数字、日期、版本一个不差",
        3: "主体准确，但有 1-2 处无法追溯、或数字/限定条件对不上",
        1: "出现编造的数字或日期，或把 A 的说法安到 B 头上",
    },
    "解释深度": {
        5: "是什么、为什么、怎么用都讲清了，读之前不懂、读完能给别人讲",
        3: "讲清了是什么，但「为什么是这样」讲得浅或跳过了",
        1: "基本是原文复述或摘要，没增加理解",
    },
    "同质化": {
        5: "组织方式明显是为这份材料设计的，换一篇文章就不成立",
        3: "结构是通用的，但内容还算贴着这份材料",
        1: "套话连篇，把材料名换掉照样能读",
    },
    "迁移价值": {
        5: "读完能用到另一个场景，迁移任务是原文没出现过的新场景",
        3: "能复述结论，但换个场景就不知道怎么用了",
        1: "只能记住原文说了什么，迁移任务是原文例子换皮",
    },
    "阅读负荷": {
        5: "很轻，5 分钟内读完且不费劲",
        3: "中等，需要集中注意力，约 8 分钟",
        1: "很重，读不完或读得很吃力",
    },
}

# 判定门槛：差值多少算「显著提高」。方案只说「显著」，这里给个可执行口径。
MIN_GAIN = 1.0

DEMO_MATERIALS = [
    {
        "title": "Agent Harness 的执行循环",
        "url": "https://example.com/agent-harness",
        "text": """
Agent Harness 是一种把模型、工具与循环组织起来的执行框架。

它的核心循环由上下文组装、模型调用、工具执行与状态回写四个阶段组成。
每一轮循环结束后，框架会把工具返回的结果追加回上下文，作为下一轮的输入。

```python
def step(ctx):
    return model.call(assemble(ctx))
```

| 阶段 | 输入 | 输出 |
| --- | --- | --- |
| 上下文组装 | 历史消息 | 完整提示词 |
| 工具执行 | 工具调用请求 | 工具结果 |

在 2025 年 3 月的评测中，该框架的准确率达到 87.5%，比基线高出 12 个百分点。
模型版本 v3.1.4 引入了并行工具调用，把多工具场景的耗时压到了原来的三分之一。

需要注意的是，这套循环假设工具调用是幂等的；涉及写操作的工具需要额外的确认层。

首页 | 订阅 | 阅读原文
""",
    },
]


def rule_version(claims, material):
    """规则版基线（v1 思路）：按原文顺序拼接证据，不做教学重写。

    规则版是「抓正文 → 套模板」这条链路在无模型时的确定性近似：不重新组织、
    不解释、不补边界、不给迁移任务。它刻意保留了 v1 的典型特征——
    原文怎么说就怎么排，读起来像摘要而不像讲解。
    """
    picked = [c for c in claims if c.get("usable", True)][:6]
    body = "\n".join(c["text"] for c in picked)
    return {
        "version": "rule",
        "title": material.get("title") or "未命名材料",
        "objective": "",
        "lead": (picked[0]["text"] if picked else "")[:60],
        "body": body,
        "key_points": [c["text"][:30] for c in picked[:3]],
        "transfer_task": "",
        "cites": [c["claim_idx"] for c in picked],
    }


def rule_version_v1(v1_provider, material, clean_text):
    """v1 真实基线：整篇正文 + T2 精读模板 → 模型出卡。

    这才是方案 11 说的「当前规则版」——v1 也调用了模型，只是它把整篇正文
    丢给模型让它自己找重点，事实无法回溯。用「原文拼接」当基线是不公平的：
    那是在拿无模型方案和有模型方案比，会人为抬高模型版。
    """
    raw = v1_provider.generate_json(
        "t2_reading",
        {"required": ["title", "summary", "body", "core_points"]},
        {
            "source": material.get("site") or material.get("title") or "未标注来源",
            "title": material.get("title") or "",
            "content": clean_text[:8000],
            "url": material.get("url") or "",
        },
    )
    return {
        "version": "rule",
        "baseline": "v1-t2-template",
        "title": raw.get("title", ""),
        "objective": "",
        "lead": raw.get("summary", ""),
        "body": raw.get("body", ""),
        "key_points": raw.get("core_points") or [],
        "transfer_task": "",
        "cites": [],
    }


class _V1T2Provider(DeepSeekProvider):
    """复用 v1 的 T2 精读模板，只借 DeepSeekProvider 的调用与审计能力。"""

    name = "deepseek-v1-t2"

    def build_messages(self, task, inputs):
        from prompts import T2_SYSTEM, T2_USER
        return [
            {"role": "system", "content": T2_SYSTEM},
            {"role": "user", "content": T2_USER.format(**inputs)},
        ]


def model_version(provider, goal, claims, material, source_id):
    """模型版（v2）：证据约束的教学重写，带质量门禁。"""
    try:
        draft, _draft_id, report = card_writer.write_card_gated(
            provider, goal, claims,
            source={"title": material.get("title"), "url": material.get("url")},
            source_id=source_id,
        )
    except ProviderError as exc:
        return {"version": "model", "error": str(exc)}
    return {
        "version": "model",
        "title": draft.get("title", ""),
        "objective": draft.get("objective", ""),
        "lead": draft.get("lead", ""),
        "body": "\n".join(
            b.get("text", "") for _k, b in schema_v2.iter_blocks(draft)
        ),
        "key_points": draft.get("key_points", []),
        "transfer_task": draft.get("transfer_task", ""),
        "boundaries": "\n".join(
            b.get("text", "") for k, b in schema_v2.iter_blocks(draft) if k == "boundaries"
        ),
        "cites": sorted({
            c for _k, b in schema_v2.iter_blocks(draft) for c in (b.get("cites") or [])
        }),
        "gate_passed": report.get("passed"),
        "gate_issues": [i["tag"] for i in report.get("issues", [])],
    }


def build_goal(material):
    """为一份材料构造 GoalSpec。材料自带 capability 时优先用它——
    三篇不同文章共用一个泛化目标，产出的卡片也会一样泛。"""
    return schema_v2.make_goal(
        key="ab-eval",
        capability=material.get("capability") or "能说清材料讲的核心机制，并判断它适用到什么边界",
        level=material.get("level") or "有基础但不系统",
        scene=material.get("scene") or "读完能在自己的项目里判断要不要用",
        success_evidence=material.get("success_evidence")
        or "能不查资料复述核心机制并说出一个不适用场景",
        prereq=material.get("prereq") or [],
        milestones=material.get("milestones") or [],
        daily_minutes=60,
    )


def build_pair(material, goal, provider, v1_provider=None):
    """对一份材料产出 (规则版, 模型版)。

    `v1_provider` 给了就走真实 v1 基线（T2 模板 + 模型），
    否则退回无模型的原文拼接——后者只能跑通流程，不能用作评判基线。
    """
    clean = evidence.clean_text(material["text"])
    claims = evidence.extract_claims(clean)
    if len(claims) < evidence.MIN_CLAIMS_FOR_PACK:
        return None
    source_id = db.save_v2_source(
        url=material.get("url") or "eval:%s" % material.get("title"),
        title=material.get("title"),
        content_hash=evidence.content_hash(clean),
        clean_text=clean,
    )
    db.save_v2_claims(source_id, claims)
    rule = (rule_version_v1(v1_provider, material, clean) if v1_provider
            else rule_version(claims, material))
    return {
        "material": material.get("title"),
        "url": material.get("url"),
        "claim_count": len(claims),
        "rule": rule,
        "model": model_version(provider, goal, claims, material, source_id),
    }


def _render_version(v, mode="body"):
    """渲染一个版本。

    mode="body" 只出标题/导语/正文/关键点，把「有没有迁移任务和引用」这类
    结构差异藏起来——否则读者一眼认出新方案，盲评就不成盲评了。
    mode="full" 出全部字段，但**绝不标注版本身份**：空字段整行省略，
    不写「规则版」「（无）」这些字——它们等于直接告诉读者哪版是新的。
    """
    lines = []
    if v.get("title"):
        lines += ["**标题**：" + v["title"], ""]
    if v.get("lead"):
        lines += ["**导语**：" + v["lead"], ""]
    lines += ["**正文**：", "", v.get("body") or "", ""]
    if v.get("key_points"):
        lines.append("**关键点**：")
        lines.extend("- " + str(k) for k in v["key_points"])
        lines.append("")

    if mode == "body":
        return "\n".join(lines).strip()

    if v.get("objective"):
        lines += ["**学习目标**：" + v["objective"], ""]
    if v.get("boundaries"):
        lines += ["**边界**：" + v["boundaries"], ""]
    if v.get("transfer_task"):
        lines += ["**迁移任务**：" + v["transfer_task"], ""]
    if v.get("cites"):
        lines.append("**引用证据编号**：" + ", ".join("#%s" % c for c in v["cites"]))
    return "\n".join(lines).strip()


def blind_pairs(pairs, seed):
    """随机决定每对里 A 是规则版还是模型版，并记录答案供事后揭盲。"""
    rng = random.Random(seed)
    blinded = []
    for i, pair in enumerate(pairs):
        rule_first = rng.random() < 0.5
        blinded.append({
            "idx": i + 1,
            "material": pair["material"],
            "A": pair["rule"] if rule_first else pair["model"],
            "B": pair["model"] if rule_first else pair["rule"],
            "_answer": {"A": "rule" if rule_first else "model",
                        "B": "model" if rule_first else "rule"},
        })
    return blinded


def render_blind_md(blinded, seed, provider_kind="fake", mode="body"):
    heading = "盲评材料（M1 A/B）· 只看正文" if mode == "body" else "材料全文（M1 A/B）"
    out = [
        "# " + heading,
        "",
        "> 生成时间：%s ｜ 随机种子：%s ｜ Provider：%s"
        % (datetime.now().strftime("%Y-%m-%d %H:%M"), seed, provider_kind),
        ">",
        "> 下面是若干组材料，每组有 A、B 两个版本，**你不知道哪个是新方案**。",
        "> 请先读完一组再打分，不要在两版之间来回对照细节。",
        "> 评判依据是「是否真的有助于学会」，不是「哪版写得更漂亮」。",
    ]
    if mode == "body":
        out += [
            ">",
            "> 这一份**只渲染了标题/导语/正文/关键点**，刻意隐去了学习目标、边界、",
            "> 迁移任务和引用——那些字段的有无本身就是新旧方案的差异，露出来就不叫盲评了。",
            "> 想看完整字段请读 `blind_full.md`（看完就知道哪版是哪个，建议最后再看）。",
        ]
    if provider_kind == "fake":
        out += [
            ">",
            "> ⚠️ **本批由 Fake Provider 生成，盲评不成立，请勿据此下结论。**",
            "> 它的唯一用途是确认脚手架跑得通；真实结论要加 `--provider deepseek` 重跑。",
        ]
    out.append("")
    for item in blinded:
        out.append("---")
        out.append("")
        out.append("## 第 %d 组 · %s" % (item["idx"], item["material"]))
        out.append("")
        for label in ("A", "B"):
            out.append("### 版本 %s" % label)
            out.append("")
            out.append(_render_version(item[label], mode))
            out.append("")
    return "\n".join(out)


def render_score_md(blinded, seed):
    cols = list(DIMENSIONS) + [("教学分小计", "解释+同质化+迁移，满分 15")]
    header = "| 组 | 材料 | 版本 | " + " | ".join(d for d, _ in cols) + " | 一句话理由 |"
    sep = "|---|---|---|" + "---|" * (len(cols) + 1)
    rows = [header, sep]
    for item in blinded:
        for label in ("A", "B"):
            # 标题里常带竖线（「xxx | 选自 yyy」），不转义会串列
            title = str(item["material"]).replace("|", "\\|")
            rows.append("| %d | %s | %s | " % (item["idx"], title, label)
                        + " | ".join("" for _ in cols) + " |  |")
    out = [
        "# 评分表（M1 A/B）",
        "",
        "> 随机种子：%s（答案见同目录 answers.json，**评完分再揭盲**）" % seed,
        ">",
        "> 每项 1-5 分。**阅读负荷是反向指标**（5=很轻，1=很重），其余越高越好。",
        "> 打分锚点见下方「打分锚点」表，别凭感觉打。",
        "",
    ]
    out.extend(rows)
    out.append("")
    out.append("## 打分锚点")
    out.append("")
    out.append("每项 1-5 分，只写 1/3/5 这三个档；觉得在两档之间就写 2 或 4。")
    out.append("")
    out.append("| 维度 | 1 分 | 3 分 | 5 分 |")
    out.append("|---|---|---|---|")
    for name, _desc in DIMENSIONS:
        a = ANCHORS[name]
        out.append("| **%s**%s | %s | %s | %s |" % (
            name,
            "（反向）" if name == "阅读负荷" else "",
            a[1], a[3], a[5],
        ))
    out.append("")
    out.append("## 汇总与判定")
    out.append("")
    out.append("先算出每个版本的两项汇总分（同一材料内比较）：")
    out.append("")
    out.append("- **教学分** = 解释深度 + 同质化 + 迁移价值 → 满分 15，越高越好")
    out.append("- **阅读负荷**单独看 → 越低越好（5=很轻，1=很重）")
    out.append("- **事实准确**是**一票否决项**，不参与求平均")
    out.append("")
    out.append("### 进入 M2/M3 的条件（三条全满足）")
    out.append("")
    out.append("1. 模型版 **事实准确 ≥ 规则版**（新方案不能为了好读牺牲事实质量）；")
    out.append("2. 模型版 **教学分 − 规则版教学分 ≥ %.1f**（满分 15，即平均每项至少高 %.1f）；"
               % (MIN_GAIN * 3, MIN_GAIN))
    out.append("3. 模型版 **阅读负荷 ≥ 规则版**（负荷分越高越轻，即不能更累）。")
    out.append("")
    out.append("任一条件不满足 → 回到 M1 内部调提示词与门禁阈值，不要进入 M2。")
    out.append("")
    out.append("## 理由要不要写")
    out.append("")
    out.append("一句话理由**建议写，但不强求**。只在三种情况下**必须写**：")
    out.append("")
    out.append("1. 打了 1 分或 2 分——不写清哪里坏了，后面没法改；")
    out.append("2. 打了 5 分——说清好在哪，才知道该保留什么；")
    out.append("3. **同质化**和**迁移价值**这两项主观性最强，建议无论如何写一句。")
    out.append("")
    out.append("中间分（3-4 分）可以只打分不写理由。")
    return "\n".join(out)


def main(argv=None):
    ap = argparse.ArgumentParser(description="M1 A/B 评测脚手架（方案第 11 节）")
    ap.add_argument("--demo", action="store_true", help="用内置样例材料，离线跑通")
    ap.add_argument("--materials", help="材料 JSON 文件：[{title,url,text}]")
    ap.add_argument("--out", default="eval_out", help="输出目录，默认 eval_out")
    ap.add_argument("--seed", type=int, default=20260910, help="盲评随机编号种子")
    ap.add_argument("--provider", default="fake", choices=["fake", "deepseek"],
                    help="模型版用哪个 Provider；fake 只能验证流程，不能得结论")
    ap.add_argument("--rule-baseline", default="concat", choices=["concat", "v1"],
                    help="规则版基线：concat=原文拼接（无模型，仅跑通流程）；"
                         "v1=真实 v1 基线（T2 模板 + 模型，评判必须用这个）")
    args = ap.parse_args(argv)

    if not args.demo and not args.materials:
        ap.error("需要 --demo 或 --materials")

    if args.materials:
        with open(args.materials, encoding="utf-8") as fh:
            materials = json.load(fh)
    else:
        materials = DEMO_MATERIALS

    provider = _make_provider(args.provider)
    v1_provider = None
    if args.rule_baseline == "v1":
        if args.provider != "deepseek":
            ap.error("--rule-baseline v1 需要 --provider deepseek")
        from providers import DeepSeekProvider
        import json as _json
        key = _json.load(open(os.path.join(
            os.path.dirname(os.path.abspath(__file__)), "config.json"),
            encoding="utf-8"))["deepseek_api_key"]
        v1_provider = _V1T2Provider(api_key=key)

    db.init_db()

    pairs, skipped = [], []
    for m in materials:
        pair = build_pair(m, build_goal(m), provider, v1_provider)
        (pairs if pair else skipped).append(pair or m.get("title"))

    blinded = blind_pairs(pairs, args.seed)

    outdir = os.path.join(args.out, datetime.now().strftime("%Y-%m-%d"))
    os.makedirs(outdir, exist_ok=True)
    _write(os.path.join(outdir, "blind_body.md"),
           render_blind_md(blinded, args.seed, args.provider, "body"))
    _write(os.path.join(outdir, "blind_full.md"),
           render_blind_md(blinded, args.seed, args.provider, "full"))
    _write(os.path.join(outdir, "score_sheet.md"), render_score_md(blinded, args.seed))
    _write(os.path.join(outdir, "answers.json"),
           json.dumps({"seed": args.seed,
                       "answers": {b["idx"]: b["_answer"] for b in blinded}},
                      ensure_ascii=False, indent=2))
    _write(os.path.join(outdir, "pairs.json"),
           json.dumps(pairs, ensure_ascii=False, indent=2, default=str))

    print("✅ 盲评材料已生成：%s" % outdir)
    print("   材料组数：%d ｜ 跳过（证据不足）：%d" % (len(pairs), len(skipped)))
    if skipped:
        print("   ⏭️  跳过：%s" % "、".join(str(s) for s in skipped))
    if args.provider == "fake":
        print("⚠️  用的是 Fake Provider：模型版内容由脚本合成，只能验证流程跑通，")
        print("   不能用来判断教学价值。真实结论请加 --provider deepseek。")
    print("👉 先填 score_sheet.md，再对照 answers.json 揭盲。脚本不会替你判定胜负。")
    return 0


def _fake_body(inputs):
    """离线假模型：正文部分。刻意不写阿拉伯数字，避免数字门禁误报干扰脚手架自检。"""
    import re
    idxs = [int(n) for n in re.findall(r"\[#(\d+)\]", inputs.get("evidence_block", ""))]
    a = idxs[0] if idxs else 0
    b = idxs[1] if len(idxs) > 1 else a
    return {
        "schema_version": schema_v2.SCHEMA_VERSION,
        "objective": inputs.get("objective", "")[:60] or "说清材料讲的核心机制",
        "title": "【合成】" + (inputs.get("source_title") or "未命名材料"),
        "lead": "这是 Fake Provider 产出的合成内容，仅用于验证脚手架链路是否跑通，不代表真实生成质量。",
        "explanation": [
            {"text": "（合成文本）该机制由若干相互衔接的环节组成，每个环节都把上一环的输出当成自己的输入。"
                     "理解了这一点就能解释为什么判断问题出在哪一环，比笼统地评价整体表现更有用："
                     "整体表现只是一个结果，环节才是可以下手改的地方。",
             "cites": [a, b]},
            {"text": "（合成文本）这些环节不是并列关系而是串联关系，顺序本身携带信息。"
                     "跳过中间任何一环，后面的环节就拿不到它需要的东西，于是表现会突然崩掉而不是平滑下降——"
                     "这种「断崖式」的失败模式，正是判断哪一环掉了的最实用线索。"
                     "也因此，优化时盯着整体指标往往看不出该改哪一环，必须先把链条拆开看。",
             "cites": [b]},
            {"text": "（合成文本）把链条拆开之后，排查就有了固定顺序：先看输入装了什么，"
                     "再看中间处理是否按预期发生，最后看结果有没有被正确传下去。"
                     "这个顺序之所以有效，是因为串联结构里问题只可能发生在某一环，"
                     "而每一环都能单独观测。",
             "cites": [b]},
        ],
        "examples": [
            {"text": "（合成文本）一次完整调用会按顺序走完这些环节，中途任一步的结果都会被记录下来。"
                     "这份记录不只是日志，它同时是下一环的输入、也是事后复盘的依据，"
                     "所以这整个机制天然是可观测、可调试的。反过来说，如果你看不到中间环节，"
                     "就只能对着最终结果猜，排查成本会高一个量级。",
             "cites": [b]},
        ],
    }


def _fake_structure(inputs):
    """离线假模型：结构部分。"""
    import re
    idxs = [int(n) for n in re.findall(r"\[#(\d+)\]", inputs.get("evidence_block", ""))]
    c = idxs[2] if len(idxs) > 2 else (idxs[0] if idxs else 0)
    return {
        "boundaries": [
            {"text": "（合成文本）这套划分来自材料给出的场景；换到材料未覆盖的场景时，"
                     "需要先验证前提是否还成立，不能直接照搬。"
                     "这一点容易被忽略：结构看起来是通用的，但它成立的前提往往写在材料里而不是标题里。",
             "cites": [c]},
        ],
        "key_points": ["由若干相互衔接的环节组成", "上一环输出即下一环输入", "每个环节都是可观测的调试点"],
        "transfer_task": "（合成文本）挑一个你熟悉的同类系统，指出它对应的中间环节在哪，并说明跳过后会怎样。",
    }


def _fake_draft(inputs):
    """保留旧签名：返回一份完整草稿（两段拼起来），供不关心拆分的调用方使用。"""
    return schema_v2.merge_card(_fake_body(inputs), _fake_structure(inputs))


def _fake_responder(task, inputs):
    if task == "card_body":
        return _fake_body(inputs)
    return _fake_structure(inputs)


def _make_provider(kind):
    if kind == "fake":
        return FakeTextProvider(responder=_fake_responder)
    from providers import DeepSeekProvider
    cfg_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")
    if not os.path.exists(cfg_path):
        print("❌ 缺少 config.json，无法使用 deepseek provider", file=sys.stderr)
        raise SystemExit(2)
    with open(cfg_path, encoding="utf-8") as fh:
        key = json.load(fh).get("deepseek_api_key")
    if not key or key.startswith("sk-你的"):
        print("❌ config.json 里的 deepseek_api_key 未填写", file=sys.stderr)
        raise SystemExit(2)
    return DeepSeekProvider(api_key=key)


def _write(path, text):
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


if __name__ == "__main__":
    raise SystemExit(main())
