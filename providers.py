# -*- coding: utf-8 -*-
"""微知 v2 · 文本模型 Provider 抽象（CP2）。

解决的问题：v1 在 4 个文件里裸调了 11 次 `OpenAI(...)`，没有缓存、没有幂等、
没有统一的重试和调用留痕。方案 4.3 要求文本与图片模型都不得散落在业务代码里。

本模块把「重试 / JSON 解析 / Schema 校验 / 幂等缓存 / 调用审计」收在基类里，
子类只负责发一次请求。收益是任何调用都可追溯、可重放、可替换：
把 DeepSeekProvider 换成 FakeTextProvider，整条链路就能离线跑通。

关于费用：`PRICING` 默认为空，此时 cost 记 NULL。**宁可留空也不猜单价**——
猜出来的成本数字比没有更危险。需要成本口径时按当前官方定价填 PRICING 即可。

用法::

    from providers import FakeTextProvider
    p = FakeTextProvider(responder=lambda task, inputs: {"ok": True})
    data = p.generate_json("demo", schema=None, inputs={"x": 1})
"""
import hashlib
import json
import time

import db

DEEPSEEK_BASE_URL = "https://api.deepseek.com"

# 每百万 token 单价，键为 model 名。留空则 cost 记 NULL，不猜价格。
PRICING = {
    # "deepseek-chat": {"in": 2.0, "out": 8.0},
}


class ProviderError(Exception):
    """调用失败（超时、连续非法 JSON、Schema 不合规等）。"""


class SchemaError(ProviderError):
    """输出不合 Schema。"""


def make_input_hash(task, prompt_version, provider, inputs):
    """幂等键：task + prompt_version + provider + 规范化后的 inputs。

    为什么 prompt_version 要进 hash：提示词改了才算新输入，否则改了提示词
    却命中旧缓存，等于悄悄用了旧版本的生成逻辑。
    """
    blob = json.dumps(
        {"task": task, "v": prompt_version, "p": provider, "inputs": inputs},
        ensure_ascii=False, sort_keys=True, default=str,
    )
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def validate_against(data, schema):
    """轻量校验，返回错误列表（空 = 通过）。

    不引入 jsonschema 依赖（保持「标准库 + 3 个三方包」的现状）。
    schema 支持三种形态：
      - None：不校验
      - 可调用对象：返回错误字符串列表
      - dict：检查 required 字段存在性与 properties 的类型
    """
    if schema is None:
        return []
    if callable(schema):
        return list(schema(data) or [])
    if not isinstance(schema, dict):
        return ["schema 必须是 dict 或可调用对象"]

    errs = []
    if not isinstance(data, dict):
        return ["输出必须是 JSON 对象"]
    for key in schema.get("required", []):
        if key not in data:
            errs.append("缺字段: %s" % key)
    for key, typ in (schema.get("properties") or {}).items():
        if key in data and data[key] is not None:
            if typ == "string" and not isinstance(data[key], str):
                errs.append("%s 应为字符串" % key)
            elif typ == "array" and not isinstance(data[key], list):
                errs.append("%s 应为数组" % key)
            elif typ == "object" and not isinstance(data[key], dict):
                errs.append("%s 应为对象" % key)
            elif typ == "integer" and not isinstance(data[key], int):
                errs.append("%s 应为整数" % key)
    return errs


class TextModelProvider:
    """文本模型基类：重试 + 解析 + 校验 + 幂等缓存 + 审计。"""

    name = "base"
    model = "unknown"
    prompt_version = "1.0"

    def __init__(self, max_retries=2, use_cache=True):
        self.max_retries = max_retries
        self.use_cache = use_cache

    # --- 子类需要实现的两个方法 ---

    def build_messages(self, task, inputs):
        raise NotImplementedError

    def _raw_call(self, task, inputs, messages):
        """发一次请求，返回 (文本, prompt_tokens, completion_tokens)。失败请抛异常。"""
        raise NotImplementedError

    # --- 公共能力 ---

    def estimate_cost(self, prompt_tokens, completion_tokens):
        """按 PRICING 估算费用；未配置单价时返回 None。"""
        price = PRICING.get(self.model)
        if not price or prompt_tokens is None or completion_tokens is None:
            return None
        return round(
            prompt_tokens / 1e6 * price["in"] + completion_tokens / 1e6 * price["out"], 6
        )

    def _audit(self, task, input_hash, output=None, latency_ms=None,
               prompt_tokens=None, completion_tokens=None, retries=0,
               error=None, cached=0):
        db.save_v2_model_call(
            task=task, provider=self.name, model=self.model,
            prompt_version=self.prompt_version, input_hash=input_hash,
            output=output, latency_ms=latency_ms,
            prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
            cost=self.estimate_cost(prompt_tokens, completion_tokens),
            retries=retries, error=error, cached=cached,
        )

    def generate_json(self, task, schema, inputs, idempotency_key=None):
        """生成并解析 JSON。成功返回 dict，失败抛 ProviderError。

        保证：失败时也留审计行（含 error），但**不写入任何半成品产物**——
        产物由调用方在本方法成功返回后才落库。
        """
        key = idempotency_key or make_input_hash(
            task, self.prompt_version, self.name, inputs
        )

        if self.use_cache:
            hit = db.find_v2_model_call(key)
            if hit:
                self._audit(task, key, output=hit["output"], latency_ms=0, cached=1)
                return json.loads(hit["output"])

        messages = self.build_messages(task, inputs)
        last_err = None
        for attempt in range(self.max_retries + 1):
            started = time.monotonic()
            try:
                text, pt, ct = self._raw_call(task, inputs, messages)
                data = json.loads(text)
                errs = validate_against(data, schema)
                if errs:
                    raise SchemaError("；".join(errs))
                self._audit(
                    task, key, output=text,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    prompt_tokens=pt, completion_tokens=ct, retries=attempt,
                )
                return data
            except Exception as exc:  # noqa: BLE001 - 任何失败都要留痕再重试
                last_err = exc
                self._audit(
                    task, key,
                    latency_ms=int((time.monotonic() - started) * 1000),
                    retries=attempt, error="%s: %s" % (type(exc).__name__, exc),
                )
        raise ProviderError(
            "%s 调用失败（重试 %d 次）: %s" % (self.name, self.max_retries, last_err)
        )


class DeepSeekProvider(TextModelProvider):
    """真实 DeepSeek 调用。无 API Key 时不要在测试里实例化它。"""

    name = "deepseek"
    model = "deepseek-chat"

    def __init__(self, api_key, prompt_version="1.0", timeout=60, **kw):
        super().__init__(**kw)
        self.api_key = api_key
        self.prompt_version = prompt_version
        self.timeout = timeout

    def build_messages(self, task, inputs):
        from prompts import V2_PROMPTS
        spec = V2_PROMPTS[task]
        # system / user 都做格式化：禁用词表这类常量放 system 里更省 token，
        # 代价是两侧模板里的字面花括号都要写成 {{ }}
        return [
            {"role": "system", "content": spec["system"].format(**inputs)},
            {"role": "user", "content": spec["user"].format(**inputs)},
        ]

    def _raw_call(self, task, inputs, messages):
        from openai import OpenAI
        client = OpenAI(api_key=self.api_key, base_url=DEEPSEEK_BASE_URL, timeout=self.timeout)
        resp = client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=0.3,
            response_format={"type": "json_object"},
        )
        usage = getattr(resp, "usage", None)
        return (
            resp.choices[0].message.content,
            getattr(usage, "prompt_tokens", None),
            getattr(usage, "completion_tokens", None),
        )


class FakeTextProvider(TextModelProvider):
    """本地假 Provider：不联网、不收费、确定性输出，供集成测试使用。

    可以注入三种故障来测降级：fail_times（前 N 次抛超时）、
    bad_json（返回非法 JSON）、responder 返回缺字段的结构。
    """

    name = "fake"
    model = "fake-1.0"

    def __init__(self, responder=None, fail_times=0, bad_json=False, **kw):
        super().__init__(**kw)
        self.responder = responder
        self.fail_times = fail_times
        self.bad_json = bad_json
        self.call_count = 0

    def build_messages(self, task, inputs):
        return [{"role": "user", "content": json.dumps(inputs, ensure_ascii=False)}]

    def _raw_call(self, task, inputs, messages):
        self.call_count += 1
        if self.call_count <= self.fail_times:
            raise TimeoutError("fake timeout #%d" % self.call_count)
        if self.bad_json:
            return "{ 这不是 JSON", None, None
        payload = self.responder(task, inputs) if self.responder else {"task": task}
        return json.dumps(payload, ensure_ascii=False), None, None
