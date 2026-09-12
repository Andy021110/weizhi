#!/usr/bin/env python3
"""Create or upgrade the reviewable v2 Harness learning pack.

This local UI fixture never calls a model or the network. Re-running it upgrades
the three cards through CardVersion so existing learning state is preserved.
"""
import hashlib
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import db
from weizhi_v2 import storage


TITLE = "从一次 LLM 调用到 Agent Harness"
VERSION = "v2-demo-rich-images-20260909"
SOURCE_URL = "https://www.bestblogs.dev/article/c7b1be8460"


CLAIMS = [
    {"id": "harness_loop", "claim_text": "Agent 由模型、动作、环境与观察构成闭环", "evidence_text": "Agent 真正的分水岭不是会思考，而是建立了模型、动作、环境、观察、模型的闭环。"},
    {"id": "harness_runtime", "claim_text": "Harness 管理上下文、权限、生命周期和持久化", "evidence_text": "Harness 决定这一步在什么上下文、权限、生命周期与持久化规则下发生。"},
    {"id": "harness_tradeoff", "claim_text": "不同 Harness 交换成本、控制、恢复与长期学习", "evidence_text": "不同路线交换的是成本、控制力、可恢复性和长期学习能力。"},
]


def card_definitions():
    return [
        {
            "concept_id": "evolution",
            "learning_objective": "从模型边界推导 Context、Loop、Tools 和 Memory",
            "minimum_recall": "能沿着模型边界说明四个部件为什么出现",
            "estimated_minutes": 8,
            "claim_ids": ["harness_loop"],
            "content": {
                "title": "从一次调用到可行动的 Agent",
                "lead": "不要先背 Agent 的组件名。先看一次 LLM 调用做不到什么，每一层工程能力就会自然出现。",
                "sections": [
                    {"heading": "1. 起点：模型只做一次转换", "paragraphs": ["最小的 LLM 应用可以写成 answer = LLM(question)。模型只接收本次输入并生成输出；它不会自然记得上次会话，也不知道自己输出的命令是否真的被执行。", "因此，“能不能长时间工作”并不只由模型能力决定，还取决于外部系统怎样反复准备输入、执行动作并把结果送回模型。"]},
                    {"heading": "2. Context：让每次调用看到正确的东西", "paragraphs": ["多轮对话并不是模型在内部保存了会话。应用需要在每次调用前重新装配系统指令、近期历史、相关资料和当前问题。这份临时装配的内容就是工作上下文。", "上下文工程的关键不是塞入越多越好，而是让当前步骤只看到必要且可信的信息。"]},
                    {"heading": "3. Loop 与 Tools：从回答变成行动闭环", "paragraphs": ["问答机器人通常生成一次就结束，Agent 则要根据行动结果继续决策。外部程序解析动作、调用工具、记录 observation，再把新状态送给模型。", "结构化 Tool Calling 又将动作拆成 Schema、Router 和 Result。这避免了依赖自由文本和正则解析所带来的参数漂移、缺失和误执行。"]},
                    {"heading": "4. Memory：工作上下文不等于长期记忆", "paragraphs": ["历史变长后，系统必须区分当前调用能看到的工作记忆，以及保存在模型外、按需检索的长期记忆。把所有历史都塞回上下文，只会增加成本并稀释关键信息。", "长期记忆的难点不是保存，而是写入门槛、冲突处理、检索预算和错误纠正。"]},
                ],
                "key_points": ["模型只生成内容，外部系统负责状态与执行。", "Agent 的分水岭是动作与观察闭环。", "上下文是当前工作集，长期记忆是可检索的外部存储。"],
                "illustrations": [
                    {"src": "/assets/v2/01-evolution-ladder.svg", "after_section": 1, "alt": "LLM 逐步增加 Context、Tools、Loop 和 Memory 的能力阶梯", "caption": "图 1：每个组件都来自前一层的能力边界。", "takeaway": "判断是否需要组件时，先问当前系统做不到什么。"},
                    {"src": "/assets/v2/02-action-loop.svg", "after_section": 2, "alt": "模型、动作、环境和观察构成的 Agent 闭环", "caption": "图 2：模型不直接改变环境，外部程序执行动作并返回观察。", "takeaway": "只有当 observation 回到上下文，系统才能基于真实结果继续决策。"},
                ],
                "evidence_quotes": [{"claim": CLAIMS[0]["claim_text"], "quote": CLAIMS[0]["evidence_text"], "source": "《从一次 LLM 调用到完整 Harness》", "source_url": SOURCE_URL}],
                "factual_sentences": [{"text": CLAIMS[0]["claim_text"], "claim_ids": ["harness_loop"]}],
                "quiz": [{"question": "Bot 与 Agent 的系统分水岭是什么？", "options": ["模型大小", "动作与观察闭环", "回答长度"], "answer": 1, "objective_id": "evolution", "explanation": "模型大小可以影响能力，但 Agent 需要外部程序执行动作、返回观察并继续决策。"}],
            },
        },
        {
            "concept_id": "runtime",
            "learning_objective": "把长任务故障映射到 Harness 的状态、安全和生命周期能力",
            "minimum_recall": "能区分 Agent Loop 与 Harness 的职责",
            "estimated_minutes": 8,
            "claim_ids": ["harness_runtime"],
            "content": {
                "title": "Harness 管的不是下一步，而是运行条件",
                "lead": "Agent Loop 负责选择下一步动作；Harness 负责让这一步在可恢复、可观察、可控的环境中发生。",
                "sections": [
                    {"heading": "1. 为什么 Loop 还不够", "paragraphs": ["短任务里，一个“模型—工具—观察”循环已经能工作。任务变长后，崩溃恢复、上下文压缩、事件记录、多客户端同步和子任务隔离都会成为独立问题。", "Harness 将这些问题从提示词中抽离，放到可测试的运行时中。"]},
                    {"heading": "2. 任务身份、事件和恢复", "paragraphs": ["长任务需要稳定身份，否则进程退出后就无法知道应该从哪里继续。把模型消息、工具调用、文件变更和批准结果记录成事件，便可以重建任务状态、审计过程并向多个界面投影。"]},
                    {"heading": "3. Approval 与 Sandbox 是两道不同的门", "paragraphs": ["用户批准回答“这个动作现在是否允许”，技术沙箱则限制“即使允许，它实际能接触哪些文件、网络和系统能力”。", "只有批准没有沙箱，误操作的影响范围仍然过大；只有沙箱没有批准，系统又无法表达用户对具体高风险动作的意图。"]},
                    {"heading": "4. 多 Agent 不只是多调几次模型", "paragraphs": ["子 Agent 需要自己的上下文、权限、运行环境和生命周期。如果多个任务共用同一份不断膨胀的历史，隔离、恢复和责任界定都会失效。", "一个实用的判断方法是：如果子任务可能独立失败、需要不同资源，或必须单独审计，它就应该拥有独立运行记录。父 Agent 传递的应是明确目标和结果契约，而不是将全部历史复制给它。", "反过来，如果任务只需一次工具调用、失败影响很小且不需要单独恢复，继续放在主 Loop 里更简单。拆分子 Agent 应该减少耦合，而不是只为了增加并行数量。"]},
                ],
                "key_points": ["Loop 决定动作，Harness 管理动作的运行条件。", "事件记录使长任务可恢复、可审计。", "Approval 表达用户意图，Sandbox 限制技术边界。"],
                "illustrations": [
                    {"src": "/assets/v2/03-harness-layers.svg", "after_section": 0, "alt": "Agent Loop 位于 Harness 内，外层管理上下文、权限、持久化和事件", "caption": "图 1：Loop 解决下一步，Harness 解决整个任务如何可控地运行。", "takeaway": "崩溃恢复、权限和观测不应该只依赖提示词。"},
                    {"src": "/assets/v2/04-approval-sandbox.svg", "after_section": 2, "alt": "动作先通过用户批准，再受技术沙箱限制", "caption": "图 2：Approval 和 Sandbox 是先后生效的两道不同门禁。", "takeaway": "用户同意一次动作，不等于该动作获得无限的系统权限。"},
                ],
                "evidence_quotes": [{"claim": CLAIMS[1]["claim_text"], "quote": CLAIMS[1]["evidence_text"], "source": "《从一次 LLM 调用到完整 Harness》", "source_url": SOURCE_URL}],
                "factual_sentences": [{"text": CLAIMS[1]["claim_text"], "claim_ids": ["harness_runtime"]}],
                "quiz": [{"question": "用户允许命令后，什么仍限制它能写到哪里？", "options": ["Approval", "Sandbox", "标题生成器"], "answer": 1, "objective_id": "runtime", "explanation": "Approval 判断是否允许这次动作；Sandbox 仍会在操作系统层面限制它可触及的资源。"}],
            },
        },
        {
            "concept_id": "decision",
            "learning_objective": "比较 Pi、OpenCode、Codex、Hermes 并为新场景选择设计",
            "minimum_recall": "能根据任务风险、长度和复用需求做选择",
            "estimated_minutes": 9,
            "claim_ids": ["harness_tradeoff"],
            "content": {
                "title": "选 Harness 时，先选你愿意承担的成本",
                "lead": "四种路线没有统一排名。它们分别把资源投向当前效率、状态恢复、安全执行和长期学习。",
                "sections": [
                    {"heading": "1. Pi：用最小工作集保持效率", "paragraphs": ["Pi 只开放少量核心工具，通过资源加载器装配规则、Skills 和提示模板，再用会话压缩控制上下文。它适合低风险、当前任务导向的场景，代价是内建安全与隔离能力较少。"]},
                    {"heading": "2. OpenCode：用结构化事件换可恢复性", "paragraphs": ["OpenCode 把 Reasoning、Text、Tool、Step、Patch 和 Compaction 等记录成事件，再向不同客户端投影状态。优点是可恢复、可审计和多端共享，代价是状态工程和压缩策略更复杂。"]},
                    {"heading": "3. Codex：用运行时成本换安全与长任务管理", "paragraphs": ["Codex 用 Thread、Turn、Item 表达任务、用户推动的一轮过程和其中的消息、工具与变更。批准与沙箱分层处理，子 Agent 沿用独立任务抽象。它适合长时间、有真实副作用的工程工作。"]},
                    {"heading": "4. Hermes：用记忆治理换跨任务复用", "paragraphs": ["Hermes 将当前任务、会话归档、稳定 Memory 和可复用 Skills 连成闭环。它的价值在于让过去经验改变下一次行为，难点是避免错误记忆、冲突和无限膨胀。"]},
                    {"heading": "5. 把选型变成四个问题", "paragraphs": ["任务是否跨多天？动作是否具有高风险副作用？是否需要在多个客户端恢复同一状态？经验是否值得跨会话复用？", "前三个答案越多，越需要强运行时、事件和安全边界；最后一个答案为是，则需要投资记忆与 Skill 的治理。"]},
                ],
                "comparison": [
                    {"name": "Pi", "focus": "最小 Harness", "gain": "低成本、路径短", "cost": "需外部补安全隔离"},
                    {"name": "OpenCode", "focus": "Profile 与事件", "gain": "恢复、审计、多端", "cost": "状态工程更复杂"},
                    {"name": "Codex", "focus": "安全与生命周期", "gain": "可控、长任务、多 Agent", "cost": "运行时和 token 更高"},
                    {"name": "Hermes", "focus": "记忆与自我改进", "gain": "跨任务经验复用", "cost": "记忆冲突与治理"},
                ],
                "key_points": ["架构选择是取舍，不是排名。", "短、低风险任务优先减小工作集。", "长、高风险、需恢复的任务值得承担更高运行时成本。"],
                "illustrations": [
                    {"src": "/assets/v2/05-tradeoff-map.svg", "after_section": 3, "alt": "Pi、OpenCode、Codex 和 Hermes 在任务长度与风险维度上的取舍地图", "caption": "图 1：四种路线投资的能力不同，图中位置不代表排名。", "takeaway": "先确定任务长度、风险和恢复需求，再选运行时能力。"},
                    {"src": "/assets/v2/06-decision-tree.svg", "after_section": 4, "alt": "根据多日任务、高风险、多端状态和长期复用决定 Harness 规模", "caption": "图 2：用四个问题把抽象选型转成可执行判断。", "takeaway": "只有现实约束需要时才增加组件，避免提前支付复杂度。"},
                ],
                "evidence_quotes": [{"claim": CLAIMS[2]["claim_text"], "quote": CLAIMS[2]["evidence_text"], "source": "《从一次 LLM 调用到完整 Harness》", "source_url": SOURCE_URL}],
                "factual_sentences": [{"text": CLAIMS[2]["claim_text"], "claim_ids": ["harness_tradeoff"]}],
                "quiz": [{"question": "重复、高风险的长期任务最需要哪组能力？", "options": ["只缩短提示词", "恢复、安全边界与长期复用", "增加装饰图片"], "answer": 1, "objective_id": "decision", "explanation": "长任务需要恢复，高风险动作需要安全边界，重复任务才值得建立长期复用机制。"}],
            },
        },
    ]


def upgrade_existing(pack):
    if all((card.get("version") or {}).get("generator_version") == VERSION for card in pack["cards"]):
        print("演示学习包已是最新版：%s" % pack["id"])
        return 0
    by_concept = {card["concept_id"]: card for card in pack["cards"]}
    conn = storage._conn()
    try:
        conn.execute(
            "UPDATE v2_sources SET canonical_uri=? WHERE id IN (SELECT source_id FROM v2_pack_sources WHERE pack_id=?)",
            (SOURCE_URL, pack["id"]),
        )
        conn.commit()
    finally:
        conn.close()
    for definition in card_definitions():
        card = by_concept[definition["concept_id"]]
        result = storage.create_card_version(card["id"], {
            "learning_objective": definition["learning_objective"],
            "estimated_minutes": definition["estimated_minutes"],
            "claim_ids": definition["claim_ids"],
            "content": definition["content"],
            "generator_version": VERSION,
            "publish": True,
        })
        if result.get("error"):
            print(result)
            return 1
    print("已升级演示学习包内容：%s" % pack["id"])
    return 0


def main():
    db.init_db()
    existing = next((pack for pack in storage.list_packs() if pack["title"] == TITLE), None)
    if existing:
        return upgrade_existing(existing)

    goal = storage.create_goal({
        "outcome": "理解 Agent Harness，并能判断不同架构的取舍",
        "current_level": "知道 LLM、Prompt 和 Tool Calling，不熟悉运行时",
        "use_context": "Agent 产品与工程设计",
        "daily_minutes": 90,
        "success_evidence": ["能解释 Loop 与 Harness 的边界", "能为新场景选择运行时能力"],
        "milestones": ["建立 Agent 演进心智模型", "理解运行时的状态与安全", "完成架构迁移判断"],
    })["goal"]
    uri = SOURCE_URL
    source = storage.create_source({
        "source_type": "pdf", "canonical_uri": uri,
        "title": "从一次 LLM 调用到完整 Harness，Agent 到底经历了什么？",
        "snapshot_sha256": hashlib.sha256(uri.encode()).hexdigest(), "status": "ready",
    })["source"]
    claims = [dict(item, source_id=source["id"], verification_status="supported", verification={"method": "fixed_demo_fixture"}) for item in CLAIMS]
    cards = []
    for position, definition in enumerate(card_definitions(), 1):
        cards.append(dict(definition, position=position))
    created = storage.create_pack({
        "goal_id": goal["id"], "title": TITLE,
        "pack_objective": "理解 Agent 能力如何形成、Harness 管什么，以及如何做架构选择",
        "sources": [{"source_id": source["id"], "role": "user_material"}],
        "claims": claims, "cards": cards, "generation_version": VERSION,
    })
    if created.get("error"):
        print(created)
        return 1
    published = storage.publish_pack(created["pack"]["id"])
    print("已创建演示学习包：%s" % published["pack"]["id"])
    print("打开 http://127.0.0.1:8000/v2")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
