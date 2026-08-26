"""LLM generation rate: this-call stream start → last received chunk.

Numerator is this call's billed Out TokF (thought + reasoning + message +
tool-request). Never encrypted-blob TokZ (ciphertext chars/4, often 50–100×
the bill) and never leftover official ``tokens_out`` (parent turn can still
hold unpeeled sub-agent Out).

Denominator is wall ms from when this call's prompt was handed to the model
(``streamStartMs`` — the harness/user send) until the last thought/message
chunk or last tool-*request* (declare), not tool execution.
"""

from __future__ import annotations

from typing import Any, Optional


def _ms(v: Any) -> Optional[int]:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float)) and v > 0:
        return int(v)
    return None


def _pos_int(v: Any) -> int:
    if isinstance(v, bool):
        return 0
    try:
        n = int(v or 0)
    except (TypeError, ValueError):
        return 0
    return n if n > 0 else 0


def _tool_req_tokens(step: dict[str, Any]) -> int:
    n = 0
    for t in step.get("tools") or []:
        if not isinstance(t, dict):
            continue
        n += _pos_int(t.get("arg_tokens_est"))
    if n > 0:
        return n
    for ch in step.get("children") or []:
        if not isinstance(ch, dict) or ch.get("kind") != "phase_llm":
            continue
        for sub in ch.get("children") or []:
            if not isinstance(sub, dict) or sub.get("kind") != "tool_request":
                continue
            n += _pos_int(
                sub.get("arg_tokens_est")
                or sub.get("tokenizer_tokens")
            )
    return n


def call_out_tokf(step: dict[str, Any]) -> Optional[int]:
    """Billed Out TokF this LLM actually produced — not blob size, not child bill."""
    if not isinstance(step, dict):
        return None
    est = step.get("estimate") if isinstance(step.get("estimate"), dict) else {}

    def _part(est_key: str, *step_keys: str) -> int:
        if est_key in est:
            return _pos_int(est.get(est_key))
        for k in step_keys:
            n = _pos_int(step.get(k))
            if n:
                return n
        return 0

    thought = _part(
        "output_thought_tokens", "thought_summary_tokens", "thought_tokens"
    )
    msg = _part("output_message_tokens", "message_tokens")
    if "output_emit_tokens" in est:
        emit = _pos_int(est.get("output_emit_tokens"))
    else:
        emit = _tool_req_tokens(step)
    re = _part("output_reasoning_tokens")
    enc = _pos_int(
        step.get("thought_encrypted_tokens_own")
        or step.get("thought_encrypted_tokens")
    )
    # reasoning_F is leftover Enc after thought+message+toolreq. Use it only
    # when it fits this call's encrypted stamp. If residual > stamp, that is
    # unpeeled child Out — drop it. Never substitute enc TokZ (ciphertext).
    re_use = 0
    if re > 0 and enc > 0 and re <= enc:
        re_use = re
    elif re > 0 and enc <= 0:
        visible = thought + msg + emit
        if visible > 0 and re <= visible * 2 + 64:
            re_use = re
    total = thought + msg + emit + re_use
    billed = _pos_int(est.get("output_tokens"))
    if billed > 0 and total > billed:
        total = billed
    return total if total > 0 else None


def stream_out_tokens(step: dict[str, Any]) -> Optional[int]:
    """Alias — rate numerator is TokF, not streamed ciphertext size."""
    return call_out_tokf(step)


def _mean(vals: list[float]) -> Optional[float]:
    xs = [float(v) for v in vals if isinstance(v, (int, float))]
    if not xs:
        return None
    return round(sum(xs) / len(xs), 3)


def _prompt_send_ms(
    round_: dict[str, Any],
    step: dict[str, Any],
    prev_step: Optional[dict[str, Any]],
) -> Optional[int]:
    """When this call's prompt was handed to the model.

    Prefer this step's ``streamStartMs`` (harness send / LLM process start).
    Do not use ``first_llm_ms`` — that is first *visible* chunk after hidden
    reasoning and collapses the window. Do not start at user_submit when a
    stream clock exists (hooks / idle before the request are not generation).
    """
    send = _ms(step.get("stream_start_ms")) or _ms(step.get("started_ms"))
    if send is not None:
        return send
    if prev_step is not None:
        return _ms(prev_step.get("last_tool_ms"))
    return (
        _ms(round_.get("user_submit_ms"))
        or _ms(round_.get("last_user_ms"))
        or _ms(round_.get("turn_start_ms"))
        or _ms(round_.get("started_ms"))
    )


def _receive_end_ms(
    step: dict[str, Any],
    *,
    is_last: bool,
    round_: dict[str, Any],
) -> Optional[int]:
    """Last byte of this LLM response: last chunk or last tool-*request*."""
    candidates = [
        _ms(step.get("last_llm_ms")),
        _ms(step.get("last_tool_request_ms")),
        _ms(step.get("first_tool_ms")),
    ]
    present = [c for c in candidates if c is not None]
    if present:
        return max(present)
    if is_last and _ms(step.get("first_tool_ms")) is None:
        return _ms(round_.get("completed_ms"))
    return None


def attach_gen_rates(round_: dict[str, Any]) -> None:
    """Stamp per-call + round tok/s. Additive only — no billing fields."""
    if not isinstance(round_, dict):
        return
    steps = [s for s in (round_.get("model_steps") or []) if isinstance(s, dict)]
    rates: list[float] = []
    gen_sum = 0
    prev: Optional[dict[str, Any]] = None
    n = len(steps)
    for i, step in enumerate(steps):
        send = _prompt_send_ms(round_, step, prev)
        end = _receive_end_ms(step, is_last=(i == n - 1), round_=round_)
        out_t = call_out_tokf(step)
        gen_ms = None
        tps = None
        # Real send→receive span only. Never invent 1 ms (that is 1k tok/s
        # for a 1-token chunk with identical timestamps).
        if send is not None and end is not None and end > send:
            gen_ms = end - send
        if gen_ms is not None and out_t is not None:
            tps = round(out_t / (gen_ms / 1000.0), 3)
            rates.append(tps)
            gen_sum += gen_ms
        step["prompt_ready_ms"] = send
        step["prompt_start_ms"] = send
        step["response_end_ms"] = end
        step["gen_ms"] = gen_ms
        step["gen_out_tokens"] = out_t
        step["gen_tokens_per_sec"] = tps
        prev = step
    out_sum = 0
    for s in steps:
        go = s.get("gen_out_tokens")
        if isinstance(go, int):
            out_sum += go
    round_["gen_ms"] = gen_sum if steps else None
    round_["gen_out_tokens"] = out_sum if out_sum else None
    round_["gen_tokens_per_sec"] = _mean(rates)
    round_["gen_rate_n"] = len(rates)


def mean_gen_rate(rows: list[Any], key: str = "gen_tokens_per_sec") -> Optional[float]:
    vals: list[float] = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        v = row.get(key)
        if isinstance(v, (int, float)):
            vals.append(float(v))
    return _mean(vals)
