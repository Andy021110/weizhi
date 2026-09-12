# -*- coding: utf-8 -*-
"""微知 v2 · 语义配图（CP11，方案 M3「语义配图与视觉质量门禁」）。

方案 M3 的目标不是「多画几张图」，而是**每张图承担一个明确的理解任务**。
所以流程是「先选图形语法，再决定画什么」，而不是反过来的「先画图再想说明什么」。

职责划分（方案 4.1 / 4.2）：
- **模型**只做语义判断：这段内容需不需要图、需要哪种图形语法、图要解释什么命题；
- **程序**负责渲染、布局、文字溢出检查——图形是确定性的，不该让模型生成 SVG。

方案 3.1 写「每卡两张图」，方案 2.2 又把这条列为**未验证假设**。
所以 `MIN_FIGURES` 默认 0：没有理解价值时允许不出图。
要恢复旧行为把配置调成 2 即可，这是一次实验，不是一个产品规则。

用法::

    import visual
    figs = visual.plan_visuals(provider, draft, claims)
    svg = visual.render(figs[0])
"""
CANVAS_WIDTH = 640          # 移动端友好宽度
MIN_MOBILE_WIDTH = 320      # 窄于此在手机上要横向滚动，判不合格
MAX_FIGURES = 3
MIN_FIGURES = 0             # 「每卡至少两张图」是可实验配置，不是硬规则

# 确定性图（程序渲染 SVG）与非确定性图（需要图片服务）
DETERMINISTIC_KINDS = ("flow", "compare", "timeline", "hierarchy",
                       "matrix", "coordinate", "causal", "architecture")
SCENE_KIND = "scene"
ALL_KINDS = DETERMINISTIC_KINDS + (SCENE_KIND, "none")

# 单个节点标签的字数上限（按显示宽度算，中文算 2）
MAX_LABEL_DISPLAY = 20


def display_width(text):
    """显示宽度：中日韩字符占 2，其余占 1。用来判断文字会不会溢出。"""
    width = 0
    for ch in text or "":
        width += 2 if _is_wide(ch) else 1
    return width


def _is_wide(ch):
    code = ord(ch)
    return (0x1100 <= code <= 0x115F or 0x2E80 <= code <= 0xA4CF
            or 0xAC00 <= code <= 0xD7A3 or 0xF900 <= code <= 0xFAFF
            or 0xFE30 <= code <= 0xFE6F or 0xFF00 <= code <= 0xFF60
            or 0xFFE0 <= code <= 0xFFE6 or 0x20000 <= code <= 0x3FFFD)


def _esc(text):
    return (str(text or "").replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;"))


# ---------- 校验 ----------

def validate_visual_plan(data):
    """校验视觉规划的输出。方案 M3 的验收门槛落在这里。"""
    errs = []
    if not isinstance(data, dict):
        return ["视觉方案必须是 JSON 对象"]
    figures = data.get("figures")
    if not isinstance(figures, list):
        return ["figures 应为数组（不需要配图时给空数组）"]
    if len(figures) > MAX_FIGURES:
        errs.append("figures %d 张 >%d" % (len(figures), MAX_FIGURES))

    for i, fig in enumerate(figures):
        if not isinstance(fig, dict):
            errs.append("figures[%d] 应为对象" % i)
            continue
        kind = fig.get("kind")
        if kind not in ALL_KINDS:
            errs.append("figures[%d].kind=%r 不在可用图形语法里" % (i, kind))
        # 验收门槛：每张图都要能回答一个明确问题
        if len((fig.get("proposition") or "").strip()) < 8:
            errs.append("figures[%d].proposition 缺失或太空泛"
                        "（要写「展示 X 经过哪几步变成 Y」，不是「展示流程」）" % i)
        if len((fig.get("reading") or "").strip()) < 6:
            errs.append("figures[%d].reading 缺失（读图结论没写，无法验证这张图有没有用）" % i)
        if not (fig.get("alt") or "").strip():
            errs.append("figures[%d].alt 缺失" % i)
        plot = fig.get("plot")
        if kind != "none" and not isinstance(plot, dict):
            errs.append("figures[%d].plot 应为对象" % i)
        elif isinstance(plot, dict):
            nodes = plot.get("nodes")
            if nodes is not None and not isinstance(nodes, list):
                errs.append("figures[%d].plot.nodes 应为数组" % i)
            # 坐标图必须有真实数值点：从字符串编一条折线等于造数据
            if kind == "coordinate":
                pts = plot.get("points")
                if not isinstance(pts, list) or len(pts) < 2:
                    errs.append("figures[%d] 是坐标图但没给 plot.points（至少两个数值点）"
                                "——没有真实数值就不许画趋势" % i)
                else:
                    for p in pts:
                        if not (isinstance(p, (list, tuple)) and len(p) == 2
                                and all(isinstance(v, (int, float)) for v in p)):
                            errs.append("figures[%d].plot.points 每项应为 [x, y] 数值对" % i)
                            break
    return errs


def figure_errors(fig, i):
    """单张图的完整性问题清单。返回空列表 = 这张图合格。

    抽出来是为了让「逐张取舍」和「整批校验」共用同一套判据——两处各写
    一份的话，迟早一边改严一边没改，而差异只会表现为「有的图能过有的
    图不能过」这种最难查的现象。
    """
    errs = []
    if not isinstance(fig, dict):
        return ["figures[%d] 应为对象" % i]
    kind = fig.get("kind")
    if kind not in ALL_KINDS:
        errs.append("figures[%d].kind=%r 不在可用图形语法里" % (i, kind))
    if len((fig.get("proposition") or "").strip()) < 8:
        errs.append("figures[%d].proposition 缺失或太空泛" % i)
    if len((fig.get("reading") or "").strip()) < 6:
        errs.append("figures[%d].reading 缺失" % i)
    if not (fig.get("alt") or "").strip():
        errs.append("figures[%d].alt 缺失" % i)
    plot = fig.get("plot")
    if kind != "none" and not isinstance(plot, dict):
        errs.append("figures[%d].plot 应为对象" % i)
    elif isinstance(plot, dict):
        if plot.get("nodes") is not None and not isinstance(plot.get("nodes"), list):
            errs.append("figures[%d].plot.nodes 应为数组" % i)
        if kind == "coordinate":
            pts = plot.get("points")
            if not isinstance(pts, list) or len(pts) < 2:
                errs.append("figures[%d] 是坐标图但没给 plot.points" % i)
            else:
                for p in pts:
                    if not (isinstance(p, (list, tuple)) and len(p) == 2
                            and all(isinstance(v, (int, float)) for v in p)):
                        errs.append("figures[%d].plot.points 每项应为 [x, y] 数值对" % i)
                        break
    return errs


def validate_visual_plan_shape(data):
    """宽校验：只看整体结构，给 provider 重试用。

    **为什么要和 `validate_visual_plan` 分开**：那份是「一张不合格就整批
    重试、重试耗尽整批作废」——那是给「模型根本没按格式输出」准备的。
    但对「三张图里第三张忘了给数值点」这种局部问题就过重了，真实代价是
    **整张卡一张图都没有**（配图是 v2 的主要卖点之一）。
    所以局部问题交给 `plan_visuals` 逐张过滤，这里只拦「压根不是方案」。
    """
    if not isinstance(data, dict):
        return ["视觉方案必须是 JSON 对象"]
    figures = data.get("figures")
    if not isinstance(figures, list):
        return ["figures 应为数组（不需要配图时给空数组）"]
    if len(figures) > MAX_FIGURES:
        return ["figures %d 张 >%d" % (len(figures), MAX_FIGURES)]
    return []


def check_visual_quality(figures):
    """视觉质量门禁，返回 [(标签, 详情)]。"""
    issues = []
    if not isinstance(figures, list):
        return [("图形方案非法", "figures 不是数组")]

    prev_kind = None
    for i, fig in enumerate(figures):
        kind = fig.get("kind")
        # 方案 M3：同一学习包内不连续使用完全相同的图形语法
        if kind == prev_kind and kind not in (None, "none"):
            issues.append(("图形语法重复",
                           "第 %d 张与上一张都是 %s，连续同构会让人以为内容也一样" % (i + 1, kind)))
        prev_kind = kind

        # 方案 M3：图中文字无截断
        for node in (fig.get("plot") or {}).get("nodes") or []:
            if display_width(node) > MAX_LABEL_DISPLAY:
                issues.append(("文字溢出",
                               "「%s」显示宽度 %d > %d，渲染时会被截断"
                               % (node, display_width(node), MAX_LABEL_DISPLAY)))
        if CANVAS_WIDTH < MIN_MOBILE_WIDTH:
            issues.append(("画布过窄", "宽度 %d < %d，手机上要横向滚动" % (CANVAS_WIDTH, MIN_MOBILE_WIDTH)))
    return issues


# ---------- 渲染 ----------

def render(fig, width=CANVAS_WIDTH):
    """把一张图渲染成 SVG。只有确定性图形可以渲染；scene 需要图片服务。

    文字位置全部由布局算出，不交给模型——这是「图形是确定性的」的落点。
    """
    kind = (fig or {}).get("kind")
    if kind == SCENE_KIND:
        raise ValueError("scene 类型需要图片服务（Image Provider），不走确定性渲染")
    if kind in (None, "none"):
        return ""
    plot = fig.get("plot") or {}
    nodes = [n for n in (plot.get("nodes") or []) if str(n).strip()]
    if not nodes:
        raise ValueError("图形缺少 plot.nodes，无法渲染")
    builder = _BUILDERS.get(kind)
    if not builder:
        raise ValueError("不支持的图形语法: %r" % kind)
    return builder(nodes, plot, width)


def _svg(height, body, width):
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 %d %d" '
        'width="100%%" height="%d" role="img">\n%s\n</svg>' % (width, height, height, body)
    )


def _box(x, y, w, h, text, fill="#eef2ff", stroke="#4f46e5"):
    return (
        '<rect x="%d" y="%d" width="%d" height="%d" rx="8" fill="%s" stroke="%s"/>'
        '\n<text x="%d" y="%d" text-anchor="middle" font-size="14" fill="#1f2937">%s</text>'
        % (x, y, w, h, fill, stroke, x + w // 2, y + h // 2 + 5, _esc(text))
    )


def _arrow(x1, y1, x2, y2):
    return ('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#94a3b8" '
            'stroke-width="2" marker-end="url(#a)"/>' % (x1, y1, x2, y2))


def _wrap(body):
    return '<defs><marker id="a" markerWidth="8" markerHeight="8" refX="6" refY="3" ' \
           'orient="auto"><path d="M0,0 L6,3 L0,6 z" fill="#94a3b8"/></marker></defs>\n' + body


def _render_flow(nodes, plot, width):
    """横向流程：节点按顺序排，箭头连起来。超过 4 个换行排。"""
    per_row = min(4, len(nodes))
    bw, bh, gap = 130, 56, 24
    rows = [nodes[i:i + per_row] for i in range(0, len(nodes), per_row)]
    body = []
    height = 40 + len(rows) * (bh + gap)
    for r, row in enumerate(rows):
        total = len(row) * bw + (len(row) - 1) * gap
        x0 = (width - total) // 2
        y = 20 + r * (bh + gap)
        for i, node in enumerate(row):
            x = x0 + i * (bw + gap)
            body.append(_box(x, y, bw, bh, node))
            if i < len(row) - 1:
                body.append(_arrow(x + bw, y + bh // 2, x + bw + gap, y + bh // 2))
    return _svg(height, _wrap("\n".join(body)), width)


def _render_compare(nodes, plot, width):
    """左右对比：节点按奇偶分到两侧。"""
    mid = (len(nodes) + 1) // 2
    left, right = nodes[:mid], nodes[mid:]
    bw, bh, gap = 250, 52, 18
    body = []
    height = 40 + max(len(left), len(right)) * (bh + gap)
    for i, node in enumerate(left):
        body.append(_box(30, 20 + i * (bh + gap), bw, bh, node, "#ecfdf5", "#059669"))
    for i, node in enumerate(right):
        body.append(_box(width - 30 - bw, 20 + i * (bh + gap), bw, bh, node, "#fef2f2", "#dc2626"))
    labels = plot.get("labels") or ["A", "B"]
    body.append('<text x="%d" y="14" text-anchor="middle" font-size="12" fill="#6b7280">%s</text>'
                % (30 + bw // 2, _esc(labels[0])))
    body.append('<text x="%d" y="14" text-anchor="middle" font-size="12" fill="#6b7280">%s</text>'
                % (width - 30 - bw // 2, _esc(labels[1] if len(labels) > 1 else "B")))
    return _svg(height, _wrap("\n".join(body)), width)


def _render_timeline(nodes, plot, width):
    """纵向时间线：左侧轴，右侧节点。"""
    body = ['<line x1="70" y1="24" x2="70" y2="%d" stroke="#c7d2fe" stroke-width="3"/>'
            % (40 + len(nodes) * 56)]
    height = 60 + len(nodes) * 56
    for i, node in enumerate(nodes):
        y = 44 + i * 56
        body.append('<circle cx="70" cy="%d" r="6" fill="#4f46e5"/>' % y)
        body.append(_box(96, y - 18, width - 140, 38, node))
    return _svg(height, _wrap("\n".join(body)), width)


def _render_hierarchy(nodes, plot, width):
    """层级：第一个是根，其余并排作为下一层。"""
    body = [_box((width - 220) // 2, 16, 220, 50, nodes[0], "#eff6ff", "#2563eb")]
    children = nodes[1:]
    if not children:
        return _svg(90, _wrap("\n".join(body)), width)
    per = min(3, len(children))
    bw, gap = 180, 16
    rows = [children[i:i + per] for i in range(0, len(children), per)]
    height = 96 + len(rows) * 74
    for r, row in enumerate(rows):
        total = len(row) * bw + (len(row) - 1) * gap
        x0 = (width - total) // 2
        y = 96 + r * 74
        for i, node in enumerate(row):
            x = x0 + i * (bw + gap)
            body.append(_arrow(width // 2, 66 + r * 74, x + bw // 2, y))
            body.append(_box(x, y, bw, 50, node, "#f5f3ff", "#7c3aed"))
    return _svg(height, _wrap("\n".join(body)), width)


def _render_matrix(nodes, plot, width):
    """2x2 矩阵：节点按顺序放进四个象限。"""
    labels = plot.get("labels") or ["维度 A", "维度 B"]
    size, pad = 230, 60
    origins = [(pad, 50), (pad + size + 20, 50), (pad, 50 + size + 20), (pad + size + 20, 50 + size + 20)]
    body = ['<text x="%d" y="34" text-anchor="middle" font-size="12" fill="#6b7280">%s</text>'
            % (pad + size, _esc(labels[0]))]
    body.append('<text x="18" y="%d" text-anchor="middle" font-size="12" fill="#6b7280" '
                'transform="rotate(-90 18 %d)">%s</text>'
                % (50 + size // 2, 50 + size // 2, _esc(labels[1] if len(labels) > 1 else "维度 B")))
    for i, (x, y) in enumerate(origins):
        if i >= len(nodes):
            break
        body.append('<rect x="%d" y="%d" width="%d" height="%d" fill="#f8fafc" '
                    'stroke="#e2e8f0" stroke-dasharray="4 3"/>' % (x, y, size, size))
        body.append(_box(x + 12, y + size // 2 - 22, size - 24, 44, nodes[i], "#eef2ff", "#4f46e5"))
    return _svg(50 + size * 2 + 30, _wrap("\n".join(body)), width)


def _render_coordinate(nodes, plot, width):
    """坐标图：必须由模型给出真实数值点。

    为什么不从字符串节点编一条折线：那等于凭空造出一组趋势数据，
    而方案 2.1 明确把「数字改写」列为模型风险。宁可拒绝渲染，也不画假趋势。
    """
    points = plot.get("points") or []
    left, right, top, bottom = 60, 30, 30, 46
    plot_w, plot_h = width - left - right, 220
    body = ['<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#cbd5e1" stroke-width="2"/>'
            % (left, top + plot_h, left + plot_w, top + plot_h),
            '<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="#cbd5e1" stroke-width="2"/>'
            % (left, top, left, top + plot_h)]
    xs = [float(p[0]) for p in points]
    ys = [float(p[1]) for p in points]
    x0, x1 = min(xs), max(xs) or 1
    y0, y1 = min(ys), max(ys) or 1
    span_x = (x1 - x0) or 1
    span_y = (y1 - y0) or 1
    coords = []
    for px, py in points:
        cx = left + (float(px) - x0) / span_x * plot_w
        cy = top + plot_h - (float(py) - y0) / span_y * plot_h
        coords.append((cx, cy))
        body.append('<circle cx="%.1f" cy="%.1f" r="5" fill="#4f46e5"/>' % (cx, cy))
    if len(coords) > 1:
        body.append('<polyline points="%s" fill="none" stroke="#a5b4fc" stroke-width="2"/>'
                    % " ".join("%.1f,%.1f" % c for c in coords))
    labels = plot.get("labels") or []
    for i, node in enumerate(nodes[:len(coords)]):
        body.append('<text x="%.1f" y="%d" text-anchor="middle" font-size="12" fill="#374151">%s</text>'
                    % (coords[i][0], top + plot_h + 20, _esc(node)))
    if labels:
        body.append('<text x="%d" y="14" text-anchor="middle" font-size="12" fill="#6b7280">%s</text>'
                    % (left + plot_w // 2, _esc(labels[0])))
    return _svg(top + plot_h + bottom + 20, _wrap("\n".join(body)), width)


def _render_causal(nodes, plot, width):
    """因果链：纵向，箭头向下，强调传导。"""
    bw, bh, gap = 380, 48, 26
    body = []
    height = 30 + len(nodes) * (bh + gap)
    for i, node in enumerate(nodes):
        y = 20 + i * (bh + gap)
        body.append(_box((width - bw) // 2, y, bw, bh, node, "#fff7ed", "#ea580c"))
        if i < len(nodes) - 1:
            body.append(_arrow(width // 2, y + bh, width // 2, y + bh + gap))
    return _svg(height, _wrap("\n".join(body)), width)


def _render_architecture(nodes, plot, width):
    """分层架构：每个节点是一层，从上到下堆叠。"""
    bw, bh, gap = 440, 56, 14
    body = []
    height = 30 + len(nodes) * (bh + gap)
    for i, node in enumerate(nodes):
        y = 20 + i * (bh + gap)
        body.append(_box((width - bw) // 2, y, bw, bh, node, "#f0fdfa", "#0d9488"))
    return _svg(height, _wrap("\n".join(body)), width)


_BUILDERS = {
    "flow": _render_flow,
    "compare": _render_compare,
    "timeline": _render_timeline,
    "hierarchy": _render_hierarchy,
    "matrix": _render_matrix,
    "coordinate": _render_coordinate,
    "causal": _render_causal,
    "architecture": _render_architecture,
}


# ---------- 规划 ----------

TASK = "visual_plan"


def build_inputs(draft, claims, evidence_block=None):
    import card_writer
    return {
        "objective": draft.get("objective") or "",
        "body_block": card_writer.render_body(draft),
        "evidence_block": evidence_block if evidence_block is not None
        else card_writer.render_evidence(claims or []),
    }


def plan_visuals(provider, draft, claims=None, evidence_block=None):
    """规划配图。没有理解价值时返回空列表——这是允许的，不是失败。

    两级校验：
    - 整体结构交给 provider 重试（`validate_visual_plan_shape`）；
    - **逐张质量在本地过滤**（`figure_errors`）。

    为什么不是一股脑交给 provider 重试：那张图不合格就整批重试、重试耗尽
    整批作废，真实代价是**整张卡一张图都没有**。真实踩过——模型给了三张图，
    第三张坐标图忘了给数值点，前两张合格的也被一起丢掉，卡片以「配图 0」
    入库。丢一张和丢三张不成比例，所以各丢各的。
    """
    data = provider.generate_json(
        TASK, validate_visual_plan_shape, build_inputs(draft, claims, evidence_block))
    figures = []
    for i, fig in enumerate(data.get("figures") or []):
        errs = figure_errors(fig, i)
        if errs:
            # 不静默：丢掉的是图，但要知道丢了几张、为什么丢
            print("配图跳过 figures[%d]：%s" % (i, "；".join(errs)))
            continue
        item = dict(fig)
        item["id"] = "fig%d" % (i + 1)
        item["render_mode"] = "deterministic" if item.get("kind") in DETERMINISTIC_KINDS else "image"
        figures.append(item)
    return figures


def attach(draft_id, figures):
    """把图形方案挂到草稿上（落 v2_card_drafts.fixtures）。"""
    import db
    db.save_v2_draft_figures(draft_id, figures)
    return figures