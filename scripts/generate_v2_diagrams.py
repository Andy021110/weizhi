#!/usr/bin/env python3
"""Generate deterministic learning diagrams for the Harness demo pack."""
from pathlib import Path
from xml.sax.saxutils import escape


ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "assets" / "v2"

BG = "#fbf8f1"
INK = "#28251f"
MUTED = "#746f66"
LINE = "#d9d0c2"
ACCENT = "#a85f3d"
ACCENT_SOFT = "#f3dfd2"
GREEN = "#49745b"
GREEN_SOFT = "#dfece3"


def text(x, y, value, size=26, weight=500, fill=INK, anchor="start"):
    return f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{weight}" fill="{fill}" text-anchor="{anchor}">{escape(value)}</text>'


def box(x, y, w, h, title, note="", fill="#fffdf8", stroke=LINE, title_size=28):
    parts = [f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="22" fill="{fill}" stroke="{stroke}" stroke-width="2"/>']
    parts.append(text(x + w / 2, y + 48, title, title_size, 700, INK, "middle"))
    if note:
        parts.append(text(x + w / 2, y + 82, note, 18, 400, MUTED, "middle"))
    return "".join(parts)


def arrow(x1, y1, x2, y2, color=ACCENT):
    return f'<path d="M{x1} {y1} L{x2} {y2}" stroke="{color}" stroke-width="4" fill="none" marker-end="url(#arrow)"/>'


def canvas(title_value, desc_value, body):
    return f'''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1200 675" role="img" aria-labelledby="title desc">
<title id="title">{escape(title_value)}</title><desc id="desc">{escape(desc_value)}</desc>
<defs><marker id="arrow" markerWidth="12" markerHeight="12" refX="9" refY="4" orient="auto"><path d="M0,0 L0,8 L10,4 z" fill="{ACCENT}"/></marker></defs>
<rect width="1200" height="675" rx="28" fill="{BG}"/>
{text(64, 72, title_value, 34, 750)}
{text(64, 108, desc_value, 19, 400, MUTED)}
{body}
</svg>'''


def evolution_ladder():
    labels = [
        ("LLM", "一次输入 → 输出"), ("Context", "补齐本轮所需信息"),
        ("Tools", "接触外部世界"), ("Loop", "依据结果继续行动"),
        ("Memory", "跨会话检索经验"),
    ]
    parts = []
    for i, (name, note) in enumerate(labels):
        x = 55 + i * 225
        fill = ACCENT_SOFT if i in (1, 3) else "#fffdf8"
        parts.append(box(x, 250, 190, 126, name, note, fill=fill, title_size=26))
        if i < len(labels) - 1:
            parts.append(arrow(x + 190, 313, x + 218, 313))
    parts.append(text(600, 465, "每一层都在回答前一层暴露出的边界", 27, 700, ACCENT, "middle"))
    parts.append(text(600, 510, "先找缺失能力，再决定是否增加组件", 20, 400, MUTED, "middle"))
    return canvas("Agent 能力如何逐层出现", "从一次模型调用出发，而不是从组件清单出发", "".join(parts))


def action_loop():
    parts = [box(470, 250, 260, 130, "当前上下文", "模型每轮真正看到的内容", fill=ACCENT_SOFT)]
    nodes = [(470, 145, "模型", "生成下一步"), (825, 285, "动作", "调用工具"), (470, 455, "环境", "产生真实变化"), (115, 285, "观察", "返回执行结果")]
    for x, y, name, note in nodes:
        parts.append(box(x, y, 260, 105, name, note, fill="#fffdf8"))
    parts += [arrow(730, 196, 825, 300), arrow(955, 390, 730, 498), arrow(470, 505, 375, 390), arrow(245, 285, 470, 196)]
    parts.append(text(600, 624, "闭环成立后，系统才会根据真实结果继续决策", 23, 700, GREEN, "middle"))
    return canvas("Agent 的动作—观察闭环", "模型不直接改变环境；外部程序执行并把观察送回上下文", "".join(parts))


def harness_layers():
    parts = [f'<rect x="90" y="155" width="1020" height="430" rx="34" fill="#fffdf8" stroke="{ACCENT}" stroke-width="4"/>']
    parts.append(text(125, 207, "HARNESS 运行边界", 23, 750, ACCENT))
    parts.append(f'<rect x="350" y="270" width="500" height="190" rx="95" fill="{ACCENT_SOFT}" stroke="{ACCENT}" stroke-width="2"/>')
    parts.append(text(600, 345, "Agent Loop", 38, 750, INK, "middle"))
    parts.append(text(600, 388, "选择下一步动作", 21, 400, MUTED, "middle"))
    items = [(130, 245, "Context", "看到什么"), (865, 245, "Permissions", "允许什么"), (130, 455, "Persistence", "怎样恢复"), (865, 455, "Events", "怎样观察")]
    for x, y, name, note in items:
        parts.append(box(x, y, 210, 96, name, note, fill=GREEN_SOFT, stroke="#b9d2c1", title_size=23))
    parts.append(text(600, 630, "Loop 决定动作；Harness 决定动作发生的条件", 25, 700, GREEN, "middle"))
    return canvas("Loop 与 Harness 的职责边界", "把“下一步做什么”和“这一步如何安全运行”分开", "".join(parts))


def approval_sandbox():
    parts = [box(70, 260, 235, 130, "候选动作", "例如：写文件、联网", fill="#fffdf8")]
    parts.append(arrow(305, 325, 385, 325))
    parts.append(box(385, 230, 260, 190, "Approval", "用户是否允许这次动作", fill=ACCENT_SOFT, stroke=ACCENT))
    parts.append(arrow(645, 325, 725, 325))
    parts.append(box(725, 230, 260, 190, "Sandbox", "实际可以访问哪些资源", fill=GREEN_SOFT, stroke=GREEN))
    parts.append(arrow(985, 325, 1125, 325))
    parts.append(text(1080, 285, "受限执行", 22, 700, INK, "middle"))
    parts.append(text(1080, 320, "文件 / 网络", 17, 400, MUTED, "middle"))
    parts.append(text(1080, 348, "系统能力", 17, 400, MUTED, "middle"))
    parts.append(text(515, 500, "表达用户意图", 20, 700, ACCENT, "middle"))
    parts.append(text(855, 500, "限制技术边界", 20, 700, GREEN, "middle"))
    parts.append(text(600, 580, "批准通过后，沙箱仍然继续生效", 28, 750, INK, "middle"))
    return canvas("安全执行需要两道门", "Approval 与 Sandbox 解决不同问题，不能互相替代", "".join(parts))


def tradeoff_map():
    parts = [arrow(145, 545, 1080, 545, MUTED), arrow(145, 545, 145, 155, MUTED)]
    parts.append(text(1090, 580, "任务长度与恢复需求 →", 20, 600, MUTED, "end"))
    parts.append(text(55, 175, "风险与控制需求 ↑", 20, 600, MUTED))
    positions = [(310, 455, "Pi", "小工作集", ACCENT_SOFT), (675, 395, "OpenCode", "事件与恢复", "#e7e5f2"), (885, 225, "Codex", "安全与生命周期", GREEN_SOFT), (430, 225, "Hermes", "长期记忆复用", "#efe4d4")]
    for x, y, name, note, fill in positions:
        parts.append(box(x, y, 220, 100, name, note, fill=fill, title_size=25))
    parts.append(text(600, 630, "位置表示关注重点，不表示统一排名", 22, 700, ACCENT, "middle"))
    return canvas("四种 Harness 路线的取舍地图", "根据任务特征选能力，而不是先选框架名称", "".join(parts))


def decision_tree():
    questions = [("任务跨多天？", "事件 + 恢复"), ("动作有高风险副作用？", "Approval + Sandbox"), ("需要多端共享状态？", "稳定任务身份 + 投影"), ("经验值得跨会话复用？", "Memory + Skills 治理")]
    parts = []
    for i, (question, capability) in enumerate(questions):
        y = 155 + i * 112
        parts.append(box(80, y, 430, 82, question, fill="#fffdf8", title_size=23))
        parts.append(arrow(510, y + 41, 650, y + 41))
        parts.append(box(650, y, 460, 82, capability, "回答“是”时增加", fill=GREEN_SOFT, stroke="#b9d2c1", title_size=22))
    parts.append(text(600, 635, "回答“否”时保持系统更小，避免为未来假设支付复杂度", 23, 700, ACCENT, "middle"))
    return canvas("用四个问题决定 Harness 规模", "能力应由现实约束触发，而不是一次性全部安装", "".join(parts))


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    diagrams = {
        "01-evolution-ladder.svg": evolution_ladder(),
        "02-action-loop.svg": action_loop(),
        "03-harness-layers.svg": harness_layers(),
        "04-approval-sandbox.svg": approval_sandbox(),
        "05-tradeoff-map.svg": tradeoff_map(),
        "06-decision-tree.svg": decision_tree(),
    }
    for name, data in diagrams.items():
        (OUT / name).write_text(data, encoding="utf-8")
    print("已生成 %d 张学习图：%s" % (len(diagrams), OUT))


if __name__ == "__main__":
    main()
