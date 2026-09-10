"""CP2 Provider 契约测试：确定性、重试、非法 JSON、幂等缓存、审计留痕。"""
import pytest

import db
import providers
from providers import FakeTextProvider, ProviderError

INPUTS = {"goal": "理解 Agent Harness", "claims": ["a", "b"]}


def _provider(**kw):
    return FakeTextProvider(responder=lambda task, inputs: {"ok": True, "task": task}, **kw)


def test_fake_is_deterministic(tmp_db):
    p = _provider()
    a = p.generate_json("demo", None, INPUTS)
    b = p.generate_json("demo", None, INPUTS)
    assert a == b == {"ok": True, "task": "demo"}


def test_cache_hit_does_not_reinvoke(tmp_db):
    """同 input_hash 第二次调用不产生新的模型请求——这是「不重复收费」的落点。"""
    p = _provider()
    p.generate_json("demo", None, INPUTS)
    assert p.call_count == 1
    p.generate_json("demo", None, INPUTS)
    assert p.call_count == 1, "缓存未命中，重复调用了模型"

    rows = db.recent_v2_model_calls(task="demo")
    assert rows[0]["cached"] == 1
    assert rows[0]["latency_ms"] == 0


def test_different_inputs_miss_cache(tmp_db):
    p = _provider()
    p.generate_json("demo", None, INPUTS)
    p.generate_json("demo", None, {"goal": "另一个目标", "claims": ["c"]})
    assert p.call_count == 2


def test_prompt_version_changes_cache_key(tmp_db):
    """提示词版本变了就不该命中旧缓存，否则等于悄悄用了旧生成逻辑。"""
    assert providers.make_input_hash("t", "1.0", "fake", INPUTS) != \
        providers.make_input_hash("t", "1.1", "fake", INPUTS)


def test_retry_then_succeed(tmp_db):
    p = _provider(fail_times=2, max_retries=3)
    data = p.generate_json("demo", None, INPUTS)
    assert data["ok"] is True
    assert p.call_count == 3
    ok = [r for r in db.recent_v2_model_calls(task="demo") if not r["error"]]
    assert ok[0]["retries"] == 2, "成功那次应记录重试次数"


def test_invalid_json_leaves_no_half_product(tmp_db):
    """非法 JSON：抛错、留 error 审计行、且没有可复用产物。"""
    p = FakeTextProvider(bad_json=True, max_retries=1)
    with pytest.raises(ProviderError):
        p.generate_json("demo", None, INPUTS)
    assert p.call_count == 2  # 1 次 + 1 次重试

    rows = db.recent_v2_model_calls(task="demo")
    assert rows and all(r["error"] for r in rows)
    assert db.find_v2_model_call(providers.make_input_hash("demo", "1.0", "fake", INPUTS)) is None


def test_schema_violation_is_rejected(tmp_db):
    schema = {"required": ["title", "body"], "properties": {"body": "string"}}
    p = FakeTextProvider(responder=lambda t, i: {"title": "只有标题"})
    with pytest.raises(ProviderError):
        p.generate_json("demo", schema, INPUTS)


def test_callable_schema_validator(tmp_db):
    schema = lambda d: ["body 太短"] if len(d.get("body", "")) < 10 else []  # noqa: E731
    p = FakeTextProvider(responder=lambda t, i: {"body": "短"})
    with pytest.raises(ProviderError):
        p.generate_json("demo", schema, INPUTS)


def test_audit_row_has_required_fields(tmp_db):
    """方案 4.3：每次调用都要能查到 provider / model / prompt_version / 耗时。"""
    p = _provider()
    p.generate_json("demo", None, INPUTS)
    row = db.recent_v2_model_calls(task="demo")[0]
    for field in ("provider", "model", "prompt_version", "input_hash", "latency_ms", "ts"):
        assert row[field] is not None, "审计缺字段: %s" % field
    assert row["provider"] == "fake"
    assert row["error"] is None


def test_cost_is_null_when_pricing_unset(tmp_db):
    """没配单价就记 NULL，不编造成本。"""
    p = _provider()
    p.generate_json("demo", None, INPUTS)
    assert db.recent_v2_model_calls(task="demo")[0]["cost"] is None
