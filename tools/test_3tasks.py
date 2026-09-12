from weizhi.serve import reader
api_key = reader.load_api_key()

print("=== 任务1：T2 精读 · ReAct 范式 ===", flush=True)
o = reader.generate_outline(api_key, "ReAct 范式：让 LLM 边推理边行动", "t2_reading", 5)
print("大纲:", [x["title"] for x in o.get("outline", [])], flush=True)
r = reader.create_plan(api_key, o.get("title"), "ReAct 范式", o.get("outline", []), template="t2_reading", scale=5, pace=3)
print("计划:", (r.get("plan") or {}).get("title"), "| 卡数:", r.get("cards_generated"), flush=True)

print("=== 任务2：T3 数学 · 奇异值分解 ===", flush=True)
o = reader.generate_outline(api_key, "奇异值分解（SVD）：矩阵的乐高拆解", "t3_math", 5)
print("大纲:", [x["title"] for x in o.get("outline", [])], flush=True)
r = reader.create_plan(api_key, o.get("title"), "奇异值分解", o.get("outline", []), template="t3_math", scale=5, pace=3)
print("计划:", (r.get("plan") or {}).get("title"), "| 卡数:", r.get("cards_generated"), flush=True)

print("=== 任务3：T6 代码 · 自注意力机制 ===", flush=True)
o = reader.generate_outline(api_key, "自注意力机制（Attention）", "t6_code", 5)
print("大纲:", [x["title"] for x in o.get("outline", [])], flush=True)
r = reader.create_plan(api_key, o.get("title"), "自注意力机制", o.get("outline", []), template="t6_code", scale=5, pace=3)
print("计划:", (r.get("plan") or {}).get("title"), "| 卡数:", r.get("cards_generated"), flush=True)

print("=== 全部完成 ===", flush=True)
