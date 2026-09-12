from weizhi.serve import reader
api_key = reader.load_api_key()
print("=== T6 代码/算法理解：LLM 核心算法机制 ===", flush=True)
o = reader.generate_outline(api_key, "LLM 核心算法机制（自注意力、KV Cache、温度采样、MoE 路由、Agent ReAct 循环）", "t6_code", 5)
print("大纲:", [x["title"] for x in o.get("outline", [])], flush=True)
r = reader.create_plan(api_key, o.get("title"), "LLM 核心算法机制", o.get("outline", []), template="t6_code", scale=5, pace=3)
print("计划:", (r.get("plan") or {}).get("title"), "| 卡数:", r.get("cards_generated"), flush=True)
