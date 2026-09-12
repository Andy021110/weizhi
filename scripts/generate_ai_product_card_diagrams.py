#!/usr/bin/env python3
"""Generate deterministic teaching diagrams for the AI product trial cards."""
from pathlib import Path
from xml.sax.saxutils import escape


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets" / "ai-product"


DIAGRAMS = {
    "01-problem-chain.svg": ("问题证据链", "先证明问题，再讨论 AI", ["用户任务", "当前做法", "可观察代价", "产品假设"]),
    "02-problem-filter.svg": ("首个项目筛选器", "三个条件缺一不可", ["能接触用户", "任务会重复", "成败可判断", "进入试验"]),
    "03-golden-set.svg": ("黄金测试集构成", "不要只收集顺利样例", ["典型任务 60%", "边界输入 25%", "高风险输入 15%", "固定版本"]),
    "04-eval-loop.svg": ("评测驱动迭代", "每次修改都回到同一把尺", ["固定样例", "运行版本", "按规则评分", "失败归因"]),
    "05-architecture-ladder.svg": ("复杂度阶梯", "只有证据要求时才上台阶", ["单次调用", "检索 / 工具", "固定工作流", "自主 Agent"]),
    "06-architecture-proof.svg": ("升级架构的证据", "组件必须对应一种失败", ["失败样例", "机制假设", "成对实验", "净收益"]),
    "07-llm-boundary.svg": ("LLM 的系统边界", "模型生成候选，系统保证结果", ["上下文", "概率生成", "候选输出", "外部验证"]),
    "08-failure-tree.svg": ("失败假设树", "同一个坏答案可能来自不同层", ["知识缺失", "上下文错误", "工具执行失败", "交互误导"]),
    "09-system-choice.svg": ("按任务选择系统", "路径确定性决定控制方式", ["稳定知识→RAG", "外部状态→工具", "固定步骤→Workflow", "开放路径→Agent"]),
    "10-risk-cost-map.svg": ("收益与代价一起看", "复杂度增加也会放大风险", ["质量提升", "延迟增加", "成本增加", "错误传播"]),
    "11-news-funnel.svg": ("技术动态转化漏斗", "新闻只有进入实验才影响产品", ["原始发布", "能力假设", "最小实验", "产品决策"]),
    "12-release-scorecard.svg": ("新模型对比表", "在自己的任务上判断价值", ["任务质量", "严重错误", "响应延迟", "单任务成本"]),
    "13-token-flow.svg": ("一次 LLM 调用", "从文本到下一个 token", ["文本切分", "token 表示", "logits", "概率采样"]),
    "14-model-lifecycle.svg": ("模型能力从哪里来", "区分训练、对齐和运行时增强", ["预训练", "指令对齐", "上下文 / RAG", "工具执行"]),
    "15-attention-shapes.svg": ("Attention 张量路径", "先追形状，再理解公式", ["X: n×d", "Q,K,V", "QKᵀ: n×n", "输出: n×d"]),
    "16-attention-meaning.svg": ("Attention 的两层理解", "数值操作连接语义问题", ["点积相似度", "缩放与 Softmax", "加权求和", "信息路由"]),
    "17-rag-pipeline.svg": ("RAG 主链路", "每一段都有独立失败模式", ["解析切分", "检索排序", "上下文组装", "生成引用"]),
    "18-rag-diagnosis.svg": ("RAG 失败诊断", "先问证据是否被找到", ["目标证据存在?", "Recall@K", "答案有依据?", "归因改进"]),
    "19-agent-loop.svg": ("Agent Loop", "动作结果必须返回模型", ["模型决策", "结构化动作", "环境执行", "观察 / 继续"]),
    "20-agent-runtime.svg": ("Harness 运行边界", "自主行动需要外部约束", ["权限", "检查点", "事件记录", "停止条件"]),
    "21-lora-choice.svg": ("何时考虑微调", "先排除更便宜的修复", ["提示词基线", "RAG 基线", "稳定行为缺口", "LoRA 实验"]),
    "22-lora-mechanism.svg": ("LoRA 核心机制", "冻结主权重，只训练低秩增量", ["冻结 W", "低秩 A", "低秩 B", "合并增量"]),
    "23-code-trace.svg": ("源码追踪路线", "从入口沿真实数据流阅读", ["公开 API", "配置与对象", "核心 forward", "测试断点"]),
    "24-code-proof.svg": ("源码理解证据", "能修改并验证才算追通", ["调用图", "张量 / 状态", "最小复现", "回归测试"]),
    "25-training-map.svg": ("能力来源地图", "同一问题可能需要不同机制", ["权重知识", "指令行为", "临时上下文", "外部动作"]),
    "26-failure-lab.svg": ("坏回答诊断实验", "一次只改变一个变量", ["固定问题", "替换上下文", "替换模型", "比较输出"]),
    "27-softmax-table.svg": ("Softmax 数值观察", "相对差异决定注意力集中程度", ["原始分数", "减去最大值", "指数归一化", "概率分布"]),
    "28-attention-hand.svg": ("手算 Attention", "小矩阵把公式变成可检查步骤", ["写 Q,K,V", "算分数", "逐行 Softmax", "乘 V"]),
    "29-rag-eval-grid.svg": ("RAG 双层评测", "检索与回答使用两把尺", ["目标证据", "召回命中", "引用忠实", "答案完整"]),
    "30-rag-experiment.svg": ("检索成对实验", "保持查询与测试集不变", ["关键词", "向量", "混合检索", "Rerank"]),
    "31-permission-shell.svg": ("Agent 权限壳", "动作从意图到执行经过多道边界", ["用户意图", "批准", "沙箱", "审计事件"]),
    "32-recovery-timeline.svg": ("长任务恢复时间线", "检查点阻止副作用重复发生", ["Step 1", "Checkpoint", "进程中断", "恢复 Step 2"]),
    "33-data-quality.svg": ("微调数据质量面", "数量只是其中一维", ["任务代表性", "标签一致性", "去重污染", "失败覆盖"]),
    "34-lora-scorecard.svg": ("LoRA 实验计分卡", "训练曲线之外的上线证据", ["盲测质量", "严重错误", "推理成本", "回滚条件"]),
    "35-breakpoint-map.svg": ("源码断点地图", "在状态改变处停下来", ["Tokenizer", "Generation loop", "Model forward", "Logits processor"]),
    "36-minimal-change.svg": ("最小源码改动", "预测、修改、回归构成闭环", ["行为预测", "小改动", "定向测试", "风险说明"]),
    "37-capability-stack.svg": ("LLM 能力栈", "从权重到产品结果的分层视图", ["模型权重", "上下文", "工具与检索", "产品门禁"]),
    "38-baseline-compare.svg": ("方案基线对照", "不同机制解决不同缺口", ["Prompt", "RAG", "Fine-tune", "Tool"]),
    "39-math-code-bridge.svg": ("公式—代码桥", "每个数学符号都落到一个张量操作", ["公式符号", "张量形状", "代码算子", "数值测试"]),
    "40-stability-spectrum.svg": ("Softmax 稳定性", "先平移 logits 再指数化", ["极小值", "普通值", "大值", "溢出风险"]),
    "41-chunk-map.svg": ("切分策略对照", "块边界决定可检索证据单元", ["固定长度", "结构切分", "语义切分", "父子块"]),
    "42-evidence-panel.svg": ("回答证据面板", "答案质量与证据质量分开呈现", ["答案主张", "来源定位", "支持状态", "缺口提示"]),
    "43-tool-contract.svg": ("工具契约剖面", "清晰接口降低模型误用", ["名称与用途", "参数 Schema", "错误结果", "副作用边界"]),
    "44-agent-checkpoints.svg": ("Agent 人工检查点", "高风险动作前保留用户控制", ["规划", "低风险执行", "人工批准", "高风险动作"]),
    "45-dataset-pipeline.svg": ("微调数据流水线", "每次变换都保留版本与来源", ["原始样例", "清洗去重", "训练验证切分", "冻结盲测"]),
    "46-loss-vs-product.svg": ("训练指标与产品指标", "两个下降曲线不代表同一件事", ["训练 Loss", "验证 Loss", "任务成功率", "严重错误率"]),
    "47-runtime-objects.svg": ("运行时对象关系", "从配置到输出跟踪对象所有权", ["Config", "Tokenizer", "Model", "Generation output"]),
    "48-reading-log.svg": ("源码阅读记录", "压缩成可复用的工程证据", ["入口与调用图", "关键状态", "断点截图", "测试结论"]),
}


def visual(nodes, variant):
    parts = []
    if variant == 0:  # sequence
        for index, node in enumerate(nodes):
            x = 48 + index * 262
            parts.append(f'<rect x="{x}" y="220" width="230" height="118" rx="22" class="box"/><text x="{x+115}" y="282" text-anchor="middle" class="node">{escape(node)}</text>')
            if index < 3:
                parts.append(f'<path d="M{x+230} 279 H{x+254}" class="line" marker-end="url(#arrow)"/>')
    elif variant == 1:  # matrix
        for index, node in enumerate(nodes):
            x = 90 + (index % 2) * 520; y = 178 + (index // 2) * 132
            parts.append(f'<rect x="{x}" y="{y}" width="420" height="100" rx="18" class="box alt{index%2}"/><text x="{x+210}" y="{y+59}" text-anchor="middle" class="node">{escape(node)}</text>')
        parts.append('<path d="M560 165 V420 M55 294 H1065" class="axis"/><text x="575" y="408" class="label">对照矩阵</text>')
    elif variant == 2:  # layers
        for index, node in enumerate(nodes):
            x = 100 + index * 38; y = 170 + index * 62; w = 920 - index * 76
            parts.append(f'<rect x="{x}" y="{y}" width="{w}" height="82" rx="16" class="box alt{index%2}"/><text x="560" y="{y+50}" text-anchor="middle" class="node">第 {index+1} 层 · {escape(node)}</text>')
    elif variant == 3:  # cycle
        coords = [(275,210),(845,210),(845,365),(275,365)]
        for index, (x,y) in enumerate(coords):
            parts.append(f'<ellipse cx="{x}" cy="{y}" rx="165" ry="55" class="box alt{index%2}"/><text x="{x}" y="{y+8}" text-anchor="middle" class="node">{escape(nodes[index])}</text>')
        parts.append('<path d="M440 210 H675 M845 265 V302 M680 365 H440 M275 310 V272" class="line" marker-end="url(#arrow)"/>')
        parts.append('<circle cx="560" cy="287" r="62" fill="#c76b43"/><text x="560" y="296" text-anchor="middle" class="center">循环</text>')
    elif variant == 4:  # two-column comparison
        parts.append('<rect x="70" y="170" width="450" height="230" rx="26" class="panelA"/><rect x="600" y="170" width="450" height="230" rx="26" class="panelB"/><text x="295" y="215" text-anchor="middle" class="label">输入 / 基线</text><text x="825" y="215" text-anchor="middle" class="label">结果 / 约束</text>')
        for index,node in enumerate(nodes[:2]): parts.append(f'<text x="295" y="{278+index*66}" text-anchor="middle" class="node">• {escape(node)}</text>')
        for index,node in enumerate(nodes[2:]): parts.append(f'<text x="825" y="{278+index*66}" text-anchor="middle" class="node">• {escape(node)}</text>')
        parts.append('<path d="M520 285 H590" class="line" marker-end="url(#arrow)"/>')
    else:  # spectrum / timeline
        parts.append('<path d="M120 295 H1000" class="axis" marker-end="url(#arrow)"/>')
        for index,node in enumerate(nodes):
            x=150+index*275; y=240 if index%2==0 else 350
            parts.append(f'<circle cx="{x}" cy="295" r="15" fill="#c76b43"/><path d="M{x} 295 V{y}" class="thin"/><rect x="{x-108}" y="{y-48 if index%2==0 else y}" width="216" height="58" rx="14" class="box alt{index%2}"/><text x="{x}" y="{y-12 if index%2==0 else y+37}" text-anchor="middle" class="small">{escape(node)}</text>')
    return ''.join(parts)


def render(title, subtitle, nodes, variant):
    blocks = visual(nodes, variant)
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="1120" height="520" viewBox="0 0 1120 520" role="img" aria-labelledby="title desc">
<title id="title">{escape(title)}</title><desc id="desc">{escape(subtitle)}</desc>
<defs><marker id="arrow" markerWidth="8" markerHeight="8" refX="7" refY="4" orient="auto"><path d="M0,0 L8,4 L0,8 Z" fill="#6f746f"/></marker></defs>
<rect width="1120" height="520" rx="34" fill="#f5f1e8"/>
<text x="48" y="78" class="title">{escape(title)}</text>
<text x="48" y="126" class="subtitle">{escape(subtitle)}</text>
{blocks}
<text x="48" y="478" class="hint">图形承担关系表达；结合正文中的例子与问题阅读。</text>
<style>.title{{font:700 38px -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;fill:#24231f}}.subtitle{{font:24px -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;fill:#68665f}}.node{{font:600 22px -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;fill:#2c2a25}}.small{{font:600 19px -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;fill:#2c2a25}}.label{{font:700 20px -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;fill:#6b4939}}.center{{font:700 22px -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;fill:white}}.hint{{font:20px -apple-system,BlinkMacSystemFont,"PingFang SC",sans-serif;fill:#79756c}}.box{{fill:#fffaf2;stroke:#c76b43;stroke-width:3}}.alt1{{fill:#e9f0eb;stroke:#66806d}}.panelA{{fill:#fff4e9;stroke:#c76b43;stroke-width:3}}.panelB{{fill:#e9f0eb;stroke:#66806d;stroke-width:3}}.line{{stroke:#6f746f;stroke-width:4;fill:none}}.thin{{stroke:#9a8f82;stroke-width:2}}.axis{{stroke:#8b8175;stroke-width:3;fill:none}}</style>
</svg>'''


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for filename, values in DIAGRAMS.items():
        variant = (int(filename[:2]) - 1) % 6
        (OUT / filename).write_text(render(*values, variant), encoding="utf-8")
    print("已生成 %d 张教学图：%s" % (len(DIAGRAMS), OUT))


if __name__ == "__main__":
    main()
