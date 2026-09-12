# -*- coding: utf-8 -*-
"""前端注入逻辑的测试：真的把 reader.html 里的函数抽出来用 node 跑。

**为什么要这么测**：`renderFigures` 是用户能不能看到 v2 配图的唯一关口，
而它是 JS。此前整个前端零测试——改错了不会有任何信号，只会「图不显示」，
而且没人知道是后端没发还是前端没渲染。

做法：从 reader.html 里按括号配对抽出 `escapeHtml` 与 `renderFigures`，
拼一个最小 harness 交给 node 执行，把结果打成 JSON 读回来。
本机没 node 时整体跳过（不误报失败）。
"""
import json
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
READER = os.path.join(ROOT, "weizhi", "serve", "web", "reader.html")

NODE_CANDIDATES = (
    "/Users/minghan/.workbuddy/binaries/node/versions/22.22.2/bin/node",
    "node",
)


def _node():
    for c in NODE_CANDIDATES:
        if os.path.isabs(c):
            if os.path.exists(c):
                return c
        else:
            found = shutil.which(c)
            if found:
                return found
    return None


pytestmark = pytest.mark.skipif(_node() is None, reason="本机无 node，跳过前端逻辑测试")


def _extract(src, name):
    """按括号配对抽出一个顶层 function 的完整源码。"""
    start = src.index("function %s(" % name)
    i = src.index("{", start)
    depth = 0
    while i < len(src):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start:i + 1]
        i += 1
    raise AssertionError("括号不配对: %s" % name)


def _run_render(figs):
    """在 node 里对 figs 调一次 renderFigures，返回 HTML 字符串。"""
    src = open(READER, encoding="utf-8").read()
    js = "\n".join([
        _extract(src, "escapeHtml"),
        _extract(src, "renderFigures"),
        "var FIGS = %s;" % json.dumps(figs, ensure_ascii=False),
        "process.stdout.write(JSON.stringify(renderFigures(FIGS)));",
    ])
    out = subprocess.run([_node(), "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr[-500:]
    return json.loads(out.stdout)


FIG = {
    "kind": "flow",
    "caption": "循环的四个阶段",
    "reading": "少任何一步循环都闭合不了",
    "alt": "四个方框按顺序排列",
    "svg": '<svg viewBox="0 0 640 120"><rect width="10" height="10"/></svg>',
}


# ---------- 正常路径 ----------

def test_renders_one_figure_completely():
    html = _run_render([FIG])
    assert "看图理解" in html
    assert FIG["svg"] in html, "SVG 要被原样注入"
    assert "循环的四个阶段" in html
    # 读图结论是图存在的理由，不能只出图
    assert "少任何一步循环都闭合不了" in html
    assert 'aria-label="四个方框按顺序排列"' in html


def test_multiple_figures_all_render():
    a = dict(FIG, caption="第一张")
    b = dict(FIG, caption="第二张")
    html = _run_render([a, b])
    assert html.count("fig-canvas") == 2
    assert "第一张" in html and "第二张" in html


def test_caption_falls_back_to_alt_for_aria_label():
    f = dict(FIG)
    f.pop("alt")
    assert 'aria-label="循环的四个阶段"' in _run_render([f])


# ---------- 拒绝路径（前端不能假设后端永远正确）----------

def test_no_figures_is_empty_string():
    assert _run_render([]) == ""
    assert _run_render(None) == ""


def test_svg_not_starting_with_svg_tag_is_rejected():
    """普通文本不是标签，不能当 HTML 注入。"""
    bad = dict(FIG, svg="<div>hello</div>")
    assert _run_render([bad]) == ""


def test_svg_with_script_is_rejected():
    bad = dict(FIG, svg='<svg><script>alert(1)</script></svg>')
    assert _run_render([bad]) == ""


def test_svg_with_event_handler_is_rejected():
    bad = dict(FIG, svg='<svg onload="alert(1)"></svg>')
    assert _run_render([bad]) == ""


def test_bad_figure_does_not_take_down_the_good_one():
    """一张坏图不能让整块消失——这正是「图不见了」最难查的情形。"""
    html = _run_render([dict(FIG, svg="<div>x</div>"), FIG])
    assert html.count("fig-canvas") == 1
    assert FIG["svg"] in html


def test_missing_svg_field_is_skipped():
    assert _run_render([dict(FIG, svg=None)]) == ""


# ---------- 转义 ----------

def test_caption_and_reading_are_escaped():
    """图注来自模型，必须转义，否则能往页面注入标签。"""
    f = dict(FIG, caption='<img src=x onerror=alert(1)>', reading="a & b")
    html = _run_render([f])
    assert "<img" not in html
    assert "&lt;img" in html
    assert "a &amp; b" in html


def test_caption_markup_is_not_treated_as_html():
    f = dict(FIG, caption="**不是加粗**")
    assert "**不是加粗**" in _run_render([f])


# ---------- 复习流程：答案不能回到前端 ----------
#
# 这一组守的是 M4 的核心决定：判卷在服务端做。此前每道题的 answer 随卡片
# 下发、由前端比对，打开开发者工具就能看到答案——"复习"就不成立了。
# 一旦有人把本地判卷改回来，下面这些用例会红。

def _strip_js_comments(text):
    """去掉整行 `//` 注释。

    守卫要看的是**代码**而不是散文：注释里提到 "c.quiz" 是为了说明
    "以前是这么写的"，那不是要拦的东西。不剥注释就会把说明文案当成违规。
    """
    return "\n".join(l for l in text.split("\n") if not l.strip().startswith("//"))


def _review_region():
    """截出复习流程的 JS 段，并剥掉注释。"""
    src = open(READER, encoding="utf-8").read()
    start = src.index("// ---------- 复习 ----------")
    end = src.index("function skipCurrentReview()")
    return _strip_js_comments(src[start:end])


def test_review_flow_does_not_grade_on_the_client():
    region = _review_region()
    for bad in ("data-correct", "q.answer", "correctIdx", "=== st.quiz[st.index].answer"):
        assert bad not in region, "复习流程又出现了本地判卷痕迹: %s" % bad


def test_review_flow_calls_the_grading_endpoint():
    region = _review_region()
    assert "/api/question/answer" in region
    assert "data-orig" in region, "选项必须带原始下标，否则服务端无法判卷"


def test_review_flow_reads_the_sealed_question_bank():
    region = _review_region()
    assert "c.questions" in region, "题目应来自服务端密封题库"
    assert "review_quiz" not in region and "c.quiz" not in region, \
        "不该再从原始字段取题（那里带答案）"


def test_review_flow_shows_error_reason():
    """方案 M4 要求用户看得到错在哪，而不只是正确答案。"""
    region = _review_region()
    assert "error_reason" in region
    assert "review-error-reason" in region


def test_review_flow_reports_card_result_once():
    """SM-2 量的是「这张卡还记不记得」，逐题推进会让一次复习记成多次间隔跳跃。"""
    region = _review_region()
    assert region.count("/api/review/finish") == 1


def test_review_flow_does_not_fake_success_on_grading_failure():
    """判卷失败必须让用户重试，不能默默当答对——那会污染掌握度。"""
    region = _review_region()
    assert "判卷失败" in region
    assert "st.answered = false" in region


def test_layer_labels_and_score_format():
    src = open(READER, encoding="utf-8").read()
    js = "\n".join([
        _extract(src, "layerLabel"),
        _extract(src, "fmtScore"),
        "process.stdout.write(JSON.stringify(["
        "layerLabel('immediate'), layerLabel('day1'), layerLabel('day7'),"
        "layerLabel('其他'), fmtScore(0.4), fmtScore(0.4567), fmtScore(-0.12), fmtScore(null)]));",
    ])
    out = subprocess.run([_node(), "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr[-400:]
    assert json.loads(out.stdout) == [
        "当日巩固", "24 小时回忆", "一周后迁移", "其他",
        "0.40", "0.46", "-0.12", "0.00",
    ]


# ---------- CP21：三处自测都必须走服务端判卷 ----------
#
# 复习流程已改好之后，卡片详情自测与计划任务自测还留着 `data-correct`
# 本地判卷——同一个泄露点换了个地方。这组用例把三处一起钉住。

def _script():
    """整段 <script>（含注释，因为下面要按行判断）。"""
    src = open(READER, encoding="utf-8").read()
    return "\n;\n".join(re.findall(r"<script[^>]*>(.*?)</script>", src, re.S))


def _code_lines():
    """去掉整行注释的 script 行——守卫要看代码，不看说明文案。"""
    return [l for l in _script().split("\n") if not l.strip().startswith("//")]


def test_no_client_side_grading_left_anywhere():
    code = "\n".join(_code_lines())
    for bad, why in (("data-correct", "答案被写进 DOM 属性"),
                     ("data-opt", "旧的本地判卷选项属性"),
                     ("c.quiz", "从原始题库取题（那里带答案）")):
        assert bad not in code, "全站仍有本地判卷痕迹 %s（%s）" % (bad, why)


def test_all_three_flows_call_the_grading_endpoint():
    """复习、卡片详情、计划任务——三处都要打同一个判卷端点。"""
    code = "\n".join(_code_lines())
    assert code.count("/api/question/answer") == 1, "判卷端点应只在一处封装"
    for fn in ("renderReviewQuiz", "chooseAnswer", "renderTaskQuiz"):
        assert "function %s(" % fn in code
    # 三处作答都经由 gradeQuestion
    assert code.count("gradeQuestion(") >= 4, "三处作答 + 定义都应走 gradeQuestion"


def test_reference_answer_never_travels_to_the_client():
    code = "\n".join(_code_lines())
    assert "reference_answer:" not in code, "前端在上传参考答案"
    assert "oq.reference_answer" not in code and "toq.reference_answer" not in code, \
        "前端仍直接读参考答案"
    # 但判分后要展示服务端回传的参考答案
    assert code.count("data.reference_answer") >= 2, "判分后应展示服务端回传的参考答案"


def test_grading_reports_question_index_not_client_answer():
    """判卷靠 (卡号, 题号, 选项号)，前端不参与对错判断。"""
    code = "\n".join(_code_lines())
    assert "data-qidx" in code and "data-orig" in code
    assert "correct_index" in code, "高亮应使用服务端返回的正确下标"


def _statement_at(code, i):
    """从 i 起取到当前语句的 `;`（跳过字符串与括号内的分号）。

    必须按语句边界取：按固定字符数取窗口会溢到后面的代码里，
    于是后续函数体里的 `data.` 被误判成"读了响应体"，守卫就会误报。
    """
    depth, quote, j = 0, None, i
    while j < len(code):
        ch = code[j]
        if quote:
            if ch == "\\":
                j += 2
                continue
            if ch == quote:
                quote = None
        elif ch in "\"'`":
            quote = ch
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif ch == ";" and depth == 0:
            return code[i:j + 1]
        j += 1
    return code[i:i + 400]


def test_postjson_calls_that_read_the_body_parse_json():
    """`postJSON` 返回 Response，不是数据。直接读 res.success 永远是 undefined，
    而且不报错——只会表现成"功能默默失效"。这个 bug 真上线过。"""
    code = "\n".join(_code_lines())
    offenders = []
    for m in re.finditer(r"postJSON\('", code):
        stmt = _statement_at(code, m.start())
        reads_body = ("res." in stmt) or ("data." in stmt)
        if reads_body and ".json()" not in stmt:
            offenders.append(stmt[:140].replace("\n", " "))
    assert not offenders, ("这些 postJSON 读了响应体但没解析 JSON"
                           "（应改用 postJSONData）：\n" + "\n".join(offenders))


def test_postjsondata_helper_exists_and_parses():
    code = "\n".join(_code_lines())
    assert "function postJSONData(" in code
    assert "return postJSON(url, body).then(function (r) { return r.json(); });" in code
    # 判卷与整卡提交都必须用它（这两处要读响应体）
    assert "postJSONData('/api/question/answer'" in code
    assert "postJSONData('/api/review/finish'" in code


# ---------- CP22：按内容块渲染，不按类型分支 ----------
#
# 范围决策：六种固定卡片类型不是六种学习目标，只是内容表现形式。系统应按
# 学习目标自动组合内容块。所以渲染层不再看 `_meta.template`，
# 只看"这张卡里有哪些块"。下面第一组用例钉住"每张老类型的卡照样能渲染"，
# 第二组钉住"混着来也能渲染"——后者是类型分家做不到的。

def _render_cards(cards):
    """用 reader.html 里的真实渲染函数渲染一批卡片，返回 {名字: html}。"""
    src = open(READER, encoding="utf-8").read()
    blocks = src[src.index("  var CARD_BLOCKS = ["):src.index("  function _section(")]
    list_kinds = src[src.index("  var LIST_KINDS = "):src.index("  function renderCardContent(")]
    fns = [_extract(src, n) for n in
           ("escapeHtml", "renderBody", "renderFigures", "layerLabel",
            "_section", "_numbered", "renderCardBlock", "renderCardContent")]
    js = "\n".join([blocks, list_kinds] + fns + [
        "var CARDS = %s;" % json.dumps(cards, ensure_ascii=False),
        "var out = {};",
        "Object.keys(CARDS).forEach(function (k) { out[k] = renderCardContent(CARDS[k]); });",
        "process.stdout.write(JSON.stringify(out));",
    ])
    out = subprocess.run([_node(), "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr[-600:]
    return json.loads(out.stdout)


def _titles(html):
    return re.findall(r'class="section-title">([^<]+)<', html)


def test_every_legacy_card_type_still_renders_by_its_blocks():
    """六种老类型都不再靠 template 分支，但各自的块必须照旧渲染出来。"""
    out = _render_cards({
        "vocab": {"word": "ephemeral", "phonetic": "/ɪˈfemərəl/", "pos_label": "adj.",
                  "definition_cn": "短暂的", "etymology": "来自希腊语",
                  "examples": [{"en": "a", "cn": "甲"}], "common_mistake": "别混"},
        "words": {"words": [{"word": "a", "definition_cn": "甲",
                             "example": {"en": "e", "cn": "c"}}]},
        "math": {"intuition": "想象一个斜面", "example": "f(x)=x^2", "try_it": "自己推"},
        "trivia": {"hook": "你知道吗", "body": "正文", "fun_facts": ["f1"],
                   "share_line": "分享一句"},
        "skill": {"steps": [{"step": "准备", "detail": "先收集"}],
                  "bad_example": "别跳过", "good_example": "先写测试",
                  "tip": "小步提交", "try_it": "今天就试"},
        "code": {"summary": "一句话", "intuition": "直觉", "pseudocode": "if x: y()",
                 "key_steps": ["一", "二"], "why": "为了性能", "example": "走一遍",
                 "pitfalls": "注意越界"},
    })
    assert _titles(out["vocab"])[0] == "词汇详解", "词汇卡应先把单词摆出来"
    assert "例句" in _titles(out["vocab"]) and "易错点" in _titles(out["vocab"])
    assert "词库 · 1 词" in "".join(_titles(out["words"]))
    assert _titles(out["math"]) == ["直觉理解", "走一遍例子", "今天就试"]
    assert "你知道吗" in _titles(out["trivia"]) and "有趣细节" in _titles(out["trivia"])
    assert _titles(out["skill"]) == ["步骤", "别这么做", "应该这么做", "进阶技巧", "今天就试"]
    assert set(_titles(out["code"])) == {"一句话核心", "直觉理解", "核心逻辑（伪代码）",
                                         "走一遍例子", "关键步骤", "为什么这么设计", "常见坑"}


def test_one_card_can_carry_concept_math_and_code_at_once():
    """这是类型分家做不到的能力：一张卡同时含概念、数学与代码，必须全渲染。"""
    out = _render_cards({"mixed": {
        "summary": "概念", "word": "w", "definition_cn": "d", "intuition": "直觉",
        "body": "正文", "pseudocode": "code()",
        "steps": [{"step": "s", "detail": "d"}], "try_it": "动手"}})
    titles = _titles(out["mixed"])
    for must in ("一句话核心", "直觉理解", "正文精读", "核心逻辑（伪代码）", "步骤", "今天就试"):
        assert must in titles, "混合卡缺少 %s，实际 %s" % (must, titles)
    assert out["mixed"].count('class="section"') >= 6


def test_empty_blocks_do_not_produce_empty_sections():
    """空数组不该渲染出空标题——那会让卡片看起来像坏了。"""
    out = _render_cards({"sparse": {
        "body": "正文", "examples": [], "steps": [], "key_steps": [],
        "fun_facts": [], "words": [], "core_points": []}})
    assert _titles(out["sparse"]) == ["正文精读"]
    assert "核心观点" not in out["sparse"]


def test_card_with_only_a_title_renders_nothing():
    out = _render_cards({"t": {"title": "只有标题"}})
    assert out["t"] == ""


def test_renderer_does_not_branch_on_template():
    """渲染层不该再出现 `_meta.template` / tpl 分支——这是本次决策的落点。"""
    src = open(READER, encoding="utf-8").read()
    start = src.index("  var CARD_BLOCKS = [")
    end = src.index("  // ---------- 数据加载 ----------")
    region = src[start:end]
    assert "tpl === '" not in region
    assert "_meta.template" not in region
    assert "CARD_BLOCKS" in region and "renderCardBlock" in region


def test_type_picker_is_gone_from_the_ui():
    """六种类型选择器已从界面下线（范围决策）。"""
    src = open(READER, encoding="utf-8").read()
    for gone in ("templateRow", "planTemplateRow", "data-tpl=", "planAutoMode",
                 "markPlanTplActive", "createTemplate"):
        assert gone not in src, "界面里还有类型选择残留: %s" % gone
    # 规模与节奏是用户真正该决定的维度，必须留着
    assert "planScaleRow" in src and "planPaceRow" in src


def test_blocks_all_have_a_renderer():
    """CARD_BLOCKS 里每个 kind 都要有分支，否则那个块会被静默丢掉。"""
    src = open(READER, encoding="utf-8").read()
    fn = _extract(src, "renderCardBlock")
    kinds = set(re.findall(r"kind: '(\w+)'", src))
    for k in kinds:
        assert "kind === '%s'" % k in fn, "renderCardBlock 缺少 kind=%s 的分支" % k


# ---------- CP23：连续打卡不再作核心指标 ----------

def test_streak_is_not_shown_anywhere():
    """范围决策：连击不再作核心指标——用连续天数施压会把学习变成打卡。

    只看代码不看注释：注释里写"连击已下线"正是我们应该留下的说明。
    """
    code = "\n".join(_code_lines())
    for gone in ("streakNum", "ws.streak", "连击"):
        assert gone not in code, "界面还有连击痕迹: %s" % gone


def test_header_shows_cumulative_study_days():
    src = open(READER, encoding="utf-8").read()
    assert 'id="studyDays"' in src
    assert "累计学习" in src
    # 累计天数 = 有记录的天数，不是连续天数
    code = "\n".join(_code_lines())
    assert "$('studyDays').textContent" in code


def test_stats_page_shows_block_distribution_not_templates():
    """统计页也不能再暴露"类型"——换成内容块分布。"""
    code = "\n".join(_code_lines())
    assert "block_dist" in code
    assert "template_dist" not in code
    assert "内容块分布" in code
    # 标题只从 CARD_BLOCKS 取，找不到就退回字段名
    fn = _extract(open(READER, encoding="utf-8").read(), "blockTitle")
    assert "CARD_BLOCKS" in fn


def test_no_notification_type_hardcoded_in_frontend():
    """前端不该自己列通知类型——白名单在服务端一处。"""
    code = "\n".join(_code_lines())
    for t in ("daily_summary", "daily_picks", "streak_warn", "stale_warn",
              "weak_review", "action_log", "weekly_report"):
        assert t not in code, "前端还在引用已停发的通知类型: %s" % t


# ---------- 预览器：抽函数清单必须跟得上渲染层 ----------

def test_preview_renderer_has_everything_it_needs():
    """预览器靠「从 reader.html 抽函数」工作，抽漏一个就白屏。

    CP22 把渲染改成内容块驱动之后它漏了 `CARD_BLOCKS`，直接 ReferenceError。
    这个用例拿一张**混装各种块**的卡跑一遍，确保清单是完整的。
    """
    from tools import preview_v1_card as P

    card = {
        "title": "混装卡", "summary": "一句话", "word": "w", "definition_cn": "释义",
        "hooks": None, "hook": "你知道吗", "intuition": "直觉", "body": "第一段\n\n第二段",
        "pseudocode": "if x: y()", "examples": [{"en": "a", "cn": "甲"}],
        "example": "走一遍", "steps": [{"step": "s", "detail": "d"}],
        "key_steps": ["一"], "why": "为了性能", "bad_example": "别这样",
        "good_example": "要这样", "tip": "技巧", "common_mistake": "易错",
        "pitfalls": "坑", "fun_facts": ["f1"], "share_line": "分享",
        "try_it": "动手", "etymology": "词源",
        "core_points": ["点1"],
        "figures": [{"svg": '<svg viewBox="0 0 640 10"></svg>', "caption": "图注",
                     "reading": "读图结论"}],
        "think_question": "迁移任务", "think_answer": "参考答案",
    }
    html = P.render_card_html(card)
    for must in ("一句话核心", "词汇详解", "你知道吗", "正文精读", "核心逻辑（伪代码）",
                 "步骤", "别这么做", "核心观点", "看图理解", "想一想"):
        assert must in html, "预览漏渲染了 %s" % must
    assert "<svg" in html


# ---------- 发现层渲染 ----------

def _run_discover(data):
    """在 node 里对 data 调一次 renderDiscover，返回 HTML 字符串。"""
    src = open(READER, encoding="utf-8").read()
    js = "\n".join([
        _extract(src, "escapeHtml"),
        _extract(src, "renderDiscover"),
        "var D = %s;" % json.dumps(data, ensure_ascii=False),
        "process.stdout.write(JSON.stringify(renderDiscover(D)));",
    ])
    out = subprocess.run([_node(), "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr[-500:]
    return json.loads(out.stdout)


def test_discover_renders_picks_with_reason():
    html = _run_discover({"picks": [{"source_url": "https://x/1", "title": "第一篇",
                                     "why": "讲清了机制为什么这样设计"}], "lab": []})
    assert "第一篇" in html
    assert "讲清了机制为什么这样设计" in html, "只给标题不给理由，荐读就退化成一条链接"
    assert 'data-url="https://x/1"' in html


def test_discover_shows_v2_lab_with_tags():
    """v2 还没进推送，发现层是它唯一的入口——配图数必须露出来，
    否则评审在点开之前看不出这张卡到底有没有图。"""
    html = _run_discover({"picks": [], "lab": [{
        "source_url": "v2:a", "title": "实验卡", "summary": "摘要",
        "date": "2026-09-11", "figures": 3, "questions": 4}]})
    assert "实验卡" in html
    assert "配图 3" in html and "题 4" in html and "2026-09-11" in html


def test_discover_empty_states_are_explicit():
    """空状态要给一句话，不能是一片空白——空白会被读成「坏了」。"""
    html = _run_discover({})
    assert "今天没有单独值得推荐的卡" in html
    assert "还没有 v2 产出的卡" in html


def test_discover_labels_rule_fallback_differently():
    """「Agent 挑的」和「今天有什么」是两种可信度，标题不能混。"""
    html = _run_discover({"picks": [{"source_url": "u", "title": "t", "why": "w",
                                     "from": "today"}], "lab": []})
    assert "今天的新卡" in html
    assert "为你挑的" not in html

    html2 = _run_discover({"picks": [{"source_url": "u", "title": "t", "why": "w",
                                      "from": "agent"}], "lab": []})
    assert "为你挑的" in html2


def test_discover_escapes_text():
    html = _run_discover({
        "picks": [{"source_url": "https://x/1", "title": "<script>alert(1)</script>",
                   "why": "w"}],
        "lab": [{"source_url": "v2:a", "title": "<b>粗</b>", "summary": "s"}]})
    assert "<script>" not in html
    assert "&lt;script&gt;" in html

