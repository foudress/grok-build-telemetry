"""Reconstruct helpers (moved from reconstruct.py — same bodies)."""
from __future__ import annotations

from typing import Any, Optional

from token_telemetry.tokenizer import (
    count_chars_as_tokens,
    tokenizer_mode,
)
from token_telemetry.pricing.rates import (
    _fit_usd_parts,
    _money_parts,
    _price_cache,
    _price_in,
    _price_out,
    _scale_ints,
    estimate_cost_usd,
    pick_tier,
    step_tier_ctx,
)

from token_telemetry.pricing.reconstruct.segments import (
    _compact_out_tokens,
    _step_compact_out_in,
    _step_stream_end,
    _step_stream_start,
    _step_total_input,
)
from token_telemetry.pricing.reconstruct.weights import (
    _clamp_paid_to_inputs,
    _collect_step_weights,
    _delta_ctx_for_fit,
    _tok_from_chars,
    _tool_arg_w,
    _tool_result_w,
    _tools_z_from_steps,
)

def _fit_tools_to_delta_ctx(
    *,
    tools_z: list[float],
    reentry: list[int],
    compact_per: list[int],
    user_in: int,
    delta_ctx: int,
    last_out: int = 0,
) -> list[int]:
    """Fit tool tokZ to Δctx leftover.

    Δctx includes the last call's Out (not yet re-entry this round). Do not
    spread that mass onto tools — it is billed as Out, then as next-round In.
    """
    n = len(tools_z)
    z_w = [max(0.0, float(tools_z[i]) if i < n else 0.0) for i in range(n)]
    if n <= 0:
        return []
    tokz = [max(0, int(round(z))) for z in z_w]
    if int(delta_ctx or 0) <= 0 or sum(z_w) <= 0:
        return tokz
    compare = (
        max(0, int(user_in or 0))
        + int(sum(int(x) for x in reentry))
        + int(sum(tokz))
        + int(sum(int(x) for x in compact_per))
        + max(0, int(last_out or 0))
    )
    delta = int(delta_ctx) - int(compare)
    target_tools = max(0, int(sum(tokz)) + int(delta))
    return _scale_ints(z_w, target_tools)

def _omit_last_prefix(caches: list[int]) -> tuple[list[int], int]:
    """Last call has no next LLM send → display Cached = 0."""
    if not caches:
        return [], 0
    out = list(caches)
    omitted = int(out[-1] or 0)
    out[-1] = 0
    return out, omitted

def _rolling_prefix_caches(
    *,
    n: int,
    cold: bool,
    prior_i: int,
    system_tokens: int,
    user_in: int,
    starts: list[int],
    harness_ins: list[int],
    compact_per: list[int],
) -> list[int]:
    """Call k Cached = prefix at that prompt (before last-call omit).

    Increment is prev In − compact_out_in (compact is already C0).
    """
    if n <= 0:
        return []
    caches = [0] * n
    usr = max(0, int(user_in or 0))
    if cold:
        sys_u = max(0, int(system_tokens or 0))
        if sys_u > 0 or usr > 0:
            caches[0] = sys_u + usr
        else:
            caches[0] = max(0, int(starts[0]) if starts else 0)
    else:
        caches[0] = max(0, int(prior_i or 0)) + usr
    for k in range(1, n):
        prev_h = int(harness_ins[k - 1]) if k - 1 < len(harness_ins) else 0
        prev_c = int(compact_per[k - 1]) if k - 1 < len(compact_per) else 0
        # compact_out_in is already this call's C0, not extra growth
        incr = max(0, prev_h - prev_c)
        caches[k] = int(caches[k - 1]) + incr
    return caches

def _reconstruct_inputs_and_cache(
    *,
    n: int,
    starts: list[int],
    ends: list[int],
    harness_w: list[float],
    raw_out_thought: list[float],
    raw_out_enc: list[float],
    raw_out_emit: list[float],
    raw_out_message: list[float],
    off_in: Optional[int],
    off_unc: Optional[int],
    cold: bool,
    prior_i: int,
    user_uncached_tokens: int,
) -> tuple[list[int], list[int]]:
    """Official-scaled Input and paid-uncached (API bill). Display Cached is rolling."""
    if n <= 0:
        return [], []

    input_w = [float(max(0, int(starts[i]) if i < len(starts) else 0)) for i in range(n)]
    if off_in is not None:
        if sum(input_w) > 0:
            inputs = _scale_ints(input_w, int(off_in))
        else:
            inputs = [0] * n
            inputs[0] = int(off_in)
    else:
        inputs = [int(max(0, round(w))) for w in input_w]

    paid_w: list[float] = []
    for i in range(n):
        start_i = int(starts[i]) if i < len(starts) else 0
        if i == 0:
            if cold:
                user_u = max(0, int(user_uncached_tokens or 0))
                small = float(harness_w[0]) if harness_w else 0.0
                paid_w.append(float(max(1, user_u, small)))
            else:
                growth = max(0, start_i - int(prior_i))
                user_u = max(0, int(user_uncached_tokens or 0))
                small = float(harness_w[0]) if harness_w else 0.0
                paid_w.append(float(max(1, growth, user_u, small)))
        else:
            prev_end = int(ends[i - 1]) if i - 1 < len(ends) else 0
            prev_out = (
                (raw_out_thought[i - 1] if i - 1 < len(raw_out_thought) else 0.0)
                + (raw_out_enc[i - 1] if i - 1 < len(raw_out_enc) else 0.0)
                + (raw_out_emit[i - 1] if i - 1 < len(raw_out_emit) else 0.0)
                + (raw_out_message[i - 1] if i - 1 < len(raw_out_message) else 0.0)
            )
            h_prev = float(harness_w[i - 1]) if i - 1 < len(harness_w) else 0.0
            h_cur = float(harness_w[i]) if i < len(harness_w) else 0.0
            paid_w.append(
                float(max(1.0, start_i - prev_end, prev_out + h_prev, h_cur))
            )

    if off_unc is not None and sum(paid_w) > 0:
        paid_uncached = _scale_ints(paid_w, int(off_unc))
    elif off_unc is not None:
        paid_uncached = [0] * n
        paid_uncached[0] = int(off_unc)
    else:
        paid_uncached = [max(0, int(round(w))) for w in paid_w]

    paid_uncached = _clamp_paid_to_inputs(inputs, paid_uncached)
    return inputs, paid_uncached
