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
    _step_stream_end,
    _step_stream_start,
    _step_total_input,
)

def _tok_from_chars(chars: int) -> float:
    return float(count_chars_as_tokens(max(0, int(chars or 0))))

def _tool_result_w(t: dict[str, Any]) -> float:
    """Harness In weight: tokenizer result tokens (chat_history first). Never args."""
    for key in ("ch_result_tokens", "result_tokens_est"):
        est = int(t.get(key) or 0)
        if est > 0:
            return float(est)
    chars = int(t.get("ch_result_chars") or t.get("result_chars") or 0)
    return _tok_from_chars(chars)

def _tool_arg_w(t: dict[str, Any]) -> float:
    est = int(t.get("arg_tokens_est") or 0)
    if est > 0:
        return float(est)
    return _tok_from_chars(int(t.get("arg_chars") or 0))

def _collect_step_weights(steps: list[dict[str, Any]]) -> dict[str, Any]:
    """Starts/ends + tokenizer Out / harness / tool weights for each LLM call."""
    starts: list[int] = []
    ends: list[int] = []
    raw_out_thought: list[float] = []
    raw_out_enc: list[float] = []
    raw_out_emit: list[float] = []
    raw_out_message: list[float] = []
    harness_w: list[float] = []
    tool_ws: list[list[float]] = []

    for step in steps:
        starts.append(_step_stream_start(step))
        ends.append(_step_stream_end(step))

        th_tok_w = step.get("thought_summary_tokens")
        if th_tok_w is not None and int(th_tok_w) > 0:
            th_w = float(int(th_tok_w))
        else:
            th_w = _tok_from_chars(
                int(
                    step.get("thought_summary_chars")
                    or step.get("thought_chars")
                    or 0
                )
            )
        enc_tok_w = step.get("thought_encrypted_tokens")
        if enc_tok_w is not None and int(enc_tok_w) > 0:
            enc_w = float(int(enc_tok_w))
        else:
            enc_w = _tok_from_chars(int(step.get("thought_encrypted_chars") or 0))
        msg_tok_w = step.get("message_tokens")
        if msg_tok_w is not None and int(msg_tok_w) > 0:
            msg_w = float(int(msg_tok_w))
        else:
            msg_w = _tok_from_chars(int(step.get("message_chars") or 0))
        tools = step.get("tools") or []
        emit = sum(_tool_arg_w(t) for t in tools)
        if emit <= 0:
            tt_emit = max(0, int(step.get("model_emit_delta") or 0))
            if 0 < tt_emit < 2_000:
                emit = float(tt_emit)
        raw_out_thought.append(th_w)
        raw_out_enc.append(enc_w)
        raw_out_emit.append(float(emit))
        raw_out_message.append(msg_w)

        tw = [_tool_result_w(t) for t in tools]
        pool = int(step.get("harness_pool_tokens") or 0)
        if pool <= 0:
            pool = sum(max(0, int(t.get("context_delta") or 0)) for t in tools)
        if pool <= 0:
            pool = sum(max(0, int(t.get("tt_delta_observed") or 0)) for t in tools)
        if sum(tw) <= 0 and pool > 0:
            tw = [float(pool)]
        tool_ws.append(tw)
        if pool > 0:
            harness_w.append(float(pool))
        else:
            harness_w.append(float(sum(tw)) if tw else 0.0)

    return {
        "starts": starts,
        "ends": ends,
        "raw_out_thought": raw_out_thought,
        "raw_out_enc": raw_out_enc,
        "raw_out_emit": raw_out_emit,
        "raw_out_message": raw_out_message,
        "harness_w": harness_w,
        "tool_ws": tool_ws,
    }

def _clamp_paid_to_inputs(inputs: list[int], paid_uncached: list[int]) -> list[int]:
    """paid_uncached[i] <= inputs[i]; leftover unc goes to later/last calls."""
    n = len(inputs)
    paid = [int(paid_uncached[i]) if i < len(paid_uncached) else 0 for i in range(n)]
    leftover = 0
    for i in range(n):
        if paid[i] > inputs[i]:
            leftover += paid[i] - inputs[i]
            paid[i] = int(inputs[i])
    if leftover > 0:
        for i in range(n - 1, -1, -1):
            room = int(inputs[i]) - paid[i]
            if room <= 0:
                continue
            take = min(room, leftover)
            paid[i] += take
            leftover -= take
            if leftover <= 0:
                break
    return paid

def _delta_ctx_for_fit(
    *,
    cold: bool,
    prior_i: int,
    system_tokens: int,
    context_end_tokens: Optional[int],
    stream_ends: list[int],
) -> int:
    """Window growth for tool fit. R1: end − System. Warm: end − prior."""
    y = 0
    pos_ends = [int(e or 0) for e in stream_ends if int(e or 0) > 0]
    if pos_ends:
        y = max(pos_ends)
    elif context_end_tokens is not None:
        try:
            y = max(0, int(context_end_tokens))
        except (TypeError, ValueError):
            y = 0
    x = int(system_tokens or 0) if cold else int(prior_i or 0)
    return max(0, int(y) - int(x))

def _tools_z_from_steps(steps: list[dict[str, Any]]) -> tuple[list[float], list[list[float]]]:
    """Per-call / per-tool tokZ. Never invent a fake tool from the harness pool."""
    z_sum: list[float] = []
    per: list[list[float]] = []
    for step in steps:
        tools = step.get("tools") or [] if isinstance(step, dict) else []
        tw = [_tool_result_w(t) for t in tools if isinstance(t, dict)]
        per.append(tw)
        z_sum.append(float(sum(tw)))
    return z_sum, per
