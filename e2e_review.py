# -*- coding: utf-8 -*-
"""CP20 复习流程的真实 HTTP 端到端验证（本地起服务，打真实接口）。"""
import json
import os
import sys
import urllib.request

# 本地直连：清掉代理变量，否则请求会被代理接走（表现为 502 / 空响应）
for k in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy",
          "ALL_PROXY", "all_proxy"):
    os.environ.pop(k, None)
os.environ["NO_PROXY"] = "*"
os.environ["no_proxy"] = "*"

BASE = "http://127.0.0.1:%s" % os.environ.get("PORT", "8791")
KEY = "https://example.com/h#v2:e2e"

_ROOT = os.path.dirname(os.path.abspath(__file__))
TOKEN = json.load(open(os.path.join(_ROOT, "config.json"),
                       encoding="utf-8")).get("access_token") or ""
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

FAILS = []


def check(label, ok, detail=""):
    print("  %s %s%s" % ("✅" if ok else "❌", label, ("  " + detail) if detail else ""))
    if not ok:
        FAILS.append(label)


def req(path, data=None):
    body = json.dumps(data, ensure_ascii=False).encode() if data is not None else None
    r = urllib.request.Request(BASE + path, data=body,
                               headers={"X-Auth-Token": TOKEN,
                                        "Content-Type": "application/json"})
    with OPENER.open(r, timeout=30) as resp:
        raw = resp.read().decode("utf-8")
    return json.loads(raw) if raw.strip() else None


def main():
    sys.path.insert(0, _ROOT)
    import db

    print("=== 1. 复习队列：答案不能下发 ===")
    q = req("/api/review/queue")
    blob = json.dumps(q, ensure_ascii=False)
    cards = q.get("reviews") or []
    check("队列有卡", len(cards) == 1, "共 %d 张" % len(cards))
    if not cards:
        return 1
    c = cards[0]
    print("     题面: %s" % [x["question"] for x in c["questions"]])
    print("     分层: %s | 层数说明: %s"
          % (c["layers"], [x["layer"] for x in c["questions"]]))
    check("题目已密封（无 answer 字段）", '"answer"' not in blob)
    check("无解析", "解析" not in blob)
    check("无错误原因", "把循环和流水线混为一谈" not in blob and "漏掉了边界条件" not in blob)
    check("无原始题库字段", "review_quiz" not in blob)
    check("无简答题参考答案", "open_question" not in blob)
    check("只考 day1/day7 层", c["layers"] == {"day1": 1, "day7": 1})

    print()
    print("=== 2. 服务端判卷：答错 ===")
    fb = req("/api/question/answer",
             {"card_key": KEY, "index": 1, "chosen": 1, "elapsed_ms": 2500})["feedback"]
    print("     正确=%s 正确选项=%s 层=%s" % (fb["correct"], fb["correct_option"], fb["layer"]))
    print("     错误原因: %s" % fb["error_reason"])
    print("     掌握度: %s（累计 %s/%s）"
          % (fb["mastery"]["score"], fb["mastery"]["correct"], fb["mastery"]["attempts"]))
    check("答错返回错误原因", bool(fb["error_reason"]))
    check("答错回流掌握度", fb["mastery"]["attempts"] == 1)

    print()
    print("=== 3. 服务端判卷：答对 ===")
    fb2 = req("/api/question/answer",
              {"card_key": KEY, "index": 2, "chosen": 1, "elapsed_ms": 9000})["feedback"]
    print("     正确=%s 错误原因=%r（答对应为 None）" % (fb2["correct"], fb2["error_reason"]))
    print("     掌握度: %s（累计 %s/%s）"
          % (fb2["mastery"]["score"], fb2["mastery"]["correct"], fb2["mastery"]["attempts"]))
    check("答对不显示错误原因", fb2["error_reason"] is None)
    check("答对后掌握度上升", fb2["mastery"]["score"] > fb["mastery"]["score"])

    print()
    print("=== 4. 异常输入要挡住 ===")
    for payload, label, expect in (
            ({"card_key": "不存在", "index": 0, "chosen": 0}, "未知卡", "不存在"),
            ({"card_key": KEY, "index": 99, "chosen": 0}, "题号越界", "越界"),
            ({"card_key": KEY, "index": 1, "chosen": 99}, "选项越界", "越界")):
        r = req("/api/question/answer", payload)
        check(label, r.get("success") is False and expect in (r.get("error") or ""),
              str(r.get("error")))

    print()
    print("=== 5. 单卡完成 → 推进 SM-2 ===")
    res = req("/api/review/finish", {"card_key": KEY, "correct": 2, "total": 2})["result"]
    print("     第 1 次通过=%s 间隔=%s天 下次=%s"
          % (res["passed"], res["state"]["interval_days"], res["state"]["next_review_at"]))
    # SM-2 首轮通过就是 1 天，这不是 bug——要看的是**再通过一次间隔会不会拉长**
    check("答对判定为通过", res["passed"] is True)
    res2 = req("/api/review/finish", {"card_key": KEY, "correct": 2, "total": 2})["result"]
    print("     第 2 次通过=%s 间隔=%s天 下次=%s"
          % (res2["passed"], res2["state"]["interval_days"], res2["state"]["next_review_at"]))
    check("再通过一次间隔拉长", res2["state"]["interval_days"] > res["state"]["interval_days"],
          "%s → %s 天" % (res["state"]["interval_days"], res2["state"]["interval_days"]))
    bad = req("/api/review/finish", {"card_key": KEY, "correct": 5, "total": 2})
    check("越界提交被拒", bad.get("success") is False and "0..total" in (bad.get("error") or ""))

    print()
    print("=== 6. 落库复查 ===")
    got = db.get_card(KEY)
    print("     review_count=%s memory_state=%s next_review_at=%s"
          % (got["review_count"], got["memory_state"], got["next_review_at"]))
    logs = db.list_v2_reviews(goal_key="g-e2e")
    print("     v2 逐题记录 %d 条: %s"
          % (len(logs), [(l["question_idx"], l["correct"], l["confidence"]) for l in logs]))
    m = db.get_v2_mastery("g-e2e", "m1") or {}
    print("     v2 掌握度: score=%s attempts=%s correct=%s interval=%s"
          % (round(m.get("score") or 0, 3), m.get("attempts"),
             m.get("correct"), m.get("interval_days")))
    check("SM-2 已推进", got["review_count"] == 2)
    check("逐题记录已写入 v2", len(logs) == 2)
    check("掌握度按里程碑归因", (m.get("attempts") or 0) == 2)

    print()
    print("=== 7. 今日概览 ===")
    daily = req("/api/review/daily")
    print("     %s" % json.dumps({k: v for k, v in daily.items() if k != "due"},
                                 ensure_ascii=False))
    check("概览接口可用", "due_count" in daily)

    print()
    if FAILS:
        print("❌ 失败 %d 项: %s" % (len(FAILS), FAILS))
        return 1
    print("✅ 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
