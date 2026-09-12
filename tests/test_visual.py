"""CP11 测试：语义配图。图形语法选择、确定性渲染、视觉门禁。"""
import xml.etree.ElementTree as ET

import pytest

import db
import visual
from providers import FakeTextProvider

GOOD_FIG = {
    "kind": "flow",
    "proposition": "Agent 一次调用经过哪四步变成答案",
    "reading": "少任何一步循环都闭合不了",
    "caption": "循环的四个阶段",
    "alt": "四个方框按顺序排列，箭头依次相连",
    "plot": {"nodes": ["组装上下文", "模型调用", "工具执行", "状态回写"]},
}


def _provider(figures):
    return FakeTextProvider(responder=lambda task, inputs: {"figures": figures})


# ---------- 校验 ----------

def test_empty_figures_is_allowed():
    """方案 2.2 把「每卡两张图」列为未验证假设——没有理解价值时允许不出图。"""
    assert visual.validate_visual_plan({"figures": []}) == []


def test_good_figure_passes():
    assert visual.validate_visual_plan({"figures": [GOOD_FIG]}) == []


@pytest.mark.parametrize("mutate,expect", [
    (lambda f: f.update(kind="pie"), "kind"),
    (lambda f: f.update(proposition="展示流程"), "proposition"),
    (lambda f: f.pop("reading"), "reading"),
    (lambda f: f.pop("alt"), "alt"),
    (lambda f: f.update(plot="不是对象"), "plot"),
])
def test_rejects_bad_figure(mutate, expect):
    fig = dict(GOOD_FIG)
    mutate(fig)
    errs = visual.validate_visual_plan({"figures": [fig]})
    assert errs and any(expect in e for e in errs), errs


def test_rejects_too_many_figures():
    errs = visual.validate_visual_plan({"figures": [dict(GOOD_FIG)] * (visual.MAX_FIGURES + 1)})
    assert any(">" in e for e in errs)


def test_coordinate_requires_real_numbers():
    """坐标图没有真实数值就不许画趋势——那等于凭空造数据。"""
    fig = dict(GOOD_FIG, kind="coordinate", plot={"nodes": ["v1", "v2"]})
    errs = visual.validate_visual_plan({"figures": [fig]})
    assert any("points" in e for e in errs)

    ok = dict(GOOD_FIG, kind="coordinate", plot={"nodes": ["v1", "v2"], "points": [[1, 2], [2, 4]]})
    assert visual.validate_visual_plan({"figures": [ok]}) == []


# ---------- 视觉门禁 ----------

def test_gate_catches_consecutive_same_kind():
    """方案 M3：同一学习包内不连续使用完全相同的图形语法。"""
    figs = [dict(GOOD_FIG), dict(GOOD_FIG)]
    issues = visual.check_visual_quality(figs)
    assert any(t == "图形语法重复" for t, _ in issues)


def test_gate_allows_same_kind_when_not_adjacent():
    figs = [dict(GOOD_FIG), dict(GOOD_FIG, kind="timeline"), dict(GOOD_FIG)]
    assert visual.check_visual_quality(figs) == []


def test_gate_catches_text_overflow():
    """方案 M3：图中文字无截断。"""
    long_node = "这是一个远远超出方框宽度的节点标签文字"
    fig = dict(GOOD_FIG, plot={"nodes": [long_node]})
    issues = visual.check_visual_quality([fig])
    assert any(t == "文字溢出" for t, _ in issues)


def test_display_width_counts_cjk_as_two():
    assert visual.display_width("ab") == 2
    assert visual.display_width("中文") == 4
    assert visual.display_width("中a") == 3


# ---------- 渲染 ----------

DETERMINISTIC_FIGS = [
    dict(GOOD_FIG, kind="flow", plot={"nodes": ["一步", "两步", "三步"]}),
    dict(GOOD_FIG, kind="compare", plot={"nodes": ["方案甲", "方案乙", "方案丙"], "labels": ["旧", "新"]}),
    dict(GOOD_FIG, kind="timeline", plot={"nodes": ["2022 提出", "2023 标准化", "2024 协议化"]}),
    dict(GOOD_FIG, kind="hierarchy", plot={"nodes": ["运行时", "上下文", "工具", "记忆"]}),
    dict(GOOD_FIG, kind="matrix", plot={"nodes": ["高频高价值", "低频高价值", "高频低价值", "低频低价值"],
                                        "labels": ["流量", "价值"]}),
    dict(GOOD_FIG, kind="causal", plot={"nodes": ["模型无状态", "输出漂移", "必须持续评估"]}),
    dict(GOOD_FIG, kind="architecture", plot={"nodes": ["接入层", "编排层", "工具层"]}),
]


@pytest.mark.parametrize("fig", DETERMINISTIC_FIGS)
def test_all_deterministic_kinds_render_valid_svg(fig):
    svg = visual.render(fig)
    root = ET.fromstring(svg)          # 不合法会直接抛异常
    assert root.tag.endswith("svg")
    assert 'viewBox="0 0 %d' % visual.CANVAS_WIDTH in svg
    assert svg.count("<text") >= 1


def test_scene_kind_needs_image_service():
    """场景插图是唯一非确定性的图形，必须走图片服务，不能本地编。"""
    fig = dict(GOOD_FIG, kind="scene")
    with pytest.raises(ValueError, match="图片服务"):
        visual.render(fig)


def test_none_kind_renders_nothing():
    assert visual.render({"kind": "none"}) == ""


def test_render_refuses_empty_nodes():
    with pytest.raises(ValueError, match="plot.nodes"):
        visual.render(dict(GOOD_FIG, plot={"nodes": []}))


def test_render_escapes_markup():
    """节点文本来自模型，必须转义，否则能注入 SVG 标签。"""
    svg = visual.render(dict(GOOD_FIG, plot={"nodes": ["<script>x</script>"]}))
    assert "<script>" not in svg
    ET.fromstring(svg)


# ---------- 规划与落库 ----------

def test_plan_visuals_assigns_ids_and_render_mode(tmp_db):
    figs = visual.plan_visuals(_provider([GOOD_FIG, dict(GOOD_FIG, kind="scene")]),
                               {"objective": "x", "title": "t"})
    assert [f["id"] for f in figs] == ["fig1", "fig2"]
    assert figs[0]["render_mode"] == "deterministic"
    assert figs[1]["render_mode"] == "image"


def test_attach_and_read_back(tmp_db):
    did = db.save_v2_card_draft(
        input_hash="h1", schema_version="1.0", payload={"title": "t"}, goal_key="g")
    visual.attach(did, [GOOD_FIG])
    assert db.get_v2_draft_figures(did)[0]["kind"] == "flow"
    assert db.get_v2_card_draft_by_id(did)["figures"]


def test_default_min_figures_is_zero():
    """「每卡至少两张图」是实验配置不是产品规则——默认允许不出图。"""
    assert visual.MIN_FIGURES == 0


# ---------- 逐张取舍：一张坏图不该让整卡失去全部配图 ----------

def _bad_coordinate():
    """一张忘了给真实数值点的坐标图。"""
    return dict(GOOD_FIG, kind="coordinate", plot={"nodes": ["v1", "v2"]})


def test_plan_visuals_drops_only_the_bad_figure(tmp_db):
    """回归：模型给了三张图，第三张坐标图没给数值点，前两张合格的被一起丢掉，
    卡片以「配图 0」入库。丢一张和丢三张不成比例。"""
    figs = visual.plan_visuals(
        _provider([GOOD_FIG, dict(GOOD_FIG), _bad_coordinate()]),
        {"objective": "x", "title": "t"})
    assert len(figs) == 2
    assert all(f["kind"] == "flow" for f in figs)
    assert [f["id"] for f in figs] == ["fig1", "fig2"]


def test_plan_visuals_all_bad_returns_empty(tmp_db):
    """全都不合格 = 这张卡没有配图。这是允许的结果，不是崩溃。"""
    figs = visual.plan_visuals(_provider([_bad_coordinate()]),
                               {"objective": "x", "title": "t"})
    assert figs == []


def test_plan_visuals_still_fails_on_broken_shape(tmp_db):
    """宽校验只放宽局部问题：整体结构坏了（figures 不是数组）仍要失败，
    否则模型输出跑偏会被当成「今天不用配图」。"""
    with pytest.raises(Exception):
        visual.plan_visuals(_provider("不是数组"), {"objective": "x", "title": "t"})


def test_plan_visuals_still_caps_figure_count(tmp_db):
    with pytest.raises(Exception):
        visual.plan_visuals(_provider([dict(GOOD_FIG)] * (visual.MAX_FIGURES + 1)),
                            {"objective": "x", "title": "t"})


def test_shape_check_only_cares_about_structure():
    assert visual.validate_visual_plan_shape({"figures": []}) == []
    assert visual.validate_visual_plan_shape({"figures": [GOOD_FIG]}) == []
    # 局部内容问题不该在这里报出来——那是逐张过滤的职责
    assert visual.validate_visual_plan_shape({"figures": [_bad_coordinate()]}) == []
    assert visual.validate_visual_plan_shape({"figures": "不是数组"})
    assert visual.validate_visual_plan_shape({"figures": [GOOD_FIG] * (visual.MAX_FIGURES + 1)})


def test_figure_errors_agrees_with_whole_plan_check():
    """两套判据不能漂移：单图检查报的问题，整批检查必须同样报出来。"""
    for fig in (GOOD_FIG, _bad_coordinate(), dict(GOOD_FIG, kind="pie"),
                dict(GOOD_FIG, proposition="短"), dict(GOOD_FIG, alt="")):
        assert bool(visual.figure_errors(fig, 0)) == bool(
            visual.validate_visual_plan({"figures": [fig]})), fig

