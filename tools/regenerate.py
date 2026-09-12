# -*- coding: utf-8 -*-
import reader, db

api_key = reader.load_api_key()

# 1. 清空旧卡
conn = db._conn()
try:
    for tbl in ("reviews", "cards", "plans"):
        try:
            conn.execute("DELETE FROM %s" % tbl)
        except Exception:
            pass
    conn.commit()
finally:
    conn.close()
print("清空完成", flush=True)

# 2. T1 词汇：AI 高频术语 20 词
print("=== T1 词汇 ===", flush=True)
o = reader.generate_outline(api_key, "AI 高频术语（10个技术类如 hallucination/inference/RAG/RLHF/fine-tuning + 10个产品类如 PMF/retention/churn/GTM/north star metric）", "t1_vocab", 20)
r = reader.create_plan(api_key, o.get("title"), "AI 高频术语", o.get("outline", []), template="t1_vocab", scale=20, pace=3)
print("T1:", (r.get("plan") or {}).get("title"), "| 卡数:", r.get("cards_generated"), flush=True)

# 3. T2 精读：LLM 原理论文 5 篇
print("=== T2 精读 ===", flush=True)
o = reader.generate_outline(api_key, "LLM 原理论文（Attention is All You Need、Scaling Laws、RLHF-InstructGPT、RAG、Agent 架构）", "t2_reading", 5)
r = reader.create_plan(api_key, o.get("title"), "LLM 原理论文", o.get("outline", []), template="t2_reading", scale=5, pace=3)
print("T2:", (r.get("plan") or {}).get("title"), "| 卡数:", r.get("cards_generated"), flush=True)

# 4. T3 数学：ML 数学底座 5 概念
print("=== T3 数学 ===", flush=True)
o = reader.generate_outline(api_key, "ML 数学底座（贝叶斯定理、KL 散度、SVD 奇异值分解、梯度下降、交叉熵）", "t3_math", 5)
r = reader.create_plan(api_key, o.get("title"), "ML 数学底座", o.get("outline", []), template="t3_math", scale=5, pace=3)
print("T3:", (r.get("plan") or {}).get("title"), "| 卡数:", r.get("cards_generated"), flush=True)

# 5. T5 技能：AI PM 实操 5 模块
print("=== T5 技能 ===", flush=True)
o = reader.generate_outline(api_key, "AI 产品经理实操（写 AI 产品的 PRD、做竞品分析、Badcase 归因、RAG 搭建、评测体系设计）", "t5_skill", 5)
r = reader.create_plan(api_key, o.get("title"), "AI 产品经理实操", o.get("outline", []), template="t5_skill", scale=5, pace=3)
print("T5:", (r.get("plan") or {}).get("title"), "| 卡数:", r.get("cards_generated"), flush=True)

# 6. T4 通识：5 张散卡
print("=== T4 通识 ===", flush=True)
for topic in ["图灵测试", "深度学习三巨头 LeCun/Bengio/Hinton", "Transformer 诞生背后的故事", "ChatGPT 爆发始末", "AI 芯片之争 GPU vs TPU"]:
    r = reader.create_card(api_key, topic=topic, template="t4_trivia")
    c = r.get("card") or {}
    print("  -", c.get("title") or ("失败: " + str(r.get("error"))), flush=True)

print("=== 全部完成 ===", flush=True)
