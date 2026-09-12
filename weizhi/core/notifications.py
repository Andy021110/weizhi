# -*- coding: utf-8 -*-
"""通知策略：只有三类通知允许出现在用户面前。

范围决策原文——通知从 8 种大幅精简，只保留三类：

1. **到期复习**（`review_due`）
2. **需要用户审核**（`badcase_pending`）
3. **系统任务失败且无法自动恢复**（`system_failure`）

「荐食提醒、断签焦虑、每日生成报告、普通成功通知可以取消或默认关闭。」

**为什么做成一个模块，而不是把那几处调用删掉**：通知是唯一会主动打扰用户的
东西，而"顺手也提醒一下"是很容易随手加回来的。把「什么能发」收在一处、
并且让**被停发的类型在代码里留下名字和理由**，比散落各处的删除更不容易回潮。

停发的类型也仍然会被 `emit` 拦下——包括模型自己生成的类型（LLM 有时会
自作主张编一个 type 出来）。
"""
from weizhi.core import db

# 允许的通知类型。key 是 type 字段，value 是「它为什么值得打扰用户」
KEEP = {
    "review_due": "到期复习：不提醒的话，前面投入的时间就白花了",
    "badcase_pending": "需要你审核：模型生成的内容不能自动发布",
    "system_failure": "系统任务失败且无法自动恢复：只有这种情况才需要人介入",
}

# 停发的类型。**保留名字与理由**——它们不是"忘了"，是决定不发。
STOPPED = {
    "daily_summary": "每日生成报告：每天一条总结，读的人早就免疫了",
    "daily_picks": "荐读推送：内容发现应该是用户主动去逛，不是被推送",
    "streak_warn": "断签焦虑：用连续天数施压，会把学习变成打卡",
    "stale_warn": "过时卡提醒：属于内容质量问题，交给质检流程而不是通知",
    "weak_review": "薄弱卡提醒：与「到期复习」重复，一个入口就够",
    "action_log": "普通成功通知：成功了不用报喜，报喜本身是噪音",
    "weekly_report": "周报：同「每日生成报告」",
}

LEVELS = ("info", "warn", "action")

# 被拦下的通知计数（便于测试与排查：「这条为什么没发」）
_suppressed = {}


def allowed(ntype):
    """这个类型能不能发。"""
    return ntype in KEEP


def reason_stopped(ntype):
    """被停发的理由；未知类型给出明确说明。"""
    if ntype in STOPPED:
        return STOPPED[ntype]
    return "不在允许的三类里（%s）" % "、".join(sorted(KEEP))


def emit(ntype, title, body, level="info", date=None):
    """发一条通知。**这是唯一的出口**，返回是否真的发了。

    被拦下时计入 `suppressed()`，不抛异常——通知发不出去不该让整个任务失败。
    """
    if not allowed(ntype):
        _suppressed[ntype] = _suppressed.get(ntype, 0) + 1
        return False
    db.add_notification(ntype, title, body,
                        level=level if level in LEVELS else "info", date=date)
    return True


def failure(stage, error, hint=""):
    """报一次「系统任务失败且无法自动恢复」。

    只在**真的没法自愈**时调用：能重试、能降级、能自动修复的都不算。
    否则这条通知会变成另一种噪音，而这个类型存在的唯一理由就是"稀有"。
    """
    text = str(error or "").strip().splitlines()
    detail = text[-1][:200] if text else "（无错误信息）"
    body = "%s 失败：%s" % (stage, detail)
    if hint:
        body += "。" + hint
    return emit("system_failure", "系统任务失败：%s" % stage, body, level="action")


def suppressed():
    """被拦下的通知类型与次数。"""
    return dict(_suppressed)


def reset_suppressed():
    _suppressed.clear()


def review_due(due, limit_note=""):
    """到期复习通知（保留的三类之一）。"""
    return emit("review_due", "今天有 %d 张复习到期" % due,
                "到期未复习。打开页面即可开始。" + limit_note, level="warn")


def pending_review(count, names):
    """需要你审核（保留的三类之一）。"""
    return emit("badcase_pending", "%d 张卡待你拍板" % count,
                names + "。点「📋 质检」查看评分与理由，决定重生成或保留。",
                level="action")
