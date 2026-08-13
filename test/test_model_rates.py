"""Model-aware list rates (grok-4.5 vs grok-4.6 cache)."""

from token_telemetry.pricing import (
    estimate_cost_usd,
    estimate_from_usage,
    normalize_model_id,
    pick_tier,
    pricing_model_scope,
    rates_for,
)


def test_normalize_model_ids():
    assert normalize_model_id("grok-4.6") == "grok-4.6"
    assert normalize_model_id("grok-4.6-build") == "grok-4.6"
    assert normalize_model_id("grok-4.5-build") == "grok-4.5"
    assert normalize_model_id("Grok 4.5") == "grok-4.5"
    assert normalize_model_id("grok-4.3") is None
    assert normalize_model_id(None) is None


def test_45_cache_unchanged():
    low = pick_tier(1_000, model="grok-4.5")
    high = pick_tier(250_000, model="grok-4.5")
    assert low["cached_input"] == 0.3
    assert high["cached_input"] == 0.6
    assert low["input"] == 2.0 and low["output"] == 6.0
    assert high["input"] == 4.0 and high["output"] == 12.0


def test_46_cache_higher_same_io():
    low = pick_tier(1_000, model="grok-4.6")
    high = pick_tier(250_000, model="grok-4.6")
    assert low["cached_input"] == 0.5
    assert high["cached_input"] == 1.0
    assert low["input"] == 2.0 and low["output"] == 6.0
    assert high["input"] == 4.0 and high["output"] == 12.0


def test_estimate_cache_delta():
    kwargs = dict(
        input_tokens=10_000,
        output_tokens=0,
        cached_read_tokens=10_000,
        peak_context_tokens=10_000,
        model_calls=1,
    )
    a = estimate_cost_usd(**kwargs, model="grok-4.5")
    b = estimate_cost_usd(**kwargs, model="grok-4.6")
    assert abs(a["cost_usd"]["cached_input"] - 0.003) < 1e-12
    assert abs(b["cost_usd"]["cached_input"] - 0.005) < 1e-12
    assert a["model"] == "grok-4.5"
    assert b["model"] == "grok-4.6"


def test_estimate_from_usage_modelUsage():
    usage = {
        "inputTokens": 10_000,
        "outputTokens": 0,
        "cachedReadTokens": 10_000,
        "modelCalls": 1,
        "modelUsage": {"grok-4.6-build": {}},
    }
    est = estimate_from_usage(usage, peak_context_tokens=10_000)
    assert est["model"] == "grok-4.6"
    assert abs(est["cost_usd"]["cached_input"] - 0.005) < 1e-12


def test_contextvar_scope():
    with pricing_model_scope("grok-4.6"):
        assert pick_tier(1)["cached_input"] == 0.5
    assert pick_tier(1)["cached_input"] == 0.3


def test_rates_for_default_45():
    pack = rates_for(None)
    assert pack["family"] == "grok-4.5"
    assert pack["low"]["cached_input"] == 0.3
