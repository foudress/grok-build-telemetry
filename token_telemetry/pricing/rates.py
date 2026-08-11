"""Rate tables, tier selection, and pure cost math for Grok Build."""

from __future__ import annotations

from typing import Any, Optional

from token_telemetry.tokenizer import BYTES_PER_TOKEN


COST_USD_TICKS_PER_USD = 10**10
CONTEXT_TIER_THRESHOLD = 200_000
# Align with xai-token-estimation BYTES_PER_TOKEN (open-round proxy)
CHARS_PER_TOKEN = float(BYTES_PER_TOKEN)

# USD per 1_000_000 tokens (grok-4.5 list rates)
TIER_LOW = {
    "name": "≤200k",
    "input": 2.0,
    "output": 6.0,
    "cached_input": 0.3,
}
TIER_HIGH = {
    "name": ">200k",
    "input": 4.0,
    "output": 12.0,
    "cached_input": 0.6,
}


def ticks_to_usd(ticks: Optional[int | float]) -> Optional[float]:
    if ticks is None:
        return None
    try:
        t = float(ticks)
    except (TypeError, ValueError):
        return None
    if t <= 0:
        return 0.0 if t == 0 else None
    return t / COST_USD_TICKS_PER_USD


def usd_to_ticks(usd: float) -> int:
    return int(round(usd * COST_USD_TICKS_PER_USD))


def pick_tier(context_tokens: int) -> dict[str, Any]:
    if context_tokens > CONTEXT_TIER_THRESHOLD:
        return dict(TIER_HIGH)
    return dict(TIER_LOW)


def step_tier_ctx(step: Optional[dict[str, Any]], *, fallback: int = 1) -> int:
    """
    Per-LLM-call context used for ≤/>200k tier selection.

    Uses **this call's prompt size at stream start** (context_start).
    Never use round peak / context_end — if only later calls cross 200k,
    earlier calls stay on the low tier (price hike is per call, not
    applied to the whole round).
    """
    if not isinstance(step, dict):
        return max(1, int(fallback or 1))
    cs = step.get("context_start")
    if isinstance(cs, int) and cs > 0:
        return int(cs)
    # Fallbacks when stream start missing
    for key in ("stream_context_start", "calibrated_input_tokens", "tokens_in"):
        v = step.get(key)
        if isinstance(v, int) and v > 0:
            return int(v)
    return max(1, int(fallback or 1))


def resolve_context_for_tier(
    *,
    peak_context_tokens: Optional[int] = None,
    input_tokens: int = 0,
    model_calls: Optional[int] = None,
    cached_read_tokens: int = 0,
) -> dict[str, Any]:
    """
    Choose a single-prompt context size for the ≤/>200k tier.

    Priority:
      1. peak _meta.totalTokens observed during the turn (best)
      2. inputTokens / modelCalls (avg prompt size when multi-call)
      3. inputTokens alone (single call / unknown)

    Never use input + cached (that double-counts and sums multi-call).
    """
    in_t = max(0, int(input_tokens or 0))
    calls = int(model_calls or 0)
    peak = int(peak_context_tokens) if peak_context_tokens is not None else None

    method = "input_tokens"
    ctx = in_t

    if peak is not None and peak > 0:
        ctx = peak
        method = "peak_meta_total_tokens"
    elif calls > 1 and in_t > 0:
        ctx = max(1, in_t // calls)
        method = "input_div_model_calls"

    return {
        "context_tokens_for_tier": ctx,
        "method": method,
        "peak_context_tokens": peak,
        "input_tokens": in_t,
        "model_calls": calls or None,
        "note": (
            "Tier uses one prompt's context, not sum(input) across "
            "modelCalls. Cache is a subset of input, not added on top."
        ),
    }


def estimate_cost_usd(
    *,
    input_tokens: int = 0,
    output_tokens: int = 0,
    reasoning_tokens: int = 0,
    cached_read_tokens: int = 0,
    context_tokens: Optional[int] = None,
    peak_context_tokens: Optional[int] = None,
    model_calls: Optional[int] = None,
) -> dict[str, Any]:
    """Estimate USD from token counts using published rates."""
    in_t = max(0, int(input_tokens or 0))
    out_t = max(0, int(output_tokens or 0))
    reason_t = max(0, int(reasoning_tokens or 0))
    cache_t = max(0, int(cached_read_tokens or 0))
    # cache is subset of input
    if cache_t > in_t:
        cache_t = in_t
    uncached = max(0, in_t - cache_t)

    tier_info = resolve_context_for_tier(
        peak_context_tokens=peak_context_tokens
        if peak_context_tokens is not None
        else context_tokens,
        input_tokens=in_t,
        model_calls=model_calls,
        cached_read_tokens=cache_t,
    )
    context_tokens = int(tier_info["context_tokens_for_tier"])
    tier = pick_tier(context_tokens)

    # Generation: bill output only (reasoning ⊆ / reported inside output shape)
    gen_billed = out_t
    cost_input = uncached * tier["input"] / 1_000_000
    cost_cache = cache_t * tier["cached_input"] / 1_000_000
    cost_output = gen_billed * tier["output"] / 1_000_000
    total = cost_input + cost_cache + cost_output

    return {
        "tier": tier["name"],
        "context_tokens_for_tier": context_tokens,
        "tier_resolution": tier_info,
        "rates": {
            "input_per_m": tier["input"],
            "output_per_m": tier["output"],
            "cached_input_per_m": tier["cached_input"],
        },
        "tokens": {
            "input": in_t,
            "cached_read": cache_t,
            "uncached_input": uncached,
            "output": out_t,
            "reasoning": reason_t,
            "generated_billed": gen_billed,
            "reasoning_note": (
                "reasoning not added on top of output "
                "(totalTokens ≈ input+output in logs)"
            ),
        },
        "cost_usd": {
            # Full precision — UI rounds for display only
            "uncached_input": float(cost_input),
            "cached_input": float(cost_cache),
            "output": float(cost_output),
            # aliases for older UI keys
            "input": float(cost_input),
            "output_incl_reasoning": float(cost_output),
            "total": float(total),
        },
        "cost_usd_ticks_estimate": usd_to_ticks(total),
    }


def estimate_from_usage(
    usage: dict[str, Any],
    *,
    peak_context_tokens: Optional[int] = None,
) -> dict[str, Any]:
    """usage dict with camelCase (from turn_completed) or snake_case."""

    def g(*keys: str, default: int = 0) -> int:
        for k in keys:
            if k in usage and usage[k] is not None:
                return int(usage[k])
        return default

    def g_opt(*keys: str) -> Optional[int]:
        for k in keys:
            if k in usage and usage[k] is not None:
                return int(usage[k])
        return None

    return estimate_cost_usd(
        input_tokens=g("inputTokens", "input_tokens"),
        output_tokens=g("outputTokens", "output_tokens"),
        reasoning_tokens=g("reasoningTokens", "reasoning_tokens"),
        cached_read_tokens=g("cachedReadTokens", "cached_read_tokens"),
        peak_context_tokens=peak_context_tokens
        if peak_context_tokens is not None
        else g_opt("peakContextTokens", "peak_context_tokens"),
        model_calls=g_opt("modelCalls", "model_calls"),
    )


def _scale_ints(raw: list[float], target: int) -> list[int]:
    """Scale non-negative weights so they sum exactly to target."""
    if not raw:
        return []
    target = max(0, int(target))
    if target == 0:
        return [0] * len(raw)
    s = sum(max(0.0, float(x)) for x in raw)
    if s <= 0:
        # equal split
        base = target // len(raw)
        out = [base] * len(raw)
        for i in range(target - base * len(raw)):
            out[i] += 1
        return out
    floats = [max(0.0, float(x)) / s * target for x in raw]
    ints = [int(x) for x in floats]
    rem = target - sum(ints)
    # distribute remainder by fractional part
    order = sorted(
        range(len(floats)),
        key=lambda i: floats[i] - ints[i],
        reverse=True,
    )
    for i in order:
        if rem <= 0:
            break
        ints[i] += 1
        rem -= 1
    return ints


def _price_in(tokens: int, tier_ctx: int) -> float:
    if tokens <= 0:
        return 0.0
    return tokens * pick_tier(tier_ctx)["input"] / 1_000_000


def _price_cache(tokens: int, tier_ctx: int) -> float:
    if tokens <= 0:
        return 0.0
    return tokens * pick_tier(tier_ctx)["cached_input"] / 1_000_000


def _price_out(tokens: int, tier_ctx: int) -> float:
    if tokens <= 0:
        return 0.0
    return tokens * pick_tier(tier_ctx)["output"] / 1_000_000


def _money_parts(
    *,
    tokens_in: int = 0,
    tokens_cached: int = 0,
    tokens_out: int = 0,
    cost_in: float = 0.0,
    cost_cached: float = 0.0,
    cost_out: float = 0.0,
) -> dict[str, Any]:
    total = float(cost_in) + float(cost_cached) + float(cost_out)
    return {
        "tokens_in": int(tokens_in or 0),
        "tokens_cached": int(tokens_cached or 0),
        "tokens_out": int(tokens_out or 0),
        "cost_in_usd": float(cost_in),
        "cost_cached_usd": float(cost_cached),
        "cost_out_usd": float(cost_out),
        "estimate_usd": float(total),
    }


def _fit_usd_parts(parts: list[float], target: float) -> list[float]:
    """Scale parts so sum == target; put residual on the largest part (no drift)."""
    if not parts:
        return []
    if target <= 0:
        return [0.0] * len(parts)
    s = sum(parts)
    if s <= 0:
        out = [0.0] * len(parts)
        out[0] = float(target)
        return out
    scaled = [p * target / s for p in parts]
    # Fix float residual on max component
    drift = target - sum(scaled)
    if abs(drift) > 1e-12:
        j = max(range(len(scaled)), key=lambda i: scaled[i])
        scaled[j] += drift
    return scaled

