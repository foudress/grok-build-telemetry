"""Per model-step usage reconstruction (tokenizer-weighted calibration)."""

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


def _step_stream_start(step: dict[str, Any]) -> int:
    """Prompt size at stream start. Prefer stream_context_start over context_start."""
    if not isinstance(step, dict):
        return 0
    for key in ("stream_context_start", "context_start", "context_peak", "context_end"):
        v = step.get(key)
        if isinstance(v, int) and v >= 0:
            return int(v)
    return 0


def _step_stream_end(step: dict[str, Any]) -> int:
    """Prompt size at stream end. Prefer stream_context_end over context_end."""
    if not isinstance(step, dict):
        return 0
    for key in ("stream_context_end", "context_end", "context_peak"):
        v = step.get(key)
        if isinstance(v, int) and v >= 0:
            return int(v)
    return 0


def _step_total_input(step: dict[str, Any]) -> int:
    """totalinput for this LLM call = stream start (stream_context_start first)."""
    return _step_stream_start(step)


_HARNESS_IN_KINDS = ("tool", "late_context", "hook", "llm_to_in", "compact_out_in")


def _compact_out_tokens(compact: dict[str, Any]) -> int:
    for key in ("out_tokens", "tokens_after"):
        v = compact.get(key)
        try:
            if v is not None:
                n = int(v)
                if n > 0:
                    return n
        except (TypeError, ValueError):
            continue
    return 0


def _mid_round_split_points(
    steps: list[dict[str, Any]],
) -> list[tuple[int, dict[str, Any]]]:
    """(step_index, compact) where a mid-round compact has later steps."""
    out: list[tuple[int, dict[str, Any]]] = []
    n = len(steps)
    for i, s in enumerate(steps):
        if not isinstance(s, dict):
            continue
        cas = [
            c
            for c in (s.get("compacts_after") or [])
            if isinstance(c, dict) and c.get("kind") == "compaction"
        ]
        if cas and i + 1 < n:
            out.append((i, cas[-1]))
    return out


def _allocate_usage_across_segments(
    usage: Optional[dict[str, Any]],
    steps: list[dict[str, Any]],
    ranges: list[tuple[int, int]],
) -> list[Optional[dict[str, Any]]]:
    """Split official usage by stream-start weights (compact is not an extra call)."""
    n = len(ranges)
    if n <= 0:
        return []
    if not usage:
        return [None] * n
    weights = []
    for lo, hi in ranges:
        w = sum(_step_stream_start(steps[i]) for i in range(lo, hi))
        weights.append(float(max(0, w)))

    def og(*keys: str) -> Optional[int]:
        for k in keys:
            if k in usage and usage[k] is not None:
                try:
                    return int(usage[k])
                except (TypeError, ValueError):
                    return None
        return None

    off_in = og("inputTokens", "input_tokens")
    off_out = og("outputTokens", "output_tokens")
    off_cache = og("cachedReadTokens", "cached_read_tokens")
    off_reason = og("reasoningTokens", "reasoning_tokens")

    def split_field(total: Optional[int]) -> list[Optional[int]]:
        if total is None:
            return [None] * n
        if sum(weights) <= 0:
            out = [0] * n
            out[0] = int(total)
            return out
        return _scale_ints(weights, int(total))

    ins = split_field(off_in)
    caches = split_field(off_cache)
    outs = split_field(off_out)
    reasons = split_field(off_reason)
    slices: list[Optional[dict[str, Any]]] = []
    for i in range(n):
        sl = dict(usage)
        if off_in is not None:
            sl["inputTokens"] = int(ins[i] or 0)
        if off_cache is not None:
            cin = int(ins[i] or 0) if off_in is not None else int(caches[i] or 0)
            sl["cachedReadTokens"] = min(int(caches[i] or 0), max(0, cin))
        if off_out is not None:
            sl["outputTokens"] = int(outs[i] or 0)
        if off_reason is not None:
            sl["reasoningTokens"] = int(reasons[i] or 0)
        slices.append(sl)
    return slices


def _step_compact_out_in(step: dict[str, Any]) -> int:
    tot = 0
    if not isinstance(step, dict):
        return 0
    for ch in step.get("children") or []:
        if not isinstance(ch, dict) or ch.get("kind") != "phase_harness":
            continue
        for sub in ch.get("children") or []:
            if isinstance(sub, dict) and sub.get("kind") == "compact_out_in":
                tot += int(sub.get("tokens_in") or 0)
    return tot


def _inject_compact_out_in(step: dict[str, Any], compact: dict[str, Any]) -> bool:
    """First harness child: compacted history re-enters the next LLM prompt.

    Between-rounds: ``attribution=user`` (UI shows under User[N]; still here for
    reconstruct miss / rolling-cache math). Mid-round: ``attribution=harness``.
    """
    out_tok = _compact_out_tokens(compact)
    if out_tok <= 0 or not isinstance(step, dict):
        return False
    children = list(step.get("children") or [])
    harness = None
    for ch in children:
        if isinstance(ch, dict) and ch.get("kind") == "phase_harness":
            harness = ch
            break
    if harness is None:
        harness = {"kind": "phase_harness", "label": "Harness", "children": []}
        children.append(harness)
        step["children"] = children
    kids = list(harness.get("children") or [])
    if any(isinstance(c, dict) and c.get("kind") == "compact_out_in" for c in kids):
        return False
    tctx = step_tier_ctx(step, fallback=max(out_tok, 1))
    usd = float(_price_in(out_tok, tctx)) if out_tok > 0 else 0.0
    raw_attr = compact.get("_attribution") or compact.get("attribution")
    if raw_attr in ("user", "harness"):
        attribution = str(raw_attr)
    elif compact.get("placement") == "mid_round":
        attribution = "harness"
    else:
        attribution = "user"
    n = compact.get("n") or compact.get("index") or compact.get("compact_index")
    node = {
        "kind": "compact_out_in",
        "label": "Compact Out",
        "attribution": attribution,
        "compact_index": int(n) if isinstance(n, (int, float)) and int(n) > 0 else None,
        "tokens_in": int(out_tok),
        "tokenizer_tokens": int(out_tok),
        "context_delta": int(out_tok),
        "cost_in_usd": usd,
        "estimate_usd": usd,
        "estimate_note": (
            "Compacted history re-enters User In (between-rounds)."
            if attribution == "user"
            else (
                "Compacted history re-enters next LLM prompt "
                "(full window after compact)."
            )
        ),
    }
    kids.insert(0, node)
    harness["children"] = kids
    # Between-rounds (attribution=user): node kept for rolling-cache math only —
    # do not inflate Harness In display totals (User owns Compact Out).
    if attribution != "user":
        harness["tokens_in"] = int(harness.get("tokens_in") or 0) + int(out_tok)
        harness["cost_in_usd"] = float(harness.get("cost_in_usd") or 0) + usd
        harness["estimate_usd"] = float(harness.get("estimate_usd") or 0) + usd
    return True


def _merge_segment_recons(parts: list[dict[str, Any]]) -> dict[str, Any]:
    if not parts:
        return {
            "steps": [],
            "method": "empty",
            "calibrated": False,
            "totals": {},
            "breakdown": {},
            "bootstrap_residual_tokens": 0,
            "prior_context_tokens": None,
        }
    if len(parts) == 1:
        return parts[0]
    steps: list[dict[str, Any]] = []
    for p in parts:
        steps.extend(p.get("steps") or [])
    for i, s in enumerate(steps, 1):
        if isinstance(s, dict):
            s["index"] = i
    tot0 = dict(parts[0].get("totals") or {})
    tot: dict[str, Any] = {}
    keys = set()
    for p in parts:
        keys.update((p.get("totals") or {}).keys())
    for k in keys:
        vals = [(p.get("totals") or {}).get(k) for p in parts]
        nums = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
        if nums and len(nums) == len([v for v in vals if v is not None]):
            tot[k] = type(nums[0])(sum(nums)) if not isinstance(nums[0], bool) else sum(nums)
        else:
            tot[k] = tot0.get(k, vals[-1] if vals else None)
    bd: dict[str, Any] = {}
    bkeys: set[str] = set()
    for p in parts:
        bkeys.update((p.get("breakdown") or {}).keys())
    for k in bkeys:
        vals = [(p.get("breakdown") or {}).get(k) for p in parts]
        if k in ("user_cache_share_tokens", "user_cache_share_usd", "cold_round"):
            bd[k] = (parts[0].get("breakdown") or {}).get(k)
            continue
        if k == "last_cache_omitted_tokens":
            bd[k] = (parts[-1].get("breakdown") or {}).get(k)
            continue
        if k == "phys_raw_starts":
            merged: list[Any] = []
            for v in vals:
                if isinstance(v, list):
                    merged.extend(v)
            bd[k] = merged
            continue
        nums = [v for v in vals if isinstance(v, (int, float)) and not isinstance(v, bool)]
        nonempty = [v for v in vals if v is not None]
        if nums and len(nums) == len(nonempty):
            bd[k] = sum(nums)
        else:
            bd[k] = nonempty[0] if nonempty else None
    return {
        "steps": steps,
        "method": parts[-1].get("method") or parts[0].get("method"),
        "calibrated": any(bool(p.get("calibrated")) for p in parts),
        "totals": tot,
        "breakdown": bd,
        "bootstrap_residual_tokens": int(parts[0].get("bootstrap_residual_tokens") or 0),
        "note": parts[-1].get("note") or parts[0].get("note"),
        "prior_context_tokens": parts[0].get("prior_context_tokens"),
    }


def _reconstruct_compact_segments(
    steps: list[dict[str, Any]],
    *,
    split_at: list[tuple[int, dict[str, Any]]],
    official_usage: Optional[dict[str, Any]],
    prior_context_tokens: Optional[int],
    user_uncached_tokens: int,
    system_uncached_tokens: int,
    context_end_tokens: Optional[int],
    compact_reentry: Optional[dict[str, Any]],
    omit_last_cache: bool = True,
) -> dict[str, Any]:
    bounds = [0]
    reentries: list[Optional[dict[str, Any]]] = [compact_reentry]
    for idx, compact in split_at:
        nxt = idx + 1
        if nxt <= bounds[-1]:
            continue
        bounds.append(nxt)
        reentries.append(compact)
    bounds.append(len(steps))
    ranges = [(bounds[i], bounds[i + 1]) for i in range(len(bounds) - 1)]
    ranges = [(lo, hi) for lo, hi in ranges if hi > lo]
    while len(reentries) < len(ranges):
        reentries.append(None)
    reentries = reentries[: len(ranges)]
    usages = _allocate_usage_across_segments(official_usage, steps, ranges)
    parts: list[dict[str, Any]] = []
    for i, (lo, hi) in enumerate(ranges):
        if i == 0:
            seg_prior = prior_context_tokens
            seg_user = user_uncached_tokens
            seg_sys = system_uncached_tokens
        else:
            prev_c = reentries[i]
            after = None
            if isinstance(prev_c, dict):
                after = prev_c.get("tokens_after")
            try:
                seg_prior = int(after) if after is not None else None
            except (TypeError, ValueError):
                seg_prior = None
            seg_user = 0
            seg_sys = 0
        rec = reconstruct_model_step_usage(
            steps[lo:hi],
            official_usage=usages[i] if i < len(usages) else None,
            prior_context_tokens=seg_prior,
            user_uncached_tokens=int(seg_user or 0),
            system_uncached_tokens=int(seg_sys or 0),
            # Later segment Y = its stream ends, not tokens_after (that is X).
            context_end_tokens=context_end_tokens if i == 0 else None,
            compact_reentry=reentries[i],
            omit_last_cache=bool(omit_last_cache) and (i == len(ranges) - 1),
        )
        parts.append(rec)
    merged = _merge_segment_recons(parts)
    merged["method"] = (merged.get("method") or "") + "+compact_split"
    return merged


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


def _fit_tools_to_delta_ctx(
    *,
    tools_z: list[float],
    reentry: list[int],
    compact_per: list[int],
    user_in: int,
    delta_ctx: int,
) -> list[int]:
    """Spread (Δctx − user − reentry − tools_Z − compact) onto tool tokZ only."""
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


def _enc_tokz_weights(n: int, enc_w: list[float], visible_w: list[float]) -> list[float]:
    """Leftover Out → Enc. Prefer Enc TokZ; else uniform on calls with thought/out."""
    w = [float(enc_w[i]) if i < len(enc_w) else 0.0 for i in range(n)]
    if sum(w) > 0:
        return w
    w = [
        1.0 if (i < len(visible_w) and float(visible_w[i]) > 0) else 0.0
        for i in range(n)
    ]
    if sum(w) > 0:
        return w
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


def reconstruct_model_step_usage(
    steps: list[dict[str, Any]],
    *,
    official_usage: Optional[dict[str, Any]] = None,
    prior_context_tokens: Optional[int] = None,
    context_reread: bool = False,  # ignored; miss is leftover off_unc
    reread_uncached_tokens: int = 0,  # ignored; miss is leftover off_unc
    user_uncached_tokens: int = 0,
    system_uncached_tokens: int = 0,
    context_end_tokens: Optional[int] = None,
    compact_reentry: Optional[dict[str, Any]] = None,
    omit_last_cache: bool = True,
) -> dict[str, Any]:
    """
    Tokenizer-weighted per-call reconstruction (simple + fair).

    Sources:
      - stream_context_start / context_start (prefer stream) as prompt-size weights
      - chat_history / tool payloads → tokenized weights (xai-token-estimation
        bytes/4, or GROK_TOKENIZER=tiktoken)
      - turn_completed.usage → official input / cache / output / reasoning
        (SUM across modelCalls; Input−Cache = paid uncached)

    Rules:
      1. Display Cached is a rolling prefix (not a 2nd bill). C0 = prior
         (R1: System+User on Call 1; User Cached = 0). Call k Cached =
         Call(k−1) Cached + Call(k−1) In − compact_out_in (compact is
         already C0). Last call Cached = 0 (no next LLM send). Round
         header Cached tokens/$ stay official cachedRead.
      2. Tool tokZ fit to Δctx (warm: end−prior; R1: end−System). Never
         rescale User In, LLM Out reentry, or compact_out_in. No tools
         → leftover Δ is stream lag, not cache miss.
      3. Tree Call In = Harness In = reentry TokF + compact_out_in + fitted
         tools. Final call: Out → user (no Out→In); tools/hooks only.
      4. Round KV miss = max(0, off_unc − [System] − user − Σ harness).
         Not assigned to a call; not planted under User.
      5. Out: Thought/Message/ToolReq = exact TokZ; Reasoning[enc] =
         residual of full output after those TokZ, pro-rata by enc tokZ.
    """
    n = len(steps)
    bootstrap_residual_tokens = 0
    phys_raw: list[int] = []
    empty_totals = {
        "input": 0,
        "cached_read": 0,
        "uncached_input": 0,
        "output": 0,
        "cost_usd": 0.0,
        "cost_in_usd": 0.0,
        "cost_cached_usd": 0.0,
        "cost_out_usd": 0.0,
    }
    if n == 0:
        return {
            "steps": [],
            "method": "empty",
            "calibrated": False,
            "totals": empty_totals,
            "breakdown": {},
            "bootstrap_residual_tokens": 0,
            "prior_context_tokens": prior_context_tokens,
        }

    split_at = _mid_round_split_points(steps)
    if split_at:
        return _reconstruct_compact_segments(
            steps,
            split_at=split_at,
            official_usage=official_usage,
            prior_context_tokens=prior_context_tokens,
            user_uncached_tokens=user_uncached_tokens,
            system_uncached_tokens=system_uncached_tokens,
            context_end_tokens=context_end_tokens,
            compact_reentry=compact_reentry,
            omit_last_cache=omit_last_cache,
        )

    wts = _collect_step_weights(steps)
    total_inputs = list(wts["starts"])
    stream_ends = list(wts["ends"])
    raw_out_thought = list(wts["raw_out_thought"])
    raw_out_enc = list(wts["raw_out_enc"])
    raw_out_emit = list(wts["raw_out_emit"])
    raw_out_message = list(wts["raw_out_message"])
    harness_w = list(wts["harness_w"])
    tool_ws = list(wts["tool_ws"])
    phys_raw = list(total_inputs)

    off = official_usage or {}

    def og(*keys: str) -> Optional[int]:
        for k in keys:
            if k in off and off[k] is not None:
                try:
                    return int(off[k])
                except (TypeError, ValueError):
                    return None
        return None

    off_in = og("inputTokens", "input_tokens")
    off_out = og("outputTokens", "output_tokens")
    off_cache = og("cachedReadTokens", "cached_read_tokens")
    off_reason = og("reasoningTokens", "reasoning_tokens")
    calibrated = off_in is not None

    cold = not isinstance(prior_context_tokens, int) or int(prior_context_tokens) <= 0
    prior_i = (
        max(0, int(prior_context_tokens))
        if isinstance(prior_context_tokens, int) and prior_context_tokens > 0
        else 0
    )

    off_unc: Optional[int] = None
    if off_in is not None and off_cache is not None:
        off_unc = max(0, off_in - min(off_cache, off_in))
    elif off_in is not None:
        off_unc = off_in

    inputs, paid_uncached = _reconstruct_inputs_and_cache(
        n=n,
        starts=total_inputs,
        ends=stream_ends,
        harness_w=harness_w,
        raw_out_thought=raw_out_thought,
        raw_out_enc=raw_out_enc,
        raw_out_emit=raw_out_emit,
        raw_out_message=raw_out_message,
        off_in=off_in,
        off_unc=off_unc,
        cold=cold,
        prior_i=prior_i,
        user_uncached_tokens=int(user_uncached_tokens or 0),
    )
    logical_inputs = list(inputs)
    logical_uncached = list(paid_uncached)

    if cold and n and total_inputs[0] > 0 and off_in is not None:
        stream0 = int(total_inputs[0])
        if off_in > stream0 and off_cache is not None:
            bootstrap_residual_tokens = max(0, int(off_in) - sum(total_inputs))

    # --- Out model (UI + bill) ---
    _out = _reconstruct_output(
        n,
        raw_out_thought,
        raw_out_enc,
        raw_out_emit,
        raw_out_message,
        off_out,
        off_reason,
    )
    out_thought = _out["out_thought"]
    out_message = _out["out_message"]
    out_emit = _out["out_emit"]
    out_reasoning = _out["out_reasoning"]
    outputs = _out["outputs"]
    enc_w = _out["enc_w"]

    # Harness tree In = fixed LLM Out (re-enter) + tool pool (scaled separately).
    # Final call: Out → user (not next LLM); tools/hooks only.
    # Out→In mass (LLM Out [N] under Harness) = full billed Out TokF:
    #   Thought TokF + Reasoning TokF + Message TokF + ToolRequest TokF
    # (encrypted blob is server-replaced — do not use enc TokZ for re-entry.)
    enc_z_list = [max(0, int(round(enc_w[i]))) for i in range(n)]  # UI stamp only
    out_to_harness_in_w: list[float] = []
    for i in range(n):
        if n >= 2 and i < n - 1:
            out_as_in = float(
                int(out_thought[i])
                + int(out_reasoning[i])
                + int(out_message[i])
                + int(out_emit[i])
            )
        else:
            out_as_in = 0.0
        out_to_harness_in_w.append(out_as_in)

    reentry_toks = [int(round(x)) for x in out_to_harness_in_w]
    compact_per = [0] * n
    if compact_reentry:
        compact_per[0] = int(_compact_out_tokens(compact_reentry) or 0)
    user_i = max(0, int(user_uncached_tokens or 0))
    sys_i = max(0, int(system_uncached_tokens or 0))
    tools_z, tool_ws_real = _tools_z_from_steps(steps)
    tool_ws = tool_ws_real
    delta_ctx = _delta_ctx_for_fit(
        cold=cold,
        prior_i=prior_i,
        system_tokens=sys_i,
        context_end_tokens=context_end_tokens,
        stream_ends=stream_ends,
    )
    tools_toks = _fit_tools_to_delta_ctx(
        tools_z=tools_z,
        reentry=reentry_toks,
        compact_per=compact_per,
        user_in=user_i,
        delta_ctx=delta_ctx,
    )
    harness_toks = [
        int(tools_toks[i]) + int(reentry_toks[i]) + int(compact_per[i])
        for i in range(n)
    ]
    caches = _rolling_prefix_caches(
        n=n,
        cold=cold,
        prior_i=prior_i,
        system_tokens=sys_i,
        user_in=user_i,
        starts=total_inputs,
        harness_ins=harness_toks,
        compact_per=compact_per,
    )
    last_cache_omitted = 0
    if omit_last_cache:
        caches, last_cache_omitted = _omit_last_prefix(caches)
    logical_caches = list(caches)

    annotated: list[dict[str, Any]] = []
    tot_in = tot_cache = tot_out = 0
    tot_cost = tot_cost_in = tot_cost_cache = tot_cost_out = 0.0
    tot_harness_in = 0
    tot_harness_in_usd = 0.0
    tot_out_to_harness = 0
    tot_out_to_harness_usd = 0.0
    tot_out_to_harness_in = 0
    tot_out_to_harness_in_usd = 0.0
    tot_out_to_user = 0
    tot_out_to_user_usd = 0.0
    tot_reasoning = 0
    tot_reasoning_usd = 0.0
    tot_reason_budget = 0
    tot_reason_budget_usd = 0.0
    tot_thought_summary = 0
    tot_thought_summary_usd = 0.0
    tot_reasoning_enc = 0
    tot_reasoning_enc_usd = 0.0

    for i, step in enumerate(steps):
        in_t = inputs[i]
        cache_t = caches[i]
        out_t = outputs[i]
        paid_unc = int(paid_uncached[i])
        logical_in = logical_inputs[i]
        logical_cache = logical_caches[i]
        logical_unc = logical_uncached[i]
        paid_log_unc = logical_unc

        if isinstance(step, dict) and in_t > 0:
            step["calibrated_input_tokens"] = int(in_t)

        # Tier is per LLM call (this call's context_start) — never round peak
        # and never bump the whole call because context_end crossed 200k mid-gen.
        tier_ctx = step_tier_ctx(
            step if isinstance(step, dict) else None,
            fallback=logical_in if logical_in > 0 else max(in_t, 1),
        )

        est = estimate_cost_usd(
            input_tokens=in_t,
            output_tokens=out_t,
            cached_read_tokens=cache_t,
            peak_context_tokens=tier_ctx,
            model_calls=1,
        )
        paid_cost_in = _price_in(int(paid_unc), tier_ctx)
        cost_cache = _price_cache(cache_t, tier_ctx)
        cost_out = float(est["cost_usd"]["output"])
        cost_cache_logical = _price_cache(logical_cache, tier_ctx)

        th_tok = int(out_thought[i])
        re_tok = int(out_reasoning[i])  # Enc leftover
        em_tok = int(out_emit[i])
        msg_tok = int(out_message[i])
        reason_budget_tok = em_tok + re_tok
        reason_tok = re_tok
        th_usd, re_usd, em_usd, msg_usd = _fit_usd_parts(
            [
                _price_out(th_tok, tier_ctx),
                _price_out(re_tok, tier_ctx),
                _price_out(em_tok, tier_ctx),
                _price_out(msg_tok, tier_ctx),
            ],
            cost_out,
        )

        thought_chars = int(
            step.get("thought_summary_chars") or step.get("thought_chars") or 0
        )
        enc_chars = int(step.get("thought_encrypted_chars") or 0)
        message_chars = int(step.get("message_chars") or 0)
        emit_chars = sum(int(t.get("arg_chars") or 0) for t in (step.get("tools") or []))
        if emit_chars <= 0:
            emit_chars = max(0, int(step.get("model_emit_delta") or 0)) * 4

        tot_thought_summary += th_tok
        tot_thought_summary_usd += th_usd
        tot_reasoning_enc += re_tok
        tot_reasoning_enc_usd += re_usd
        tot_reasoning += reason_tok
        tot_reasoning_usd += re_usd
        tot_reason_budget += reason_budget_tok
        tot_reason_budget_usd += em_usd + re_usd
        # Attribution LLM→Harness Out = Σ Tool Request (not full Out re-entry)
        tot_out_to_harness += em_tok
        tot_out_to_harness_usd += em_usd
        # Attribution LLM→User = Message only
        tot_out_to_user += msg_tok
        tot_out_to_user_usd += msg_usd

        # Out→In fixed at billed Out; tools_share from residual-scaled pool
        out_in_tok = (
            int(round(out_to_harness_in_w[i]))
            if i < len(out_to_harness_in_w)
            else 0
        )
        tools_share = int(tools_toks[i]) if i < len(tools_toks) else 0
        h_tok = int(out_in_tok) + int(tools_share)

        tw = tool_ws[i] if i < len(tool_ws) else []
        if tools_share > 0 and tw and sum(tw) > 0:
            per_tool = _scale_ints(tw, tools_share)
        elif tools_share > 0 and (step.get("tools") or []):
            per_tool = _scale_ints([1.0] * len(step.get("tools") or []), tools_share)
        else:
            per_tool = [0] * len(step.get("tools") or [])

        out_in_usd = float(_price_in(out_in_tok, tier_ctx)) if out_in_tok > 0 else 0.0
        if out_in_tok > 0:
            tot_out_to_harness_in += out_in_tok
            tot_out_to_harness_in_usd += out_in_usd

        def annotate_child(
            ch: dict[str, Any],
            *,
            _i: int = i,
            _out_t: int = out_t,
            _cost_out: float = cost_out,
            _th: int = th_tok,
            _re: int = re_tok,
            _em: int = em_tok,
            _msg: int = msg_tok,
            _th_usd: float = th_usd,
            _re_usd: float = re_usd,
            _em_usd: float = em_usd,
            _msg_usd: float = msg_usd,
            _cache_t: int = cache_t,
            _cost_cache: float = cost_cache,
            _tier: int = tier_ctx,
            _h_tok: int = h_tok,
            _per_tool: list[int] = per_tool,
            _out_in_tok: int = out_in_tok,
            _out_in_usd: float = out_in_usd,
            _thought_chars: int = thought_chars,
            _enc_chars: int = enc_chars,
            _message_chars: int = message_chars,
            _emit_chars: int = emit_chars,
            _off_reason: Optional[int] = off_reason,
            _n: int = n,
            _enc_z: int = int(enc_z_list[i]) if i < len(enc_z_list) else 0,
            _th_f: int = int(out_thought[i]),
            _re_f: int = int(out_reasoning[i]),
            _msg_f: int = int(out_message[i]),
            _em_f: int = int(out_emit[i]),
        ) -> dict[str, Any]:
            ch = dict(ch)
            kind = ch.get("kind")

            if kind == "phase_llm":
                sub = [annotate_child(c) for c in (ch.get("children") or [])]
                ch["children"] = sub
                parts = _money_parts(tokens_out=_out_t, cost_out=_cost_out)
                ch.update(parts)
                ch["phase_summary"] = {
                    "reasoning": next(
                        (
                            {
                                "tokens_out": c.get("tokens_out"),
                                "cost_out_usd": c.get("cost_out_usd"),
                                "chars": c.get("chars") or c.get("encrypted_chars"),
                            }
                            for c in sub
                            if c.get("kind") == "reasoning"
                        ),
                        None,
                    ),
                    "thought": next(
                        (
                            {
                                "tokens_out": c.get("tokens_out"),
                                "cost_out_usd": c.get("cost_out_usd"),
                                "chars": c.get("chars") or c.get("summary_chars"),
                            }
                            for c in sub
                            if c.get("kind") == "thought"
                        ),
                        None,
                    ),
                    "tool_requests": next(
                        (
                            {
                                "tokens_out": c.get("tokens_out"),
                                "cost_out_usd": c.get("cost_out_usd"),
                                "chars": c.get("chars"),
                            }
                            for c in sub
                            if c.get("kind") in ("tool_requests", "tool_request")
                        ),
                        None,
                    ),
                    "message": next(
                        (
                            {
                                "tokens_out": c.get("tokens_out"),
                                "cost_out_usd": c.get("cost_out_usd"),
                                "chars": c.get("chars"),
                            }
                            for c in sub
                            if c.get("kind") == "message"
                        ),
                        None,
                    ),
                }
                return ch

            if kind == "phase_harness":
                sub = [annotate_child(c) for c in (ch.get("children") or [])]
                # Drop legacy residual only; keep llm_to_in (LLM Out → next In)
                sub = [
                    c
                    for c in sub
                    if c.get("kind") not in ("caused_in_residual",)
                ]
                has_tool_payload = False
                t_i = 0
                has_llm_to_in = False
                for c in sub:
                    ck = c.get("kind")
                    ev = str(c.get("event_name") or "").lower()
                    is_stop = ev in ("stop", "session_stop", "agent_stop")
                    if ck == "hook" and (c.get("to_user") or is_stop):
                        c["to_user"] = True
                        c["tokens_in"] = 0
                        c["cost_in_usd"] = 0.0
                        c["context_delta"] = int(c.get("context_delta") or 0)
                        c["tokens_cached"] = 0
                        c["cost_cached_usd"] = 0.0
                        c["estimate_usd"] = 0.0
                        c["estimate_note"] = (
                            c.get("estimate_note")
                            or "hook → user (not returned to LLM; not In/Cached)"
                        )
                        continue
                    if ck == "compact_out_in":
                        tin = int(c.get("tokens_in") or c.get("context_delta") or 0)
                        if tin <= 0:
                            continue
                        c["tokens_in"] = tin
                        c["context_delta"] = tin
                        c["tokenizer_tokens"] = int(c.get("tokenizer_tokens") or tin)
                        c["cost_in_usd"] = float(_price_in(tin, _tier))
                        c["estimate_usd"] = float(c["cost_in_usd"])
                        c.setdefault(
                            "estimate_note",
                            "Compacted history re-enters next LLM prompt "
                            "(full window after compact).",
                        )
                        continue
                    if ck == "llm_to_in":
                        has_llm_to_in = True
                        # Re-entry: all TokF (Thought + Reasoning + Message + ToolReq)
                        reentry_src = int(_th_f + _re_f + _msg_f + _em_f)
                        c["tokens_in"] = int(_out_in_tok)
                        c["context_delta"] = int(_out_in_tok)
                        c["tokens_out_source"] = int(reentry_src or _out_t or _out_in_tok)
                        c["tokenizer_tokens"] = int(reentry_src or _out_in_tok)
                        c["reentry_thought_tokf"] = int(_th_f)
                        c["reentry_reasoning_tokf"] = int(_re_f)
                        c["reentry_message_tokf"] = int(_msg_f)
                        c["reentry_toolreq_tokf"] = int(_em_f)
                        c["enc_tokenizer_tokens"] = int(_enc_z)
                        c["enc_billed_tokens"] = int(_re_f)
                        c["cost_in_usd"] = float(_out_in_usd)
                        c["estimate_usd"] = float(_out_in_usd)
                        c["estimate_note"] = (
                            "LLM Out → next-call In = "
                            f"Thought TokF({_th_f}) + Reasoning TokF({_re_f}) + "
                            f"Message TokF({_msg_f}) + ToolReq TokF({_em_f}). "
                            "Encrypted blob is server-replaced; use billed TokF."
                        )
                        c["final_to_user"] = bool(_i >= _n - 1 and _n >= 2)
                        continue
                    if ck == "tool":
                        has_tool_payload = True
                        show = int(_per_tool[t_i]) if t_i < len(_per_tool) else 0
                        floor = int(
                            c.get("ch_result_tokens")
                            or c.get("tokenizer_tokens")
                            or c.get("result_tokens_est")
                            or 0
                        )
                        name = str(c.get("name") or "").lower()
                        waitish = (
                            "get_command" in name
                            or name in (
                                "spawn_subagent",
                                "kill_command_or_subagent",
                            )
                        )
                        # Peel of child session bills must not zero/shrink the
                        # parent-facing wait result shown on this tool line.
                        if waitish and floor > show:
                            show = floor
                        elif show <= 0 and floor > 0:
                            show = floor
                        c["tokens_in"] = int(show)
                        c["context_delta"] = int(show)
                        if show > 0:
                            c["cost_in_usd"] = float(_price_in(int(show), _tier))
                            c["estimate_usd"] = float(c["cost_in_usd"])
                        c["estimate_note"] = (
                            f"tool result → In (tokenizer {tokenizer_mode()}) "
                            "prorata of Δctx leftover"
                        )
                        t_i += 1
                    elif ck in ("late_context",) or (
                        ck == "hook" or c.get("name")
                    ):
                        if ck != "hook":
                            has_tool_payload = True
                # Ensure llm_to_in exists when we have Out→In mass
                if _out_in_tok > 0 and not has_llm_to_in:
                    reentry_src = int(_th_f + _re_f + _msg_f + _em_f)
                    injected = {
                        "kind": "llm_to_in",
                        "label": f"LLM Out [{_i + 1}]",
                        "call_index": _i + 1,
                        "tool_summary": "",
                        "tokens_in": int(_out_in_tok),
                        "context_delta": int(_out_in_tok),
                        "tokens_out_source": int(reentry_src or _out_t or _out_in_tok),
                        "tokenizer_tokens": int(reentry_src or _out_in_tok),
                        "reentry_thought_tokf": int(_th_f),
                        "reentry_reasoning_tokf": int(_re_f),
                        "reentry_message_tokf": int(_msg_f),
                        "reentry_toolreq_tokf": int(_em_f),
                        "enc_tokenizer_tokens": int(_enc_z),
                        "enc_billed_tokens": int(_re_f),
                        "cost_in_usd": float(_out_in_usd),
                        "estimate_usd": float(_out_in_usd),
                        "estimate_note": (
                            "LLM Out → next-call In "
                            "(Thought + Reasoning + Message + ToolReq TokF)"
                        ),
                    }
                    sub.insert(0, injected)
                    has_llm_to_in = True
                # Drop empty Out→In on final / zero
                sub = [
                    c
                    for c in sub
                    if not (
                        c.get("kind") == "llm_to_in"
                        and int(c.get("tokens_in") or 0) <= 0
                    )
                ]
                tok_in = sum(
                    int(c.get("tokens_in") or 0)
                    for c in sub
                    if not c.get("to_user")
                    and c.get("kind")
                    in _HARNESS_IN_KINDS
                )
                if (has_tool_payload or has_llm_to_in) and tok_in <= 0:
                    tok_in = _h_tok
                usd_in = _price_in(tok_in, _tier)
                in_children = [
                    c
                    for c in sub
                    if not c.get("to_user")
                    and (
                        c.get("kind") in _HARNESS_IN_KINDS
                        or c.get("name")
                    )
                ]
                if in_children and tok_in > 0:
                    fitted = _fit_usd_parts(
                        [
                            float(c.get("tokens_in") or c.get("context_delta") or 0)
                            for c in in_children
                        ],
                        usd_in,
                    )
                    for c, u in zip(in_children, fitted):
                        c["cost_in_usd"] = float(u)
                        c["estimate_usd"] = float(
                            u
                            + float(c.get("cost_cached_usd") or 0)
                            + float(c.get("cost_out_usd") or 0)
                        )
                hook_only = not has_tool_payload and not (
                    has_llm_to_in and _out_in_tok > 0
                )
                h_cache_t = 0 if hook_only else int(_cache_t)
                h_cache_usd = 0.0 if hook_only else float(_cost_cache)
                parts = _money_parts(
                    tokens_in=tok_in,
                    cost_in=usd_in,
                    tokens_cached=h_cache_t,
                    cost_cached=h_cache_usd,
                )
                ch.update(parts)
                ch["display_cached_tokens"] = h_cache_t
                ch["display_cached_usd"] = float(h_cache_usd)
                ch["estimate_usd"] = float(usd_in)
                ch["hook_only"] = bool(hook_only)
                ch["final_to_user"] = bool(hook_only and _i >= _n - 1)
                ch["llm_out_in_tokens"] = int(_out_in_tok)
                ch["llm_out_in_usd"] = float(_out_in_usd)
                ch["cached_note"] = (
                    "Hook-only harness (internal / → user): no Cached, no next LLM."
                    if hook_only
                    else (
                        "Cached = rolling prefix at this call (last included). "
                        "Harness In = LLM Out (re-enter) + tool results."
                    )
                )
                ch["children"] = sub
                return ch

            if kind == "reasoning":
                parts = _money_parts(tokens_out=_re, cost_out=_re_usd)
                ch.update(parts)
                ch["chars"] = _enc_chars
                ch["encrypted_chars"] = _enc_chars
                # TokZ = real encrypted_content tokenizer stamp (not chars//4)
                ch["encrypted_tokens"] = int(_enc_z)
                ch["tokenizer_tokens"] = int(_enc_z)
                ch["estimate_output_tokens"] = _re
                ch["estimate_note"] = (
                    "Encrypted residual of full output: "
                    "off_out − tokZ(Thought) − tokZ(Message) − tokZ(ToolRequest), "
                    f"pro-rata by enc tokZ (tokenizer {tokenizer_mode()}). "
                    f"TokZ={_enc_z} from encrypted_content (not chars//4={_enc_chars // 4}). "
                    "Absorbs leftover pure Out + official reason mass."
                )
                ch["reasoning_tokens_official"] = _off_reason
                return ch

            if kind == "thought":
                # Exact TokZ billed as Out (scale only if fixed TokZ > off_out)
                th_show = int(ch.get("summary_tokens") or _th)
                if th_show <= 0:
                    th_show = _th
                parts = _money_parts(tokens_out=_th, cost_out=_th_usd)
                ch.update(parts)
                ch["chars"] = _thought_chars
                ch["summary_chars"] = _thought_chars
                ch["summary_tokens"] = int(ch.get("summary_tokens") or th_show)
                ch["encrypted_chars"] = 0
                ch["estimate_output_tokens"] = _th
                ch["tokenizer_tokens"] = int(ch.get("summary_tokens") or th_show)
                ch["estimate_note"] = (
                    f"Thought summary — exact TokZ billed as Out "
                    f"(tokenizer {tokenizer_mode()})."
                )
                return ch

            if kind == "message":
                parts = _money_parts(tokens_out=_msg, cost_out=_msg_usd)
                ch.update(parts)
                ch["chars"] = _message_chars
                ch["estimate_output_tokens"] = _msg
                ch["estimate_note"] = (
                    f"assistant.content — exact TokZ billed as Out "
                    f"(tokenizer {tokenizer_mode()})."
                )
                return ch

            if kind in ("tool_request", "tool_requests"):
                # Per-tool line: use this tool's arg_tokens_est; aggregate uses _em
                if kind == "tool_request":
                    arg_tok = int(
                        ch.get("arg_tokens_est")
                        or ch.get("tokenizer_tokens")
                        or 0
                    )
                    # Billed share: scale this tool's weight within step emit total
                    tools_list = step.get("tools") or []
                    weights = [
                        float(
                            max(
                                0,
                                int(t.get("arg_tokens_est") or 0)
                                or int(t.get("arg_chars") or 0) // 4,
                            )
                        )
                        for t in tools_list
                    ]
                    if _em > 0 and sum(weights) > 0 and tools_list:
                        # find index by tool_call_id / tool_seq
                        idx_t = None
                        tid = ch.get("tool_call_id")
                        tseq = ch.get("tool_seq")
                        for j, t in enumerate(tools_list):
                            if tid and t.get("tool_call_id") == tid:
                                idx_t = j
                                break
                            if tseq is not None and t.get("tool_seq") == tseq:
                                idx_t = j
                                break
                        per = _scale_ints(weights, _em)
                        show = int(per[idx_t]) if idx_t is not None else arg_tok
                        # usd share of em_usd
                        if sum(per) > 0 and idx_t is not None:
                            usd = _em_usd * (per[idx_t] / sum(per))
                        else:
                            usd = _price_out(show, _tier)
                        parts = _money_parts(tokens_out=show, cost_out=usd)
                        ch.update(parts)
                        ch["estimate_output_tokens"] = show
                        ch["tokenizer_tokens"] = arg_tok
                        ch["chars"] = int(ch.get("arg_chars") or 0)
                    else:
                        parts = _money_parts(
                            tokens_out=arg_tok, cost_out=_price_out(arg_tok, _tier)
                        )
                        ch.update(parts)
                        ch["estimate_output_tokens"] = arg_tok
                        ch["tokenizer_tokens"] = arg_tok
                else:
                    parts = _money_parts(tokens_out=_em, cost_out=_em_usd)
                    ch.update(parts)
                    ch["chars"] = _emit_chars
                    ch["estimate_output_tokens"] = _em
                ch["estimate_note"] = (
                    f"tool request arguments (chat_history tool_calls[].arguments "
                    f"preferred) — tokenizer({tokenizer_mode()}) definitive; "
                    "inside reasoningTokens (ToolReq + Enc)."
                )
                return ch

            if kind == "hook":
                delta = max(0, int(ch.get("context_delta") or ch.get("tokens_in") or 0))
                usd = _price_in(delta, _tier)
                parts = _money_parts(tokens_in=delta, cost_in=usd)
                ch.update(parts)
                ch["context_delta"] = delta
                ch["estimate_note"] = ch.get("estimate_note") or (
                    "hook_execution → In (JSON payload)"
                )
                return ch

            if kind == "llm_to_in":
                # Overwritten in phase_harness with billed Out share; keep provisional
                delta = max(0, int(ch.get("tokens_in") or ch.get("context_delta") or 0))
                usd = _price_in(delta, _tier)
                parts = _money_parts(tokens_in=delta, cost_in=usd)
                ch.update(parts)
                ch["context_delta"] = delta
                ch["estimate_note"] = ch.get("estimate_note") or (
                    "LLM Out → next-call In"
                )
                return ch

            if kind == "compact_out_in":
                delta = max(0, int(ch.get("tokens_in") or ch.get("context_delta") or 0))
                usd = _price_in(delta, _tier)
                parts = _money_parts(tokens_in=delta, cost_in=usd)
                ch.update(parts)
                ch["context_delta"] = delta
                ch["tokenizer_tokens"] = int(ch.get("tokenizer_tokens") or delta)
                ch["estimate_note"] = ch.get("estimate_note") or (
                    "Compacted history re-enters next LLM prompt "
                    "(full window after compact)."
                )
                return ch

            if kind in (
                "tool",
                "late_context",
                "caused_in_residual",
            ) or ch.get("name"):
                delta = max(0, int(ch.get("context_delta") or ch.get("tokens_in") or 0))
                usd = _price_in(delta, _tier)
                parts = _money_parts(tokens_in=delta, cost_in=usd)
                ch.update(parts)
                ch["context_delta"] = delta
                if kind == "tool":
                    ch["estimate_note"] = (
                        f"tool result → In (tokenizer {tokenizer_mode()} "
                        "prorata of Δctx leftover)"
                    )
                elif kind == "late_context":
                    ch["estimate_note"] = "late residual → In (paid next)"
                else:
                    ch["estimate_note"] = ch.get("estimate_note") or "context growth → In"
                return ch

            ch.setdefault("estimate_usd", float(ch.get("estimate_usd") or 0))
            return ch

        children_out = [annotate_child(c) for c in (step.get("children") or [])]

        if th_tok > 0 or re_tok > 0 or em_tok > 0 or thought_chars > 0 or msg_tok > 0:
            for ch in children_out:
                if ch.get("kind") != "phase_llm":
                    continue
                sub = list(ch.get("children") or [])
                has_re = any(c.get("kind") == "reasoning" for c in sub)
                has_th = any(c.get("kind") == "thought" for c in sub)
                has_msg = any(c.get("kind") == "message" for c in sub)
                has_em = any(
                    c.get("kind") in ("tool_request", "tool_requests") for c in sub
                )
                # Order: Thought → Reasoning[enc] → Message → Tool request[id]…
                if not has_th and (th_tok > 0 or thought_chars > 0):
                    th_node = {
                        "kind": "thought",
                        "chars": thought_chars,
                        "summary_chars": thought_chars,
                        "summary_tokens": int(
                            step.get("thought_summary_tokens") or th_tok
                        ),
                        "preview": step.get("thought_preview"),
                        "label": "Thought",
                    }
                    th_node.update(_money_parts(tokens_out=th_tok, cost_out=th_usd))
                    th_node["estimate_output_tokens"] = th_tok
                    th_node["estimate_note"] = "Thought — exact TokZ billed as Out"
                    sub.insert(0, th_node)
                if not has_re and re_tok > 0:
                    re_node = {
                        "kind": "reasoning",
                        "chars": enc_chars,
                        "encrypted_chars": enc_chars,
                        "label": "Reasoning",
                    }
                    re_node.update(_money_parts(tokens_out=re_tok, cost_out=re_usd))
                    re_node["estimate_output_tokens"] = re_tok
                    re_node["estimate_note"] = (
                        "Encrypted residual = off_out − Thought − Message − ToolReq"
                    )
                    # after thought
                    idx = 0
                    for j, c in enumerate(sub):
                        if c.get("kind") == "thought":
                            idx = j + 1
                    sub.insert(idx, re_node)
                for c in sub:
                    if c.get("kind") == "reasoning":
                        c["chars"] = int(enc_chars)
                        c["encrypted_chars"] = int(enc_chars)
                if not has_msg and msg_tok > 0:
                    msg_node = {
                        "kind": "message",
                        "chars": message_chars,
                        "message_tokens": int(step.get("message_tokens") or msg_tok),
                        "preview": step.get("message_preview"),
                    }
                    msg_node.update(_money_parts(tokens_out=msg_tok, cost_out=msg_usd))
                    msg_node["estimate_output_tokens"] = msg_tok
                    idx = 0
                    for j, c in enumerate(sub):
                        if c.get("kind") in ("thought", "reasoning"):
                            idx = j + 1
                    sub.insert(idx, msg_node)
                if not has_em and em_tok > 0:
                    tools_list = step.get("tools") or []
                    if tools_list:
                        weights = [
                            float(
                                max(
                                    0,
                                    int(t.get("arg_tokens_est") or 0)
                                    or int(t.get("arg_chars") or 0) // 4,
                                )
                            )
                            for t in tools_list
                        ]
                        per = (
                            _scale_ints(weights, em_tok)
                            if sum(weights) > 0
                            else _scale_ints([1.0] * len(tools_list), em_tok)
                        )
                        for j, t in enumerate(tools_list):
                            is_plan = bool(t.get("is_plan") or t.get("plan"))
                            em_node = {
                                "kind": "tool_request",
                                "label": "plan request" if is_plan else "tool request",
                                "name": t.get("name"),
                                "tool_call_id": t.get("tool_call_id"),
                                "tool_seq": t.get("tool_seq"),
                                "arg_chars": int(t.get("arg_chars") or 0),
                                "arg_tokens_est": int(t.get("arg_tokens_est") or 0),
                                "tokenizer_tokens": int(t.get("arg_tokens_est") or 0),
                                "path": t.get("path"),
                                "plan": t.get("plan"),
                                "is_plan": is_plan,
                            }
                            show = int(per[j]) if j < len(per) else 0
                            usd = (
                                em_usd * (per[j] / sum(per))
                                if sum(per) > 0
                                else 0.0
                            )
                            em_node.update(
                                _money_parts(tokens_out=show, cost_out=usd)
                            )
                            em_node["estimate_output_tokens"] = show
                            sub.append(em_node)
                    else:
                        em_node = {
                            "kind": "tool_requests",
                            "label": "tool request",
                            "chars": emit_chars,
                        }
                        em_node.update(
                            _money_parts(tokens_out=em_tok, cost_out=em_usd)
                        )
                        em_node["estimate_output_tokens"] = em_tok
                        sub.append(em_node)
                # Force stable UI order
                order = {
                    "thought": 0,
                    "reasoning": 1,
                    "message": 2,
                    "tool_request": 3,
                    "tool_requests": 3,
                }
                sub.sort(key=lambda c: order.get(str(c.get("kind")), 9))
                ch["children"] = sub
                break

        harness_in_tok = 0
        harness_in_usd = 0.0
        tools_out: list[dict[str, Any]] = []
        for c in children_out:
            if c.get("kind") == "phase_harness":
                harness_in_tok += int(c.get("tokens_in") or 0)
                harness_in_usd += float(c.get("cost_in_usd") or 0)
                for sub in c.get("children") or []:
                    if sub.get("kind") == "tool":
                        tools_out.append(sub)
            elif c.get("kind") == "tool":
                tools_out.append(c)

        if harness_in_tok <= 0 and h_tok > 0:
            harness_in_tok = h_tok
            harness_in_usd = float(_price_in(h_tok, tier_ctx))

        unc_t = int(harness_in_tok)
        cost_in = float(harness_in_usd)
        if unc_t > 0 and cost_in <= 0:
            cost_in = float(_price_in(unc_t, tier_ctx))
        logical_unc = int(unc_t)
        cost_in_logical = float(cost_in)

        final_to_user = i == n - 1 and n >= 2
        paid_cache_t = int(cache_t)
        paid_cost_cache = float(cost_cache)
        # API bill @ stream start (paid uncached + cache + out) — session accounting
        api_bill = float(paid_cost_in) + float(paid_cost_cache) + float(cost_out)
        api_usd = api_bill
        # White line total on LLM call = sum of *displayed* parts (tree In + Cached + Out)
        cost = float(cost_in) + float(cost_cache) + float(cost_out)

        tot_in += in_t
        tot_cache += paid_cache_t
        tot_out += out_t
        tot_cost += api_bill
        tot_cost_in += paid_cost_in
        tot_cost_cache += paid_cost_cache
        tot_cost_out += cost_out
        tot_harness_in += harness_in_tok
        tot_harness_in_usd += harness_in_usd

        late = int(step.get("late_context_delta") or 0)
        late_absorbed = int(step.get("late_absorbed") or 0)
        late_total = max(late, late_absorbed) if late_absorbed else late
        late_display = int(step.get("late_context_delta_display") or 0)

        tools_only_in = sum(
            int(t.get("tokens_in") or t.get("context_delta") or 0) for t in tools_out
        )
        # Bar "harness" = full Call In (tree In), not tools-only / not minus Out→In
        call_in_bar = int(unc_t) if unc_t > 0 else int(harness_in_tok)
        if call_in_bar <= 0:
            call_in_bar = int(out_in_tok) + int(tools_share)
        step_comp = dict(step.get("composition") or {})
        step_comp.update(
            {
                "thought_out": th_tok,
                "thought_summary_out": th_tok,
                "reasoning_encrypted_out": re_tok,
                "reason_budget_out": reason_budget_tok,
                "model_emit": em_tok,
                "message_out": msg_tok,
                "output_total": out_t,
                "llm_out_to_in": int(out_in_tok),
                "harness_tools_only": max(0, int(tools_share) or int(tools_only_in)),
                # Full Call In for repartition bar
                "harness_results": max(0, call_in_bar),
                "harness_in_total": int(harness_in_tok),
                # late always absorbed into tools — never a bar/UI residual
                "late_residual": 0,
            }
        )

        # Display Δctx is this call's stream window (end−start), not fitted
        # harness In (that is the tool-fit target for the round/segment).
        raw_delta = 0
        s0, s1 = _step_stream_start(step), _step_stream_end(step)
        if s1 > 0:
            raw_delta = max(0, s1 - s0)
        raw_h = (
            int(max(0, round(float(harness_w[i]))))
            if i < len(harness_w)
            else 0
        )
        if raw_h <= 0:
            raw_h = max(0, int(tools_only_in), int(late_display))
        if raw_delta > 0:
            growth_est = max(raw_delta, int(out_t))
        else:
            growth_est = max(0, int(out_t) + int(raw_h))

        step_ann = dict(step)
        step_ann["composition"] = step_comp
        step_ann["tier_context_tokens"] = int(tier_ctx)
        step_ann["tier"] = pick_tier(tier_ctx)["name"]
        step_ann["tokens_in"] = int(unc_t)
        step_ann["cost_in_usd"] = float(cost_in)
        step_ann["tokens_cached"] = int(cache_t)
        step_ann["cost_cached_usd"] = float(cost_cache)
        step_ann["tokens_out"] = int(out_t)
        step_ann["cost_out_usd"] = float(cost_out)
        # White total = In + Cached + Out on the call line (tree/harness In)
        step_ann["estimate_usd"] = float(cost)
        step_ann["cost_of_call_usd"] = float(cost)
        # True API paid@start bill (may differ when In is caused/harness-only)
        step_ann["api_call_usd"] = float(api_usd)
        step_ann["in_attribution"] = "tokenizer_prorata_harness"
        step_ann["harness_in_tokens"] = int(harness_in_tok)
        step_ann["harness_in_usd"] = float(harness_in_usd)
        step_ann["llm_out_in_tokens"] = int(out_in_tok)
        step_ann["llm_out_in_usd"] = float(out_in_usd)
        if final_to_user:
            step_ann["final_to_user"] = True
            step_ann["in_note"] = (
                "Final call of the round → user. Cached stays. "
                "(In = harness tools only; Out → user, not next LLM)."
            )
        step_ann["context_growth_est"] = int(growth_est)
        step_ann["context_growth_raw"] = int(raw_delta)
        step_ann["harness_pool_unscaled"] = int(raw_h)
        step_ann["paid_at_start_tokens"] = int(paid_unc)
        step_ann["paid_at_start_usd"] = float(paid_cost_in)
        step_ann["estimate"] = {
            "input_tokens": in_t,
            "cached_read_tokens": cache_t,
            "uncached_input_tokens": int(unc_t),
            "logical_input_tokens": logical_in,
            "logical_cached_tokens": logical_cache,
            "logical_uncached_tokens": int(logical_unc),
            "paid_uncached_tokens": int(paid_unc),
            "paid_logical_uncached_tokens": int(paid_log_unc),
            "context_growth_est": int(growth_est),
            "context_growth_raw": int(raw_delta),
            "harness_pool_unscaled": int(raw_h),
            "prior_context_tokens": (
                int(prior_context_tokens)
                if i == 0 and isinstance(prior_context_tokens, int)
                else (int(caches[i - 1]) if i > 0 else logical_cache)
            ),
            "late_tokens": late_total,
            "output_tokens": out_t,
            "output_thought_tokens": th_tok,
            "output_reasoning_tokens": re_tok,
            "output_reason_total_tokens": reason_tok,
            "output_reason_budget_tokens": reason_budget_tok,
            "output_emit_tokens": em_tok,
            "output_message_tokens": msg_tok,
            "tier": est["tier"],
            "cost_usd": {
                "uncached_input": float(paid_cost_in),
                "cached_input": float(cost_cache),
                "output": float(cost_out),
                "input": float(paid_cost_in),
                "total": float(cost),
                "api_total": float(api_usd),
                "caused_uncached_input": float(cost_in),
            },
            "cost_in_usd": float(cost_in),
            "cost_paid_in_usd": float(paid_cost_in),
            "cost_cached_usd": float(cost_cache),
            "cost_out_usd": float(cost_out),
            "cost_in_logical_usd": float(cost_in_logical),
            "cost_cached_logical_usd": float(cost_cache_logical),
            "estimate_usd": float(cost),
            "api_call_usd": float(api_usd),
            "cost_of_call_usd": float(cost),
            "llm_phase_usd": float(cost_out),
            "harness_in_tokens": harness_in_tok,
            "harness_in_usd": float(harness_in_usd),
            "prompt_context": logical_in,
            "method": (
                "tokenizer_prorata_calibrated"
                if calibrated
                else "tokenizer_prorata_proxy"
            ),
            "tokenizer": tokenizer_mode(),
            "in_note": (
                "Tree In (call) = Harness In = reentry TokF + compact_out_in + tools. "
                "Cached = rolling prefix (last call keeps it). "
                "White estimate_usd = In + Cached + Out (displayed). "
                "api_call_usd = paid@start uncached + cache + out."
            ),
        }
        step_ann["children"] = children_out
        step_ann["tools"] = tools_out
        annotated.append(step_ann)

    if compact_reentry and annotated:
        inserted = _inject_compact_out_in(annotated[0], compact_reentry)
        cot = _compact_out_tokens(compact_reentry) if inserted else 0
        if cot > 0:
            tctx0 = step_tier_ctx(annotated[0], fallback=max(cot, 1))
            cot_usd = float(_price_in(cot, tctx0))
            # Miss math still needs Compact Out in Σ harness-side uncached.
            tot_harness_in = int(tot_harness_in) + int(cot)
            tot_harness_in_usd = float(tot_harness_in_usd) + cot_usd
            raw_attr = (
                compact_reentry.get("_attribution")
                or compact_reentry.get("attribution")
            )
            user_owned = (
                raw_attr == "user"
                or (
                    raw_attr not in ("harness",)
                    and compact_reentry.get("placement") != "mid_round"
                )
            )
            s0 = annotated[0]
            if not user_owned:
                s0["harness_in_tokens"] = int(s0.get("harness_in_tokens") or 0) + int(cot)
                s0["harness_in_usd"] = float(s0.get("harness_in_usd") or 0) + cot_usd
                s0["tokens_in"] = int(s0.get("tokens_in") or 0) + int(cot)
                s0["cost_in_usd"] = float(s0.get("cost_in_usd") or 0) + cot_usd
                est0 = dict(s0.get("estimate") or {})
                est0["uncached_input_tokens"] = int(s0["tokens_in"])
                est0["logical_uncached_tokens"] = int(s0["tokens_in"])
                est0["harness_in_tokens"] = int(s0["harness_in_tokens"])
                est0["harness_in_usd"] = float(s0["harness_in_usd"])
                est0["cost_in_usd"] = float(s0["cost_in_usd"])
                s0["estimate"] = est0
            else:
                # Display Call/Harness In excludes User-owned Compact Out.
                s0["user_compact_out_tokens"] = int(cot)
                s0["user_compact_out_usd"] = float(cot_usd)
                est0 = dict(s0.get("estimate") or {})
                est0["user_compact_out_tokens"] = int(cot)
                est0["user_compact_out_usd"] = float(cot_usd)
                s0["estimate"] = est0

    if annotated:
        roll_ins = [int(s.get("tokens_in") or 0) for s in annotated]
        roll_comp = [_step_compact_out_in(s) for s in annotated]
        caches = _rolling_prefix_caches(
            n=len(annotated),
            cold=cold,
            prior_i=prior_i,
            system_tokens=sys_i,
            user_in=user_i,
            starts=total_inputs,
            harness_ins=roll_ins,
            compact_per=roll_comp,
        )
        if omit_last_cache:
            caches, last_cache_omitted = _omit_last_prefix(caches)
        logical_caches = list(caches)
        for s, c in zip(annotated, caches):
            s["tokens_cached"] = int(c)
            est = dict(s.get("estimate") or {})
            est["cached_read_tokens"] = int(c)
            est["logical_cached_tokens"] = int(c)
            s["estimate"] = est

    tools_only_sum = int(sum(int(x) for x in tools_toks)) if tools_toks else 0
    miss_base = int(off_unc) if off_unc is not None else 0
    if cold:
        cache_miss_tok = max(
            0, miss_base - int(sys_i) - int(user_i) - int(tot_harness_in)
        )
    else:
        cache_miss_tok = max(0, miss_base - int(user_i) - int(tot_harness_in))
    if off_unc is None:
        cache_miss_tok = 0
    user_cache_share = 0 if cold or cache_miss_tok > 0 else int(prior_i or 0)

    cache_display_sum = (
        int(sum(int(s.get("tokens_cached") or 0) for s in annotated)) if annotated else 0
    )
    # Price each call's *own* prefix. Do not scale tokens so Σ tree Cached
    # equals official cachedRead — round header stays official off_c.
    call_cache_tok = int(cache_display_sum)
    user_cache_usd = 0.0
    call_cache_usd = float(tot_cost_cache)
    if off_cache is not None:
        if annotated:
            tot_cost_cache = 0.0
            tot_cost = 0.0
            for s in annotated:
                try:
                    c_tok = int(s.get("tokens_cached") or 0)
                except (TypeError, ValueError):
                    c_tok = 0
                tctx = step_tier_ctx(s, fallback=max(c_tok, 1))
                c_usd = float(_price_cache(c_tok, tctx))
                s["tokens_cached"] = int(c_tok)
                s["cost_cached_usd"] = c_usd
                s["tier_context_tokens"] = int(tctx)
                s["tier"] = pick_tier(tctx)["name"]
                paid_in = float(s.get("paid_at_start_usd") or 0)
                out_usd = float(s.get("cost_out_usd") or 0)
                in_usd = float(s.get("cost_in_usd") or 0)
                api_bill = paid_in + c_usd + out_usd
                line_bill = in_usd + c_usd + out_usd
                s["estimate_usd"] = float(line_bill)
                s["cost_of_call_usd"] = float(line_bill)
                s["api_call_usd"] = float(api_bill)
                est = dict(s.get("estimate") or {})
                est["cached_read_tokens"] = int(c_tok)
                est["cost_cached_usd"] = c_usd
                est["estimate_usd"] = float(line_bill)
                est["cost_of_call_usd"] = float(line_bill)
                est["api_call_usd"] = float(api_bill)
                est["tier_context_tokens"] = int(tctx)
                est["tier"] = pick_tier(tctx)["name"]
                if isinstance(est.get("cost_usd"), dict):
                    est["cost_usd"] = dict(est["cost_usd"])
                    est["cost_usd"]["cached_input"] = float(c_usd)
                    est["cost_usd"]["total"] = float(line_bill)
                    est["cost_usd"]["api_total"] = float(api_bill)
                s["estimate"] = est
                for ch in s.get("children") or []:
                    if ch.get("kind") == "phase_harness" and not ch.get("hook_only"):
                        ch["cost_cached_usd"] = c_usd
                        ch["tokens_cached"] = int(c_tok)
                        ch["display_cached_tokens"] = int(c_tok)
                        ch["display_cached_usd"] = c_usd
                tot_cost_cache += c_usd
                tot_cost += api_bill
            call_cache_usd = float(tot_cost_cache)
        else:
            call_cache_usd = float(tot_cost_cache)
        # User-row continuity $ (same prefix as Call 1). Do not add it
        # again into tot_cost — Call 1 already prices that prefix.
        if not cold and user_cache_share > 0:
            user_tier_ctx = 1
            if annotated:
                user_tier_ctx = step_tier_ctx(
                    annotated[0], fallback=int(prior_i or 1) or 1
                )
            elif isinstance(prior_i, int) and prior_i > 0:
                user_tier_ctx = int(prior_i)
            user_cache_usd = float(_price_cache(int(user_cache_share), user_tier_ctx))
        tot_cache = int(off_cache)

    paid_unc_sum = int(off_unc) if off_unc is not None else (
        int(sum(paid_uncached)) if paid_uncached else 0
    )
    caused_unc_sum = int(tot_harness_in)
    api_in_usd = float(tot_cost_in)
    miss_tier = 1
    if context_end_tokens is not None:
        try:
            miss_tier = max(1, int(context_end_tokens))
        except (TypeError, ValueError):
            miss_tier = 1
    elif stream_ends:
        miss_tier = max(1, max(int(e or 0) for e in stream_ends))
    elif prior_i:
        miss_tier = max(1, int(prior_i))
    header_cache_usd = float(call_cache_usd)
    if off_cache is not None:
        header_cache_usd = float(_price_cache(int(off_cache), miss_tier))
    cache_miss_usd = (
        float(_price_in(int(cache_miss_tok), miss_tier)) if cache_miss_tok else 0.0
    )
    user_in_usd = float(_price_in(int(user_i), miss_tier)) if user_i else 0.0
    tree_tok = int(user_i) + int(tot_harness_in) + int(cache_miss_tok)
    tree_usd = float(user_in_usd) + float(tot_harness_in_usd) + float(cache_miss_usd)
    compact_sum = int(sum(int(x) for x in compact_per)) if compact_per else 0
    tools_usd = max(
        0.0,
        float(tot_harness_in_usd)
        - float(tot_out_to_harness_in_usd)
        - (float(_price_in(compact_sum, miss_tier)) if compact_sum else 0.0),
    )
    breakdown = {
        "uncached_in_tokens": paid_unc_sum,
        "uncached_in_usd": api_in_usd,
        "caused_uncached_tokens": caused_unc_sum,
        "tree_in_tokens": tree_tok,
        "tree_in_usd": tree_usd,
        "cached_tokens": int(off_cache) if off_cache is not None else int(tot_cache),
        "cached_tokens_display_sum": int(cache_display_sum),
        "cached_usd": float(header_cache_usd),
        "output_tokens": tot_out,
        "output_usd": float(tot_cost_out),
        "total_usd": float(tot_cost),
        "user_in_tokens": int(user_i),
        "user_in_usd": float(user_in_usd),
        "cache_miss_in_tokens": int(cache_miss_tok),
        "cache_miss_in_usd": float(cache_miss_usd),
        "last_cache_omitted_tokens": int(last_cache_omitted),
        "harness_in_tokens": tot_harness_in,
        "harness_in_usd": float(tot_harness_in_usd),
        "harness_tools_in_tokens": int(tools_only_sum),
        "harness_tools_in_usd": float(tools_usd),
        "llm_out_to_harness_tokens": tot_out_to_harness,
        "llm_out_to_harness_usd": float(tot_out_to_harness_usd),
        "llm_out_to_harness_in_tokens": int(tot_out_to_harness_in),
        "llm_out_to_harness_in_usd": float(tot_out_to_harness_in_usd),
        "llm_out_to_user_tokens": tot_out_to_user,
        "llm_out_to_user_usd": float(tot_out_to_user_usd),
        "llm_reasoning_tokens": tot_reasoning,
        "llm_reasoning_usd": float(tot_reasoning_usd),
        "llm_reason_budget_tokens": int(tot_reason_budget),
        "llm_reason_budget_usd": float(tot_reason_budget_usd),
        "llm_thought_summary_tokens": int(tot_thought_summary),
        "llm_thought_summary_usd": float(tot_thought_summary_usd),
        "llm_reasoning_encrypted_tokens": int(tot_reasoning_enc),
        "llm_reasoning_encrypted_usd": float(tot_reasoning_enc_usd),
        "reasoning_tokens_official": off_reason,
        "paid_uncached_tokens": paid_unc_sum,
        "user_prior_paid_uncached": int(paid_uncached[0]) if paid_uncached else 0,
        "bootstrap_residual_tokens": int(bootstrap_residual_tokens),
        "phys_raw_starts": list(phys_raw) if phys_raw else [],
        "cold_round": bool(cold),
        "user_uncached_reserved_tokens": int(user_i if not cold else 0),
        "user_cache_share_tokens": int(user_cache_share),
        "user_cache_share_usd": float(user_cache_usd),
        "call_cached_tokens": int(call_cache_tok),
        "call_cached_usd": float(call_cache_usd),
        "tokenizer": tokenizer_mode(),
    }

    return {
        "steps": annotated,
        "method": (
            "tokenizer_prorata_calibrated" if calibrated else "tokenizer_prorata_proxy"
        ),
        "calibrated": calibrated,
        "totals": {
            "input": tot_in,
            "cached_read": tot_cache,
            "uncached_input": paid_unc_sum,
            "caused_uncached_input": caused_unc_sum,
            "tree_in": tree_tok,
            "tree_in_usd": tree_usd,
            "harness_in": int(tot_harness_in),
            "output": tot_out,
            "cost_usd": float(tot_cost),
            "cost_in_usd": api_in_usd,
            "cost_tree_in_usd": tree_usd,
            "cost_cached_usd": float(header_cache_usd),
            "cost_out_usd": float(tot_cost_out),
        },
        "breakdown": breakdown,
        "bootstrap_residual_tokens": int(bootstrap_residual_tokens),
        "note": (
            f"Tokenizer={tokenizer_mode()}. "
            "Reasoning=Thought+ToolReq+Enc residual; pure Out→messages only. "
            "Cached display=rolling prefix (last keeps it). "
            "Round header Cached tokens/$ = official cachedRead. "
            "Round KV miss is leftover official uncached."
        ),
        "prior_context_tokens": prior_context_tokens,
    }
