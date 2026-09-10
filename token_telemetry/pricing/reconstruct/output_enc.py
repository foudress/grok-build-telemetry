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

from token_telemetry.pricing.reconstruct.weights import _collect_step_weights

def _enc_tokz_weights(n: int, enc_w: list[float], visible_w: list[float]) -> list[float]:
    """Leftover Out → Enc pro-rata Enc TokZ (never a flat 1.0 per call).

    Enc TokZ is the weight when present. Calls missing an Enc stamp still
    get a share, but by that call's visible TokZ (thought + toolreq +
    message) — not a uniform 1.0, which made every LLM Call the same TokF
    whenever Enc stamps were absent.
    """
    enc = [
        max(0.0, float(enc_w[i])) if i < len(enc_w) else 0.0 for i in range(n)
    ]
    vis = [
        max(0.0, float(visible_w[i])) if i < len(visible_w) else 0.0
        for i in range(n)
    ]
    if sum(enc) > 0:
        filled = [enc[i] if enc[i] > 0 else vis[i] for i in range(n)]
        if sum(filled) > 0:
            return filled
        return enc
    if sum(vis) > 0:
        return vis
    return [1.0] * n if n > 0 else []

def _reconstruct_output(
    n: int,
    raw_out_thought: list[float],
    raw_out_enc: list[float],
    raw_out_emit: list[float],
    raw_out_message: list[float],
    off_out: Optional[int],
    off_reason: Optional[int],
) -> dict[str, Any]:
    """Scale thought/message/toolreq if they overflow off_out; Enc gets leftover."""
    th_w_list = [max(0.0, float(raw_out_thought[i])) for i in range(n)]
    em_w_list = [max(0.0, float(raw_out_emit[i])) for i in range(n)]
    msg_w_list = [max(0.0, float(raw_out_message[i])) for i in range(n)]
    enc_w = [max(0.0, float(raw_out_enc[i])) for i in range(n)]

    out_thought = [max(0, int(round(th_w_list[i]))) for i in range(n)]
    out_message = [max(0, int(round(msg_w_list[i]))) for i in range(n)]
    out_emit = [max(0, int(round(em_w_list[i]))) for i in range(n)]
    th_sum = int(sum(out_thought))
    msg_sum = int(sum(out_message))
    em_sum = int(sum(out_emit))
    fixed_sum = int(th_sum + msg_sum + em_sum)

    if off_out is not None:
        total_out_sum = max(0, int(off_out))
    else:
        total_out_sum = max(
            fixed_sum + max(0, int(round(sum(enc_w)))),
            fixed_sum,
            1,
        )

    if off_reason is not None:
        target_r_official = min(max(0, int(off_reason)), total_out_sum)
    else:
        target_r_official = max(0, em_sum + max(0, int(round(sum(enc_w)))))
        target_r_official = min(target_r_official, total_out_sum)

    # Cap visible TokZ first so Enc gets whatever remains.
    if fixed_sum > total_out_sum and fixed_sum > 0:
        scaled_fixed = _scale_ints(
            [
                float(out_thought[i] + out_message[i] + out_emit[i])
                for i in range(n)
            ],
            total_out_sum,
        )
        for i in range(n):
            bag = int(scaled_fixed[i])
            if bag <= 0:
                out_thought[i] = out_message[i] = out_emit[i] = 0
                continue
            parts = _scale_ints(
                [
                    float(out_thought[i]),
                    float(out_message[i]),
                    float(out_emit[i]),
                ],
                bag,
            )
            out_thought[i], out_message[i], out_emit[i] = (
                int(parts[0]),
                int(parts[1]),
                int(parts[2]),
            )
        th_sum = int(sum(out_thought))
        msg_sum = int(sum(out_message))
        em_sum = int(sum(out_emit))
        fixed_sum = int(th_sum + msg_sum + em_sum)
    elif total_out_sum <= 0:
        out_thought = [0] * n
        out_message = [0] * n
        out_emit = [0] * n
        th_sum = msg_sum = em_sum = fixed_sum = 0

    enc_pool = max(0, int(total_out_sum) - int(fixed_sum))
    # leftover → Enc, pro-rata Enc TokZ
    enc_scale = _enc_tokz_weights(
        n,
        enc_w,
        [th_w_list[i] + em_w_list[i] + msg_w_list[i] for i in range(n)],
    )
    out_reasoning = (
        _scale_ints(enc_scale, enc_pool) if enc_pool > 0 and sum(enc_scale) > 0 else [0] * n
    )

    got_fixed_enc = int(
        sum(out_thought) + sum(out_message) + sum(out_emit) + sum(out_reasoning)
    )
    if total_out_sum > 0 and got_fixed_enc != total_out_sum:
        need = total_out_sum - got_fixed_enc
        if need > 0:
            extra = (
                _scale_ints(enc_scale, need)
                if sum(enc_scale) > 0
                else [0] * n
            )
            if sum(extra) == 0:
                extra = [0] * n
                extra[-1] = need
            for i in range(n):
                out_reasoning[i] = int(out_reasoning[i]) + int(extra[i])
        else:
            need = -need
            for bag in (out_reasoning, out_emit, out_message, out_thought):
                if need <= 0:
                    break
                for i in sorted(range(n), key=lambda j: bag[j], reverse=True):
                    if need <= 0:
                        break
                    take = min(int(bag[i]), need)
                    bag[i] -= take
                    need -= take

    target_r = int(sum(out_emit) + sum(out_reasoning))
    pure_out = max(0, int(sum(out_message)))

    outputs = [0] * n
    for i in range(n):
        outputs[i] = (
            int(out_thought[i])
            + int(out_reasoning[i])
            + int(out_emit[i])
            + int(out_message[i])
        )
    out_got = int(sum(outputs))
    if total_out_sum > 0 and out_got != total_out_sum:
        d = total_out_sum - out_got
        if d > 0:
            extra = (
                _scale_ints(enc_scale, d) if sum(enc_scale) > 0 else [0] * n
            )
            if sum(extra) == 0:
                extra = [0] * n
                extra[-1] = d
            for i in range(n):
                out_reasoning[i] = int(out_reasoning[i]) + int(extra[i])
                outputs[i] = int(outputs[i]) + int(extra[i])
        else:
            need = -d
            for i in range(n - 1, -1, -1):
                for bag in (out_reasoning, out_emit, out_message, out_thought):
                    if need <= 0:
                        break
                    take = min(int(bag[i]), need)
                    bag[i] -= take
                    outputs[i] -= take
                    need -= take

    return {
        "out_thought": out_thought,
        "out_message": out_message,
        "out_emit": out_emit,
        "out_reasoning": out_reasoning,
        "outputs": outputs,
        "enc_w": enc_w,
        "th_w_list": th_w_list,
        "em_w_list": em_w_list,
        "msg_w_list": msg_w_list,
        "total_out_sum": total_out_sum,
        "target_r": target_r,
        "pure_out": pure_out,
        "target_r_official": target_r_official,
    }
