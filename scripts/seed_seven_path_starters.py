#!/usr/bin/env python3
"""Create one visible starter task for each of the seven personalized paths."""
import hashlib
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import db
from weizhi_v2 import storage
from scripts.seed_ai_product_trial_packs import card, figure, quiz


VERSION = "seven-path-depth2-20260909"


STARTERS = [
    {
        "goal_key": "大模型完整基础", "title": "路径 2/7｜大模型基础启动：一次调用到底发生了什么", "minutes": 8,
        "concept": "llm_call", "objective": "从 token、logits 和运行时增强解释一次 LLM 产品调用", "recall": "能画出文本到 token 概率，并指出模型外的系统职责",
        "lead": "你已有计算机基础，所以不从名词表开始。第一步是追踪一个请求的数据流，并把模型能力与产品系统能力分开。",
        "sections": [
            ("1. 文本先变成 token", "Tokenizer 把字符串映射成离散编号；模型处理的是这些编号对应的向量，而不是直接读取汉字含义。先记录输入长度和截断位置，它们决定模型实际看到什么。"),
            ("2. 模型输出下一个 token 的分布", "最后一层产生词表维度的 logits，经过温度和采样规则变成下一个 token。重复这个过程才形成完整回答，因此同一输入可能出现不同输出。"),
            ("3. 产品能力还来自模型之外", "检索负责补充资料，工具负责读写外部状态，门禁负责检查证据与风险。把这些全部归为模型能力，会让调试和选型失去方向。"),
        ], "figures": [("13-token-flow.svg", "一次生成的数据流", "文本输出来自逐 token 概率生成。"), ("14-model-lifecycle.svg", "训练、上下文和工具的职责", "不同能力缺口需要不同修复方式。")],
        "points": ["模型处理 token 表示。", "logits 经采样形成下一个 token。", "检索、工具和验证属于产品系统。"],
        "question": ("模型回答缺少刚发布的公司政策，优先验证什么？", ["是否需要把可信政策检索进上下文", "是否把字体加粗", "是否立即做 LoRA"], 0, "时效知识缺失应先检查上下文和检索，而不是直接训练模型。"),
        "source": ("https://huggingface.co/docs/transformers/main/en/llm_tutorial", "Hugging Face：Text generation"),
    },
    {
        "goal_key": "Transformer", "title": "路径 3/7｜Transformer 数学启动：沿张量理解 Attention", "minutes": 9,
        "concept": "attention_tensor", "objective": "把 scaled dot-product Attention 公式对应到张量形状和信息路由", "recall": "能写出 Q、K、V 与注意力矩阵的形状并解释每一步",
        "lead": "数学学习先服务于代码判断：每个符号都必须回答形状是什么、数值操作是什么、它改变了哪条信息流。",
        "sections": [
            ("1. 从输入矩阵开始", "把长度为 n 的序列表示成 X∈Rⁿˣᵈ。三个线性投影得到 Q、K、V；先标注 batch、head、序列和特征维，很多实现错误会立刻暴露。"),
            ("2. QKᵀ 产生位置之间的关系", "每个 query 与所有 key 做点积，得到 n×n 分数矩阵。除以维度平方根控制数值尺度，Softmax 将每行转成权重分布。"),
            ("3. 权重乘 V 完成信息路由", "注意力权重对 value 加权求和，使每个位置获得与当前查询相关的信息。公式理解最终要落到一次手算与一段可运行代码。"),
        ], "figures": [("15-attention-shapes.svg", "Attention 的关键张量形状", "先保证维度闭合，再解释语义。"), ("16-attention-meaning.svg", "从数值操作到信息路由", "点积选择相关位置，加权和搬运信息。")],
        "points": ["QKᵀ 的两个序列维形成位置关系矩阵。", "缩放帮助控制 Softmax 前的数值尺度。", "权重与 V 的乘积完成信息聚合。"],
        "question": ("单头 Attention 中 Q、K 形状都是 n×d，QKᵀ 的形状是什么？", ["d×d", "n×n", "n×d"], 1, "Q 的序列维与 K 的序列维两两比较，因此得到 n×n。"),
        "source": ("https://arxiv.org/abs/1706.03762", "论文：Attention Is All You Need"),
    },
    {
        "goal_key": "独立构建一个有基准集", "title": "路径 4/7｜RAG 启动：先分清检索失败与生成失败", "minutes": 9,
        "concept": "rag_diagnosis", "objective": "用证据是否进入上下文来分离 RAG 的检索与生成失败", "recall": "能沿解析、检索、组装和生成四层定位一次错误",
        "lead": "RAG 的第一项本领不是选择向量数据库，而是判断正确证据在哪一层消失。",
        "sections": [
            ("1. 把链路拆成四段", "原文先被解析与切分，查询再召回并排序片段，系统随后组装上下文，最后模型依据上下文生成答案和引用。每段都要留下可检查的中间结果。"),
            ("2. 先测目标证据是否被召回", "如果正确片段没有进入前 K 个结果，改提示词通常无效。先固定问题与目标证据，比较关键词、向量、混合检索与 rerank 的 Recall@K。"),
            ("3. 证据存在后再测回答", "当正确片段已进入上下文，才检查模型是否遗漏、曲解或无依据扩写。这样可以把检索指标与答案指标分开，避免一次总分掩盖根因。"),
        ], "figures": [("17-rag-pipeline.svg", "RAG 的四段主链路", "每段保存中间结果才能定位错误。"), ("18-rag-diagnosis.svg", "先检索、后生成的诊断顺序", "正确证据未出现时，不要先改生成提示词。")],
        "points": ["解析、检索、上下文和生成要分别观察。", "Recall@K 回答证据是否被找到。", "证据进入上下文后再评答案忠实度。"],
        "question": ("答案错误且目标证据未进入前 K 个片段，首先应该改哪层？", ["检索与排序", "答案字体", "复习算法"], 0, "生成模型看不到目标证据时，首要瓶颈位于检索链路。"),
        "source": ("https://arxiv.org/abs/2005.11401", "论文：Retrieval-Augmented Generation"),
    },
    {
        "goal_key": "Agent 与 Harness", "title": "路径 5/7｜Agent 启动：建立动作—观察闭环", "minutes": 8,
        "concept": "agent_loop", "objective": "区分模型决策、工具执行、环境观察与 Harness 约束", "recall": "能画出 Agent Loop，并指出权限、恢复和停止条件的位置",
        "lead": "Agent 不是一次更长的回答。只有动作真实执行、结果返回上下文并影响下一步，系统才形成闭环。",
        "sections": [
            ("1. 模型提出结构化动作", "模型根据当前上下文选择工具和参数；外部程序解析并验证结构。自由文本中的“我已经执行”不能被当作真实状态变化。"),
            ("2. 环境执行并返回观察", "工具在权限边界内执行，结果、错误和状态变化作为 observation 返回。模型基于真实观察决定继续、改计划或停止。"),
            ("3. Harness 管整个任务", "长任务还需要事件记录、检查点、幂等、副作用批准和最大步数。Loop 决定下一步，Harness 保证任务可恢复、可审计、可控制。"),
        ], "figures": [("19-agent-loop.svg", "模型、动作、环境与观察闭环", "观察必须回到上下文才能影响下一步。"), ("20-agent-runtime.svg", "Harness 的四类运行约束", "自主性越高，外部控制越重要。")],
        "points": ["模型提出动作，环境产生真实结果。", "Observation 关闭行动反馈环。", "Harness 管权限、恢复、记录和停止。"],
        "question": ("模型输出“文件已保存”，什么才构成成功证据？", ["措辞很确定", "工具结果与文件状态检查", "模型再次重复结论"], 1, "外部状态必须由工具与环境观察确认。"),
        "source": ("https://www.anthropic.com/engineering/building-effective-agents", "Anthropic：Building effective agents"),
    },
    {
        "goal_key": "完成一次小模型 LoRA", "title": "路径 6/7｜LoRA 启动：先判断是否真的需要微调", "minutes": 8,
        "concept": "lora_decision", "objective": "根据知识与行为缺口判断 Prompt、RAG 或 LoRA 的适用边界", "recall": "能说明 LoRA 改变什么，以及一个微调实验必须保留哪些基线",
        "lead": "微调不是“让模型更懂业务”的通用按钮。第一步是证明失败来自稳定行为模式，而不是资料缺失或提示不清。",
        "sections": [
            ("1. 先排除提示和知识问题", "输出格式、少量示例和清晰约束能修复的问题先用提示词；需要更新事实或私有材料的问题先测 RAG。只有稳定、重复的行为缺口才值得进入微调实验。"),
            ("2. LoRA 训练低秩增量", "LoRA 冻结预训练权重，在选定层加入可训练的低秩矩阵乘积。它减少需要更新的参数，但数据规范、污染检查和盲测仍决定实验是否可信。"),
            ("3. 与便宜基线成对比较", "固定测试集上同时运行 prompt-only、RAG 和 LoRA 版本，比较目标行为、严重错误、延迟和部署成本。训练损失下降不能替代产品任务的盲测。"),
        ], "figures": [("21-lora-choice.svg", "进入 LoRA 前的方案排除顺序", "先证明提示词和 RAG 无法稳定解决目标行为。"), ("22-lora-mechanism.svg", "冻结主权重并训练低秩增量", "参数效率不等于评测可以省略。")],
        "points": ["知识更新优先评估 RAG。", "LoRA 学习低秩权重增量。", "微调必须与更便宜的基线比较。"],
        "question": ("模型总是不知道今天更新的公司制度，优先尝试什么？", ["LoRA", "可信资料检索与引用", "增加训练轮数"], 1, "经常变化的外部知识更适合检索，不适合反复写入权重。"),
        "source": ("https://arxiv.org/abs/2106.09685", "论文：LoRA"),
    },
    {
        "goal_key": "源码", "title": "路径 7/7｜源码阅读启动：从公开 API 追到 logits", "minutes": 8,
        "concept": "source_trace", "objective": "沿真实运行数据从生成 API 追踪到模型 forward 与 logits", "recall": "能用调用图、断点和张量形状证明自己追通一条路径",
        "lead": "源码阅读不从目录逐文件浏览。选一个可运行输入，从公开入口跟随对象、状态和张量，直到得到输出。",
        "sections": [
            ("1. 固定一个最小输入", "用短文本和固定模型配置调用 generate，保存版本、参数与输出。最小输入让断点、日志和张量形状保持可读。"),
            ("2. 沿调用链追状态", "从公开 API 进入配置解析、tokenizer、generation loop、model forward 和输出处理。每到一层只记录关键对象、输入输出形状和状态改变。"),
            ("3. 用修改与测试证明理解", "写一个最小复现验证核心机制，再做一项很小的修改，例如增加形状断言或事件记录，并运行回归测试。能预测修改影响比读完更多文件更有价值。"),
        ], "figures": [("23-code-trace.svg", "从公开 API 沿数据流阅读源码", "入口、核心路径和测试构成最短阅读路线。"), ("24-code-proof.svg", "源码理解的四项证据", "调用图只是开始，修改和回归测试才能验证理解。")],
        "points": ["用可运行输入驱动源码阅读。", "记录对象、状态和张量变化。", "最小修改加测试是理解证据。"],
        "question": ("阅读一个生成库源码时，最有效的起点是什么？", ["按文件名字母顺序阅读", "运行一个最小 generate 调用并沿调用栈追踪", "先背下所有类名"], 1, "真实调用路径能筛掉大量与当前学习目标无关的代码。"),
        "source": ("https://huggingface.co/docs/transformers/main/en/llm_tutorial", "Hugging Face：Text generation"),
    },
]


def extra_spec(goal_key, title, concept, objective, recall, lead, sections, figures, points, question, source):
    return {"goal_key": goal_key, "title": title, "minutes": 8, "concept": concept, "objective": objective,
            "recall": recall, "lead": lead, "sections": sections, "figures": figures, "points": points,
            "question": question, "source": source}


EXTRAS = [
    extra_spec("大模型完整基础", "能力缺口应该改权重、上下文还是工具", "capability_source",
        "根据失败类型选择提示词、RAG、微调或工具", "能把四种失败分别映射到对应机制",
        "AI 产品判断的核心不是记住方案名称，而是识别能力来自权重、上下文还是外部执行。",
        [("1. 权重与行为", "模型的通用模式来自训练权重；稳定且重复的行为缺口可能需要微调，但应保留提示词基线。"), ("2. 上下文与知识", "私有或变化知识应在调用时进入上下文，检索比反复训练更易更新和追溯。"), ("3. 工具与结果", "模型不能仅靠文字保证外部动作成功；查询、计算和写入需要工具结果返回。")],
        [("25-training-map.svg", "四类能力来源地图", "先定位能力来源，再选择实现机制。"), ("38-baseline-compare.svg", "Prompt、RAG、微调与工具对照", "方案之间是职责差异，不是等级关系。")],
        ["稳定行为与时效知识是两类缺口。", "外部状态必须由工具处理。", "所有升级都应保留简单基线。"],
        ("模型不知道今天的库存，同时需要完成下单，应组合什么？", ["RAG 或查询工具加下单工具", "只提高温度", "只增加系统提示长度"], 0, "实时状态与外部动作都需要模型外能力。"),
        ("https://huggingface.co/docs/transformers/main/en/llm_tutorial", "Hugging Face：Text generation")),
    extra_spec("大模型完整基础", "用对照实验定位一次坏回答", "llm_failure_lab",
        "为坏回答建立可证伪的上下文、模型与系统假设", "能设计一次只改变一个变量的诊断实验",
        "直接换模型会同时改变多个变量。更可靠的做法是固定问题与评分规则，逐层替换。",
        [("1. 保存完整输入", "记录系统指令、材料、历史、模型参数和工具结果，复现失败是诊断前提。"), ("2. 每次替换一层", "先替换或补全上下文，再固定上下文比较模型，最后检查门禁与界面是否扭曲结果。"), ("3. 写出淘汰结论", "实验不只记录哪个版本更好，还要说明哪个假设被否定，以及下一次最值得测什么。")],
        [("26-failure-lab.svg", "坏回答的单变量实验", "固定其他条件，才能归因变化。"), ("08-failure-tree.svg", "从内容到交互的失败假设树", "一个表面错误可能来自不同系统层。")],
        ["失败必须先复现。", "单变量实验提供因果证据。", "被否定的假设同样是有效学习结果。"],
        ("比较两个模型时还同时换了提示词，会失去什么？", ["因果归因", "页面配色", "tokenizer 文件"], 0, "多个变量同时变化时无法判断提升来自哪里。"),
        ("https://developers.openai.com/api/docs/guides/evals", "OpenAI：Working with evals")),

    extra_spec("Transformer", "手算一个最小 Attention", "attention_manual",
        "完成两 token Attention 的分数、Softmax 与加权求和", "能独立写出并检查一组小矩阵计算",
        "手算的目标不是速度，而是让转置、缩放、逐行归一化和加权求和不再是黑箱。",
        [("1. 选择可手算矩阵", "使用两个 token、二维向量和简单投影，写清 Q、K、V 的每个元素与形状。"), ("2. 逐行得到权重", "计算 QKᵀ、缩放并逐行 Softmax；检查每行权重和是否为一。"), ("3. 与代码互证", "用 NumPy 写十几行前向过程，对比每个中间矩阵，而不只比较最终输出。")],
        [("28-attention-hand.svg", "最小 Attention 手算顺序", "中间矩阵全部可见，错误才可定位。"), ("15-attention-shapes.svg", "形状闭合检查", "数值计算前先验证每次矩阵乘法。")],
        ["Softmax 按 query 的每一行归一化。", "权重行和应接近一。", "程序要与手算比较中间值。"],
        ("为什么要比较中间矩阵而不只看最终输出？", ["可以定位错误发生在哪一步", "可以减少文件大小", "可以改变词表"], 0, "最终误差可能由转置、缩放或归一化任一步造成。"),
        ("https://arxiv.org/abs/1706.03762", "论文：Attention Is All You Need")),
    extra_spec("Transformer", "理解 Softmax 的温度与数值稳定", "softmax_stability",
        "解释缩放、减最大值和温度怎样改变概率分布", "能用一组 logits 预测温度变化方向并写稳定实现",
        "很多模型参数的直觉最终落在概率分布上。Softmax 是连接 logits、注意力权重和采样行为的关键操作。",
        [("1. 平移不改变分布", "Softmax 对所有 logits 同减常数保持结果不变，因此先减最大值可以降低指数溢出风险。"), ("2. 温度改变相对差异", "较低温度放大 logits 差异，使分布更集中；较高温度使候选更平均。"), ("3. 连接产品行为", "生成温度影响输出多样性，但无法修复缺失知识或错误证据；先判断问题层级。")],
        [("27-softmax-table.svg", "从 logits 到稳定概率", "减最大值改善数值计算但不改变数学结果。"), ("40-stability-spectrum.svg", "温度与数值范围", "概率参数改变分布形状，不补充外部知识。")],
        ["减最大值用于数值稳定。", "低温度通常使分布更集中。", "温度不能修复知识与证据缺口。"],
        ("提高温度最直接改变什么？", ["输出分布的集中程度", "训练数据内容", "检索索引"], 0, "温度作用于 logits 到概率的变换。"),
        ("https://arxiv.org/abs/1706.03762", "论文：Attention Is All You Need")),

    extra_spec("独立构建一个有基准集", "设计 RAG 切分与检索成对实验", "rag_chunk_experiment",
        "保持测试问题不变，比较切分、混合检索与 rerank", "能写出一个有控制变量的检索实验表",
        "切分策略好不好不能凭块长度判断，要看目标证据能否作为完整、可排序的单元被召回。",
        [("1. 标注目标证据", "为每个测试问题标记最小支持片段及来源位置，形成检索层的标准答案。"), ("2. 分别改变切分与排序", "先固定检索器比较切分，再固定切分比较关键词、向量、混合与 rerank。"), ("3. 检查收益代价", "记录 Recall@K 的同时观察索引规模、查询延迟和送入模型的冗余文本。")],
        [("41-chunk-map.svg", "四类切分策略对照", "块边界决定证据是否完整且可检索。"), ("30-rag-experiment.svg", "固定测试集的检索实验", "一次只改变切分或排序的一类因素。")],
        ["目标证据标注是检索评测基础。", "切分与排序要分开比较。", "召回提升也要计算延迟和上下文冗余。"],
        ("同时更换切分器与 reranker 后 Recall 上升，能否判断原因？", ["不能，需要拆成成对实验", "能，一定是 reranker", "能，一定是切分器"], 0, "两个变量同时改变会破坏归因。"),
        ("https://arxiv.org/abs/2005.11401", "论文：Retrieval-Augmented Generation")),
    extra_spec("独立构建一个有基准集", "让回答中的每个关键结论可追溯", "rag_evidence",
        "把答案主张映射到来源定位并识别无依据扩写", "能检查一条答案的主张、证据和缺口",
        "RAG 找到相关段落还不够。产品需要告诉用户哪条结论由哪段材料支持，以及哪里仍然缺证据。",
        [("1. 把回答拆成主张", "按可独立核验的事实或结论切分回答，避免整段文本只挂一个模糊引用。"), ("2. 保存来源定位", "每个主张记录文档、章节或段落定位，并允许用户直接跳回原文。"), ("3. 暴露证据缺口", "支持不足时标记未知或冲突，不让语言流畅掩盖证据缺失。")],
        [("42-evidence-panel.svg", "主张与证据的独立面板", "引用要支持具体结论并能跳回原文。"), ("29-rag-eval-grid.svg", "检索与回答双层评分", "找到证据和忠实使用证据需要分别检查。")],
        ["引用粒度应接近可核验主张。", "来源定位必须可跳转。", "未知与冲突应成为显式状态。"],
        ("段落有引用，但其中一个数字原文不存在，应怎样处理？", ["将数字标为无依据并阻止发布", "保留，因为段落有引用", "删掉来源链接"], 0, "引用存在不代表它支持段落里的每个主张。"),
        ("https://arxiv.org/abs/2005.11401", "论文：Retrieval-Augmented Generation")),

    extra_spec("Agent 与 Harness", "为 Agent 工具写清接口与权限", "tool_contract",
        "设计包含 Schema、错误结果和副作用边界的工具契约", "能发现一个模糊工具定义中的误执行风险",
        "工具接口是 Agent 与真实世界之间的产品界面。参数含糊时，模型能力越强也可能更快地产生副作用。",
        [("1. 名称表达唯一动作", "工具名称和描述应说明什么时候使用、什么时候不用，避免多个近义工具相互竞争。"), ("2. Schema 限制错误空间", "使用明确类型、枚举、必填字段和绝对标识；错误应结构化返回并允许模型恢复。"), ("3. 副作用单独批准", "读取与写入分开，高风险动作展示可审查预览，再由用户确认并受沙箱限制。")],
        [("43-tool-contract.svg", "工具契约的四个表面", "接口设计本身就是可靠性工程。"), ("31-permission-shell.svg", "从用户意图到执行的权限壳", "批准和技术权限共同限制副作用。")],
        ["工具描述要划清与相邻工具的边界。", "结构化错误支持恢复。", "高风险副作用需要预览与批准。"],
        ("一个工具同时支持读取和删除文件，主要问题是什么？", ["权限边界和副作用难以审查", "模型调用次数太少", "输出一定不够长"], 0, "将读写拆分可以缩小权限和误操作影响。"),
        ("https://www.anthropic.com/engineering/building-effective-agents", "Anthropic：Building effective agents")),
    extra_spec("Agent 与 Harness", "设计可恢复的十步 Agent 任务", "agent_recovery",
        "用事件、检查点和幂等设计避免中断后重复副作用", "能说明任务从检查点恢复时重放什么、跳过什么",
        "长任务的质量包含恢复能力。只在顺利路径上完成一次，无法证明系统能安全地工作。",
        [("1. 每一步写成事件", "记录模型决定、工具参数、结果、批准和状态改变，使运行过程可以重建和审计。"), ("2. 副作用建立幂等键", "发送、支付或写入等动作保存唯一标识，恢复时先查询是否已经完成。"), ("3. 故障注入验证恢复", "在不同步骤强制终止进程，检查是否从最近检查点继续、是否重复执行以及预算是否失控。")],
        [("32-recovery-timeline.svg", "中断后的检查点恢复", "恢复要继续未完成步骤并跳过已提交副作用。"), ("44-agent-checkpoints.svg", "风险上升时加入人工检查点", "人工判断应放在具体动作可审查之后。")],
        ["事件记录用于重建状态。", "幂等键阻止副作用重复。", "故障注入是恢复能力的真实测试。"],
        ("恢复后再次发送已经发出的邮件，缺少了什么？", ["幂等检查或已提交事件", "更高温度", "更多长期记忆"], 0, "恢复前必须确认副作用是否已完成。"),
        ("https://www.anthropic.com/engineering/building-effective-agents", "Anthropic：Building effective agents")),

    extra_spec("完成一次小模型 LoRA", "把业务目标变成可训练的数据规范", "lora_dataset",
        "定义正负样例、来源、去重与冻结盲测集", "能写出一份防止训练测试污染的数据契约",
        "LoRA 的参数效率无法补救目标含糊或数据污染。训练之前，先让每条样例说明它代表什么行为。",
        [("1. 写行为规范", "把期望与不可接受输出写成可判定规则，覆盖常见、边界和失败样例。"), ("2. 保存来源与变换", "每条数据记录来源、许可、清洗、去重和版本，避免无法解释模型学到了什么。"), ("3. 冻结盲测集", "按任务实体或时间切分，防止近重复样例同时进入训练和测试。")],
        [("45-dataset-pipeline.svg", "从原始样例到冻结盲测", "数据每次变换都必须可追踪。"), ("33-data-quality.svg", "训练数据的四个质量面", "代表性、一致性和污染比单纯数量更关键。")],
        ["训练数据首先是一份行为规范。", "去重应在切分测试集之前完成。", "盲测集要在实验前冻结。"],
        ("训练集和测试集含同一文章的轻微改写，主要风险是什么？", ["评测污染导致高估效果", "GPU 利用率下降", "输出字体变化"], 0, "近重复会让模型在测试中看到训练内容的变体。"),
        ("https://arxiv.org/abs/2106.09685", "论文：LoRA")),
    extra_spec("完成一次小模型 LoRA", "设计 LoRA 与 Prompt、RAG 的盲测", "lora_eval",
        "建立包含质量、严重错误和成本的微调决策表", "能根据盲测数据给出上线或停止结论",
        "训练结束不是实验结束。只有固定盲测集上的产品任务结果，才能说明权重更新是否值得部署。",
        [("1. 保留三个基线", "在同一模型和测试集上比较 prompt-only、RAG 与 LoRA，明确每个方案使用的数据与上下文。"), ("2. 分层报告结果", "同时看任务成功、严重错误、特定切片退化、推理延迟和存储运维成本。"), ("3. 预先写上线阈值", "实验前定义最低收益、不可接受回归和回滚条件，避免看到结果后移动标准。")],
        [("34-lora-scorecard.svg", "LoRA 上线计分卡", "训练 loss 只是过程信号，盲测任务才是产品证据。"), ("46-loss-vs-product.svg", "训练指标与产品指标分离", "Loss 下降不保证任务成功率提升。")],
        ["基线必须使用同一测试分布。", "切片退化可能被平均分掩盖。", "上线阈值应在实验前写下。"],
        ("验证 Loss 下降但盲测严重错误增加，应怎样处理？", ["阻止上线并分析失败切片", "只展示 Loss", "继续增加训练轮数"], 0, "产品风险指标优先于训练过程指标。"),
        ("https://arxiv.org/abs/2106.09685", "论文：LoRA")),

    extra_spec("源码", "在关键状态变化处设置断点", "source_breakpoints",
        "定位 tokenizer、生成循环、forward 和 logits processor 的状态边界", "能解释四个断点各自观察什么",
        "断点应该放在概念发生变化的位置。这样一次运行就能把文档名词与真实对象连接起来。",
        [("1. Tokenizer 边界", "观察字符串怎样变成 input_ids、attention_mask，以及截断和 padding 在哪里发生。"), ("2. Model forward 边界", "记录输入张量、KV Cache、隐藏状态和 logits 的形状，分清一次 forward 与完整生成循环。"), ("3. 输出处理边界", "检查 logits processor、停止条件和采样器怎样把模型分数转成最终 token。")],
        [("35-breakpoint-map.svg", "四个高价值源码断点", "在数据表示或控制权变化处观察。"), ("47-runtime-objects.svg", "生成过程中的关键对象关系", "追踪对象所有权可以解释状态保存位置。")],
        ["断点服务于一个具体问题。", "一次 forward 不等于完整生成。", "停止条件与采样位于模型输出之后。"],
        ("想观察 KV Cache 在哪里增长，最相关的边界是什么？", ["生成循环调用 model forward 前后", "README 标题", "安装脚本结尾"], 0, "Cache 会随生成步进入并返回模型 forward。"),
        ("https://huggingface.co/docs/transformers/main/en/llm_tutorial", "Hugging Face：Text generation")),
    extra_spec("源码", "用一个小改动证明源码理解", "source_change",
        "完成行为预测、小修改、定向测试和风险说明", "能提交一项有回归测试的可解释源码修改",
        "读懂的最低证据是能预测代码改变后的行为，并用测试区分改前与改后。",
        [("1. 先写行为预测", "明确哪条输入会触发改动、预期输出怎样变化，以及不应受影响的路径。"), ("2. 控制改动范围", "选择形状断言、事件字段或停止条件等小改动，避免同时重构多个层。"), ("3. 用测试和复盘收尾", "运行定向与回归测试，记录性能、兼容性和正确性风险，更新调用图。")],
        [("36-minimal-change.svg", "预测到回归的源码修改闭环", "测试要能区分改动前后的行为。"), ("48-reading-log.svg", "可复用的源码阅读记录", "把入口、状态、断点和验证压缩成工程证据。")],
        ["修改前先写可证伪预测。", "小改动更容易建立因果联系。", "回归测试保护未修改路径。"],
        ("一个改动通过定向测试但大量重构无回归测试，最大问题是什么？", ["无法判断对其他路径的影响", "代码行数太少", "图表不够多"], 0, "广泛改动需要回归证据证明未破坏相邻行为。"),
        ("https://huggingface.co/docs/transformers/main/en/llm_tutorial", "Hugging Face：Text generation")),
]


def make_card(spec):
    sections = [{"heading": h, "paragraphs": [p]} for h, p in spec["sections"]]
    figures = [figure(name, caption, takeaway, index) for index, (name, caption, takeaway) in enumerate(spec["figures"])]
    q, options, answer, explanation = spec["question"]
    result = card(spec["concept"], spec["objective"], spec["recall"], spec["minutes"], spec["title"].split("：", 1)[-1],
                  spec["lead"], sections, figures, spec["points"], quiz(spec["concept"], q, options, answer, explanation))
    result["content"]["evidence_quotes"] = [{"claim": "本卡采用原始论文或官方技术文档作为方法依据。", "quote": spec["source"][1], "source": spec["source"][1], "source_url": spec["source"][0]}]
    return result


def source_for(spec):
    url, title = spec["source"]
    existing = next((x for x in storage.list_sources() if x["canonical_uri"] == url), None)
    if existing:
        return existing
    return storage.create_source({"source_type": "paper" if "arxiv.org" in url else "article", "canonical_uri": url,
                                  "title": title, "snapshot_sha256": hashlib.sha256(url.encode()).hexdigest(), "status": "ready"})["source"]


def main():
    db.init_db()
    goals = storage.list_goals()
    conn = storage._conn()
    try:
        # The old Harness demo and the second quality-pilot pack remain recoverable but no longer compete with the seven paths.
        conn.execute("UPDATE v2_learning_packs SET title=? WHERE title=?", ("路径 1/7｜AI 产品实战：问题、评测与架构证据", "AI 产品试学 01｜问题、评测与架构证据"))
        conn.execute("UPDATE v2_learning_packs SET status='archived' WHERE title IN (?,?)", ("从一次 LLM 调用到 Agent Harness", "AI 产品试学 02｜模型边界、系统选择与技术雷达"))
        conn.execute("UPDATE v2_goals SET status='archived' WHERE outcome=?", ("理解 Agent Harness，并能判断不同架构的取舍",))
        conn.execute(
            "UPDATE v2_learning_packs SET milestone_id=(SELECT id FROM v2_milestones m WHERE m.goal_id=v2_learning_packs.goal_id ORDER BY position LIMIT 1) "
            "WHERE status NOT IN ('archived','rejected') AND goal_id IS NOT NULL AND milestone_id IS NULL"
        )
        conn.commit()
    finally:
        conn.close()

    all_packs = storage.list_packs()
    # Replace the six one-card previews with complete three-card packs while retaining the old rows as history.
    conn = storage._conn()
    try:
        for pack in all_packs:
            if pack["title"].startswith("路径 ") and pack["title"] != "路径 1/7｜AI 产品实战：问题、评测与架构证据" and len(pack["cards"]) < 3:
                conn.execute("UPDATE v2_learning_packs SET status='archived' WHERE id=?", (pack["id"],))
        conn.commit()
    finally:
        conn.close()
    visible_by_title = {p["title"]: p for p in storage.list_packs() if p["status"] not in ("archived", "rejected")}
    created = []
    for spec in STARTERS:
        goal = next(g for g in goals if spec["goal_key"] in g["outcome"] and g["status"] != "archived")
        if spec["title"] in visible_by_title:
            existing = visible_by_title[spec["title"]]
            conn = storage._conn()
            try:
                conn.execute(
                    "UPDATE v2_learning_packs SET goal_id=?, milestone_id=?, generation_version=? WHERE id=?",
                    (goal["id"], goal["milestones"][0]["id"], VERSION, existing["id"]),
                )
                conn.commit()
            finally:
                conn.close()
            path_specs = [spec] + [item for item in EXTRAS if item["goal_key"] == spec["goal_key"]]
            if len(existing["cards"]) != len(path_specs):
                raise ValueError("现有学习包卡片数量不完整：%s" % spec["title"])
            for current, card_spec in zip(existing["cards"], path_specs):
                if (current.get("version") or {}).get("generator_version") == VERSION:
                    continue
                definition = make_card(card_spec)
                result = storage.create_card_version(current["id"], {
                    "learning_objective": definition["learning_objective"],
                    "estimated_minutes": definition["estimated_minutes"],
                    "claim_ids": [],
                    "content": definition["content"],
                    "generator_version": VERSION,
                    "publish": True,
                    "provenance": "user_authored",
                })
                if result.get("error"):
                    raise ValueError(result)
            continue
        path_specs = [spec] + [item for item in EXTRAS if item["goal_key"] == spec["goal_key"]]
        if len(path_specs) != 3:
            raise ValueError("路径未形成三卡完整包：%s" % spec["title"])
        source_items = [source_for(item) for item in path_specs]
        source_refs = []
        for source in source_items:
            if source["id"] not in {item["source_id"] for item in source_refs}:
                source_refs.append({"source_id": source["id"], "role": "primary"})
        definitions = [make_card(item) for item in path_specs]
        result = storage.create_pack({"goal_id": goal["id"], "milestone_id": goal["milestones"][0]["id"], "title": spec["title"],
                                      "pack_objective": "完成核心概念、引导练习与迁移任务三步启动学习",
                                      "sources": source_refs, "claims": [],
                                      "cards": [dict(item, position=index) for index, item in enumerate(definitions, 1)],
                                      "provenance": "user_authored", "generation_version": VERSION})
        if result.get("error"):
            raise ValueError(result)
        storage.publish_pack(result["pack"]["id"])
        created.append(result["pack"]["id"])
    print("七路径完整启动包已就绪；本次新增：%d" % len(created))


if __name__ == "__main__":
    main()
