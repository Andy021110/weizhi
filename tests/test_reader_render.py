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
READER = os.path.join(ROOT, "reader.html")

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
