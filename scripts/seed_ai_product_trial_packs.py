#!/usr/bin/env python3
"""Seed two personalized AI-product trial packs with evidence and diagrams."""
import hashlib
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import db
from weizhi_v2 import storage
from weizhi_v2.content_quality import apply_depth


VERSION = "ai-product-trial-depth2-20260909"
GOAL_OUTCOME = "8 周完成一个经过真实用户与评测验证的 AI 产品，并建立持续技术判断方法"
SOURCES = [
    ("https://pair.withgoogle.com/guidebook-v2/chapters", "Google PAIR Guidebook：以人为本的 AI 产品设计"),
    ("https://developers.openai.com/api/docs/guides/evals", "OpenAI：Working with evals"),
    ("https://www.anthropic.com/engineering/building-effective-agents", "Anthropic：Building effective agents"),
]


def figure(name, caption, takeaway, after):
    return {"src": "/assets/ai-product/" + name, "after_section": after, "alt": caption, "caption": caption, "takeaway": takeaway}


def quiz(concept, question, options, answer, explanation):
    return [{"question": question, "options": options, "answer": answer, "objective_id": concept, "explanation": explanation}]


def card(concept, objective, recall, minutes, title, lead, sections, figures, key_points, question):
    return apply_depth({
        "concept_id": concept, "learning_objective": objective, "minimum_recall": recall,
        "estimated_minutes": minutes, "claim_ids": [],
        "content": {"title": title, "lead": lead, "sections": sections, "illustrations": figures,
                    "key_points": key_points, "factual_sentences": [], "quiz": question},
    })


def card_definitions():
    cards = [
        card(
            "problem_evidence", "把一个模糊 AI 创意改写成可验证的用户任务",
            "能写出用户、任务、现有方案、代价和停止条件", 8,
            "先证明问题存在，再证明 AI 有用",
            "AI 产品最常见的起点错误，是从模型能力反推一个看似合理的使用场景。你的第一项本领，是把创意变成可以被现实推翻的问题假设。",
            [
                {"heading": "1. 从一句创意变成一次具体任务", "paragraphs": ["不要写“帮助用户高效学习 AI”。改写为：一名已有计算机基础、但大模型知识零散的学习者，要在十分钟碎片时间内理解一个命题，并在数天后独立回忆或迁移。", "任务描述必须出现谁、在什么情境、完成什么动作，以及什么结果才算完成。"]},
                {"heading": "2. 记录用户今天怎样解决", "paragraphs": ["AI 的竞争对象往往不是另一个 AI 产品，而是搜索、收藏、问同事、复制到笔记或干脆放弃。先观察当前路径的时间、错误、犹豫和返工，才能得到产品基线。", "对微知而言，基线可以是读完整文章、阅读普通摘要，或使用现有高度概括卡片。"]},
                {"heading": "3. 提前写下停止条件", "paragraphs": ["产品假设必须允许失败。例如：如果目标用户无法从三张卡中说出任何愿意长期记住的命题，继续提高生成速度没有意义。", "停止条件会迫使团队区分真实价值和演示效果，也防止在已经失效的方向上不断补功能。"]},
            ],
            [figure("01-problem-chain.svg", "从用户任务到产品假设的证据链", "没有当前做法和可观察代价，就无法判断产品是否改善了什么。", 0), figure("02-problem-filter.svg", "首个 AI 项目的三项筛选条件", "先选能接触用户、任务重复且成败可判断的问题。", 1)],
            ["用户问题必须可以在现实中观察。", "现有解决方式就是产品基线。", "停止条件让产品假设可以被证伪。"],
            quiz("problem_evidence", "下面哪个更接近可验证的 AI 产品任务？", ["做一个更聪明的学习助手", "让目标用户在十分钟内完成一个命题的理解与回忆，并与普通摘要比较", "接入最新的大模型"], 1, "第二项同时定义了用户行为、时间边界和可比较基线。"),
        ),
        card(
            "golden_set", "为微知卡片建立能重复运行的最小黄金测试集",
            "能区分典型、边界与高风险样例，并为结果写评分规则", 9,
            "先造一把尺，再决定哪次生成更好",
            "没有固定测试集时，每次看到的“效果更好”都可能只是换了样例。黄金测试集把主观感觉变成可重复比较。",
            [
                {"heading": "1. 样例来自真实分布", "paragraphs": ["先收集用户确实会学的材料与目标：概念解释、架构取舍、数学推导、源码追踪和产品判断。典型样例占多数，同时保留超长文章、证据冲突、信息不足和高风险陈述。", "微知的第一版可以固定三十条输入；数量不追求大，追求每条都知道为什么存在。"]},
                {"heading": "2. 评分必须指向学习结果", "paragraphs": ["不要只评语言流畅。至少检查：卡片是否围绕单一命题、解释是否足够、证据能否跳转、图是否降低理解成本、题目是否真的覆盖目标。", "严重错误应单独计算，例如伪造引用、数字无依据、答案从题干泄露。它们不能被平均分掩盖。"]},
                {"heading": "3. 失败先分类，再修改系统", "paragraphs": ["低分可能来自选题价值低、正文解释不足、来源抓取错误、图文不一致或题目设计失败。不同失败需要改不同模块。", "每次修改只写一个机制假设，并在同一批样例上与旧版本成对比较。"]},
            ],
            [figure("03-golden-set.svg", "黄金测试集需要覆盖正常、边界与高风险输入", "只用顺利样例会高估真实质量。", 0), figure("04-eval-loop.svg", "固定样例驱动的版本迭代循环", "先定位失败层，再改对应模块。", 2)],
            ["固定样例让版本结果可比。", "严重错误不能被平均分抵消。", "一次实验只验证一个主要机制假设。"],
            quiz("golden_set", "新版本平均分提高，但出现一条伪造信源，应该怎样判断？", ["平均分提高即可发布", "把伪造信源作为严重错误单独拦截", "再增加几道简单题拉高平均分"], 1, "高风险失败需要独立门禁，不能被其他维度的平均得分抵消。"),
        ),
        card(
            "complexity_evidence", "用失败证据决定是否增加 RAG、工具、工作流或 Agent",
            "能为每个新增组件指出它要消除的失败和验证方法", 9,
            "架构不是功能清单，而是一组待证明的因果假设",
            "复杂架构容易带来“做了很多”的感觉。真正的产品判断是：哪一种失败必须靠这个组件解决，净收益是否大于新增成本。",
            [
                {"heading": "1. 从单次调用建立基线", "paragraphs": ["先用最少提示词和必要上下文完成端到端任务。它可能不够好，但能暴露真正瓶颈，并提供质量、延迟和成本基线。", "如果简单方案已经越过用户成功阈值，额外编排不会自动产生产品价值。"]},
                {"heading": "2. 一个组件对应一种失败", "paragraphs": ["知识没有进入上下文时考虑检索；必须读取或改变外部状态时增加工具；步骤稳定并需要一致执行时写成工作流；只有路径难以预先确定时才让 Agent 自主选择。", "“大家都在做 Agent”不是架构证据。失败样例与任务结构才是。"]},
                {"heading": "3. 比较净收益", "paragraphs": ["加入组件后，在固定测试集上比较任务成功率和严重错误，同时记录延迟、成本、调试难度和新的失败传播路径。", "只有当收益稳定跨过预先设定阈值，组件才应该留下。否则回到更简单的系统。"]},
            ],
            [figure("05-architecture-ladder.svg", "从单次调用到自主 Agent 的复杂度阶梯", "上一个台阶前，先指出当前台阶解决不了的失败。", 0), figure("06-architecture-proof.svg", "组件从失败样例到净收益的验证路径", "架构决策必须能被成对实验推翻。", 2)],
            ["最简单端到端系统是必要基线。", "组件必须对应可观察失败。", "比较质量时要同时计算成本、延迟和风险。"],
            quiz("complexity_evidence", "任务步骤固定、每步都要检查格式，优先选择什么？", ["固定工作流和程序门禁", "完全自主 Agent", "长期记忆系统"], 0, "路径可以预先写定时，工作流通常更可预测，也更容易测试。"),
        ),
        card(
            "model_boundary", "从概率生成和外部状态区分模型问题与系统问题",
            "能对一次坏结果提出至少三个不同层级的可验证假设", 8,
            "模型会生成答案，但产品必须交付结果",
            "LLM 根据当前上下文生成后续内容。用户感受到的产品质量，还取决于资料、工具、验证、交互和运行环境。",
            [
                {"heading": "1. 模型看到的是临时上下文", "paragraphs": ["一次调用只基于本次提供的指令、材料和对话状态。正文缺失、检索选错段落或关键约束被截断，都可能表现为“模型不聪明”。", "调试时先保存模型实际看到的输入，而不是只检查最终页面。"]},
                {"heading": "2. 生成候选不等于验证事实", "paragraphs": ["语言模型擅长生成符合上下文的候选文本，但它不会自动证明引用存在、数字正确或外部操作成功。产品需要把证据定位、计算、工具结果和状态检查放在模型之外。", "这也是微知保留信源链接、主张映射和确定性门禁的原因。"]},
                {"heading": "3. 用假设树调试", "paragraphs": ["看到低质量卡片时，分别检查材料是否有价值、上下文是否完整、提示是否明确、模型是否能力不足、门禁是否漏检，以及界面是否隐藏了关键信息。", "每个假设都要对应一个观察或实验；否则“换更大模型”只是昂贵的猜测。"]},
            ],
            [figure("07-llm-boundary.svg", "从上下文、生成到外部验证的系统边界", "模型输出是候选；可追溯与正确执行由系统补足。", 0), figure("08-failure-tree.svg", "坏结果的四类常见来源", "先定位层级，避免把所有问题都归因于模型。", 2)],
            ["保存实际上下文才能调试模型行为。", "事实与外部状态需要系统验证。", "修复前先提出可证伪的失败假设。"],
            quiz("model_boundary", "卡片引用了原文中不存在的数字，第一步最该检查什么？", ["字体大小", "模型实际上下文、证据映射和数字门禁", "再增加一张配图"], 1, "这是证据链和验证问题，需要检查输入、映射和确定性校验。"),
        ),
        card(
            "system_selection", "根据知识稳定性、路径确定性和副作用选择 AI 系统形态",
            "面对新任务时能在提示词、RAG、工具、工作流和 Agent 中做有依据的选择", 9,
            "RAG、工具、Workflow、Agent 分别解决什么",
            "这些词不是产品成熟度等级。它们解决的是不同约束；选择错误会让系统更贵、更慢，也更难解释。",
            [
                {"heading": "1. 知识与动作是两类问题", "paragraphs": ["回答缺少稳定私有知识时，检索可以把相关证据送入上下文；任务需要查询数据库、运行代码或修改外部状态时，模型需要结构化工具接口。", "RAG 不能代替执行，工具也不能自动保证找到正确资料。"]},
                {"heading": "2. 工作流与 Agent 的分界", "paragraphs": ["如果步骤可以预先画出并且一致性重要，就由代码控制路径，让模型完成局部判断。若子任务和步骤数量无法提前确定，才考虑让 Agent 根据环境反馈规划。", "自主程度提高后，错误会沿多步传播，因此需要停止条件、权限、事件记录与人工检查点。"]},
                {"heading": "3. 用微知任务练习选择", "paragraphs": ["抓取、去重、正文抽取和数字校验适合确定性程序；从文章识别值得学习的命题可以由模型提出候选；证据门禁和发布条件再交给程序。", "这是一条混合工作流，不需要把整条链路都包装成自主 Agent。"]},
            ],
            [figure("09-system-choice.svg", "四类任务约束与系统形态的映射", "先判断任务约束，再选择组件名称。", 0), figure("10-risk-cost-map.svg", "架构复杂度带来的收益与新增代价", "质量提升必须覆盖延迟、成本和错误传播。", 1)],
            ["RAG 解决知识进入上下文。", "工具解决对外部世界的读取与行动。", "固定路径用工作流，开放路径才考虑 Agent。"],
            quiz("system_selection", "“从文章中抽取候选命题，再用程序检查证据后发布”更接近什么？", ["混合工作流", "纯 RAG", "完全自主 Agent"], 0, "模型负责语义判断，程序负责固定门禁，整体路径可以预先定义。"),
        ),
        card(
            "technology_radar", "把一条 AI 技术新闻转成针对自己产品的采用决策",
            "能写出原始事实、能力假设、最小实验和产品结论", 8,
            "不要追新闻，要管理会改变产品决策的证据",
            "新模型的榜单和演示只能说明它值得形成假设。只有在你的真实任务、成本和风险约束下通过实验，才构成采用依据。",
            [
                {"heading": "1. 回到原始发布", "paragraphs": ["先保存官方文档、技术报告、论文、代码或可复现实验中的具体变化，区分发布方声明与独立证据。二手摘要用于发现线索，不承担最终证据。", "只记录可能改变用户任务、系统约束或产品经济性的变化。"]},
                {"heading": "2. 把变化写成能力假设", "paragraphs": ["不要写“新模型推理更强”。改写为：它可能降低微知在数学卡片中的步骤遗漏率，同时延迟与成本仍在预算内。", "一个有效假设必须指出任务切片、要改善的失败类型和指标。"]},
                {"heading": "3. 用最小样例先淘汰", "paragraphs": ["从黄金测试集中抽取最相关的一小组样例，保持提示词、上下文和评分规则一致，进行成对比较。只有结果接近采用阈值，才运行完整测试集。", "最终记录采用、观察或忽略，并写清质量、严重错误、延迟、成本和迁移代价。"]},
            ],
            [figure("11-news-funnel.svg", "从原始发布到产品决策的技术雷达漏斗", "新闻要经过能力假设与最小实验，才能进入产品。", 0), figure("12-release-scorecard.svg", "新模型必须在自己的任务上比较四类指标", "榜单不能替代任务质量、错误、延迟和成本。", 2)],
            ["二手资讯负责发现，原始资料承担证据。", "能力声明要改写成产品假设。", "先小样本淘汰，再运行完整黄金集。"],
            quiz("technology_radar", "看到一个新模型榜单领先，下一步最合理的动作是什么？", ["立即替换线上模型", "从自己的黄金集中抽取相关样例做同条件比较", "只阅读更多媒体报道"], 1, "产品采用要依据自己的任务分布、失败代价、延迟和成本。"),
        ),
    ]
    evidence = [
        ("AI 产品设计应从用户需求、成功定义与反馈控制开始。", "PAIR Guidebook 将用户需求、成功标准、心理模型、解释信任和反馈控制作为 AI 产品设计的核心问题。", SOURCES[0]),
        ("评测需要固定样例、明确测试标准和可重复的运行过程。", "OpenAI Evals 提供由数据、测试条件和 grader 组成的可重复评测流程。", SOURCES[1]),
        ("工作流适合预定义路径，Agent 适合由模型动态决定步骤的开放任务。", "Anthropic 区分预定义代码路径的 workflow 与由模型动态控制过程的 agent，并建议从最简单方案开始。", SOURCES[2]),
        ("AI 产品需要把模型能力放进完整系统中评估。", "Anthropic 将检索、工具和记忆视为增强模型的系统能力，并强调根据具体任务进行组合。", SOURCES[2]),
        ("增加 Agent 自主性会带来额外延迟、成本和错误传播风险。", "Anthropic 建议固定任务优先使用可预测的 workflow，开放任务才采用 agent，并对自主系统进行充分测试。", SOURCES[2]),
        ("新模型是否值得采用，应在产品自己的任务分布上重复评测。", "OpenAI Evals 支持使用固定测试数据与评分器比较模型或配置变化。", SOURCES[1]),
    ]
    for item, (claim, summary, source) in zip(cards, evidence):
        item["content"]["evidence_quotes"] = [{"claim": claim, "quote": summary, "source": source[1], "source_url": source[0]}]
    return cards


def get_or_create_source(url, title):
    existing = next((item for item in storage.list_sources() if item["canonical_uri"] == url), None)
    if existing:
        return existing
    return storage.create_source({"source_type": "article", "canonical_uri": url, "title": title,
                                  "snapshot_sha256": hashlib.sha256(url.encode()).hexdigest(), "status": "ready"})["source"]


def main():
    db.init_db()
    goal = next(g for g in storage.list_goals() if g["outcome"] == GOAL_OUTCOME)
    sources = [get_or_create_source(*item) for item in SOURCES]
    source_refs = [{"source_id": item["id"], "role": "primary"} for item in sources]
    definitions = card_definitions()
    packs = [
        ("路径 1/7｜AI 产品实战：问题、评测与架构证据", "从真实问题出发，建立评测尺并用失败证据控制架构复杂度", definitions[:3]),
        ("AI 产品试学 02｜模型边界、系统选择与技术雷达", "区分模型和系统问题，并把技术变化转成可验证的产品决策", definitions[3:]),
    ]
    created_ids = []
    existing_by_title = {item["title"]: item for item in storage.list_packs()}
    for title, objective, items in packs:
        if title in existing_by_title:
            existing = existing_by_title[title]
            if all((item.get("version") or {}).get("generator_version") == VERSION for item in existing["cards"]):
                continue
            for current, definition in zip(existing["cards"], items):
                result = storage.create_card_version(current["id"], {
                    "learning_objective": definition["learning_objective"],
                    "estimated_minutes": definition["estimated_minutes"],
                    "claim_ids": [], "content": definition["content"],
                    "generator_version": VERSION, "publish": True,
                })
                if result.get("error"):
                    raise ValueError(result)
            continue
        cards = [dict(item, position=index) for index, item in enumerate(items, 1)]
        result = storage.create_pack({"goal_id": goal["id"], "title": title, "pack_objective": objective,
                                      "sources": source_refs, "claims": [], "cards": cards,
                                      "provenance": "user_authored", "generation_version": VERSION})
        if result.get("error"):
            raise ValueError(result)
        published = storage.publish_pack(result["pack"]["id"])
        if published.get("error"):
            raise ValueError(published)
        created_ids.append(result["pack"]["id"])
    print("已准备 2 个学习包、6 张卡；本次新增学习包：%d" % len(created_ids))
    for pack_id in created_ids:
        print("http://127.0.0.1:8000/v2?pack=" + pack_id)


if __name__ == "__main__":
    main()
