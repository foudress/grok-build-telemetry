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


def reconstruct_model_step_usage(
    steps: list[dict[str, Any]],
    *,
    official_usage: Optional[dict[str, Any]] = None,
    prior_context_tokens: Optional[int] = None,
    context_reread: bool = False,
    reread_uncached_tokens: int = 0,
    user_uncached_tokens: int = 0,
) -> dict[str, Any]:
    """
    Tokenizer-weighted per-call reconstruction (simple + fair).

    Sources:
      - updates `_meta.totalTokens` → context_start / context_end (totalinput)
      - chat_history / tool payloads → tokenized weights (xai-token-estimation
        bytes/4, or GROK_TOKENIZER=tiktoken)
      - turn_completed.usage → official input / cache / output / reasoning

    Rules (keep UI fields identical to previous versions):
      1. Cached per LLM call from totalinput (context_start):
           multi: Cached[i] = context_start[i] for i < last; last = 0
           single warm: Cached = prior; single cold: 0
           then scale Σ to official cachedReadTokens when present
         (no attempt to match In/Out growth under harness compression)
         context_reread: do NOT force prior into call0 cache (it was re-billed)
      2. Uncached In (paid@start): pro-rate official Input−Cache by tokenized
         *input growth* weights (user/bootstrap on call0, then prior Out+tools)
      3. Tree Call In = Harness In = LLM Out (re-enter) + tool results (+ hooks).
         Intermediate calls: Out_i is first harness line → next-call uncached In.
         Final call: Out goes to user (no Out→In); tools only.
         Warm scale: Σ harness ≈ off_unc − user_uncached − reread
         (User In is reserved — never absorbed into call/tool pool).
         Out line keeps Out mass off tools → healthier tokZ/tokF on tools.
      4. Out: Thought/Message/ToolReq = exact TokZ; Reasoning[enc] =
         residual of full output (pure+reason API buckets) after those TokZ,
         pro-rata by enc tokZ. Attr: LLM→User=Msg · Reasoning=Enc · LLM→Harness=ΣToolReq.
      5. Δctx (context_growth_est): window growth only —
           raw>0 → max(end−start, Out); raw==0 → Out + unscaled harness pool.
           Never warm-scaled Call In / off_unc (multi-call API bill ≠ window Δ).
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

    def _tok_from_chars(chars: int) -> float:
        return float(count_chars_as_tokens(max(0, int(chars or 0))))

    def _step_total_input(step: dict[str, Any]) -> int:
        """totalinput for this LLM call = context_start (stream totalTokens)."""
        cs = step.get("context_start")
        if isinstance(cs, int) and cs >= 0:
            return int(cs)
        for key in ("stream_context_start", "context_peak", "context_end"):
            v = step.get(key)
            if isinstance(v, int) and v >= 0:
                return int(v)
        return 0

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

    total_inputs: list[int] = []
    raw_out_thought: list[float] = []
    raw_out_enc: list[float] = []
    raw_out_emit: list[float] = []
    raw_out_message: list[float] = []
    harness_w: list[float] = []
    tool_ws: list[list[float]] = []

    for step in steps:
        ti = _step_total_input(step)
        total_inputs.append(ti)

        # Prefer Grok-2 token weights when hierarchy stamped them; else chars//4
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
        # Stream-faithful pool; tokenizer only splits within it.
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

    # Cached from totalinput (context_start)
    caches = [0] * n
    if n >= 2:
        for i in range(n - 1):
            caches[i] = max(0, int(total_inputs[i]))
        caches[n - 1] = 0
        # Warm continuity: prior was served as cache — unless full re-read
        if not cold and prior_i > 0 and not context_reread:
            caches[0] = max(caches[0], prior_i)
    elif n == 1:
        if not cold and prior_i > 0 and not context_reread:
            caches[0] = prior_i
        else:
            caches[0] = 0

    # User-prompt continuity Cached is part of official cachedRead — not on top.
    # Reserve prior for the user row; scale LLM-call caches to the remainder so
    # user_cached + Σ call_cached == official cachedRead.
    user_cache_share = 0
    if (
        not cold
        and prior_i > 0
        and not context_reread
        and off_cache is not None
        and int(off_cache) > 0
    ):
        user_cache_share = min(int(prior_i), int(off_cache))

    if off_cache is not None:
        off_c = int(off_cache)
        call_cache_target = max(0, off_c - int(user_cache_share))
        if n >= 2:
            head = [float(max(0, caches[i])) for i in range(n - 1)]
            if sum(head) > 0:
                scaled = _scale_ints(head, call_cache_target)
                for i in range(n - 1):
                    caches[i] = int(scaled[i])
            elif call_cache_target > 0:
                caches[0] = call_cache_target
            caches[n - 1] = 0
            drift = call_cache_target - sum(caches[: n - 1])
            if drift != 0 and n >= 2:
                caches[n - 2] = max(0, caches[n - 2] + drift)
            caches[n - 1] = 0
        else:
            # Single call: remainder after user continuity (often 0 on pure warm)
            caches[0] = call_cache_target

    # Paid uncached pro-rata (tokenized input growth)
    paid_w: list[float] = []
    for i in range(n):
        if i == 0:
            if cold:
                paid_w.append(float(max(1, total_inputs[0], caches[0] + 1)))
            else:
                growth = max(0, total_inputs[0] - prior_i)
                paid_w.append(float(max(1, growth, harness_w[0])))
        else:
            prev_out_w = (
                raw_out_thought[i - 1]
                + raw_out_enc[i - 1]
                + raw_out_emit[i - 1]
                + raw_out_message[i - 1]
            )
            paid_w.append(float(max(1.0, prev_out_w + harness_w[i - 1], harness_w[i])))

    if off_unc is not None and sum(paid_w) > 0:
        paid_uncached = _scale_ints(paid_w, int(off_unc))
    elif off_unc is not None:
        paid_uncached = [0] * n
        paid_uncached[0] = int(off_unc)
    else:
        paid_uncached = [max(0, int(round(w))) for w in paid_w]

    inputs = [int(caches[i] + paid_uncached[i]) for i in range(n)]
    logical_inputs = list(inputs)
    logical_caches = list(caches)
    logical_uncached = list(paid_uncached)

    if cold and n and total_inputs[0] > 0 and off_in is not None:
        stream0 = int(total_inputs[0])
        if caches[0] > stream0:
            bootstrap_residual_tokens = max(0, caches[0] - stream0)
        elif off_in > stream0 and off_cache is not None:
            bootstrap_residual_tokens = max(0, int(off_in) - sum(total_inputs))
            if bootstrap_residual_tokens <= 0:
                bootstrap_residual_tokens = max(0, caches[0] - stream0)

    # --- Out model (UI + bill) ---
    # Thought / Message / ToolRequest = exact TokZ.
    # Reasoning[encrypted] = residual of the *full* official output pool
    # (pure Out + reasoningTokens API buckets) after those TokZ, pro-rata by
    # encrypted tokZ. Enc thus absorbs leftover pure Out as well as reason.
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
        # proxy: fixed TokZ + enc signal
        total_out_sum = max(
            fixed_sum + max(0, int(round(sum(enc_w)))),
            fixed_sum,
            1,
        )

    # Official reason bucket (informational / clamp reference only)
    if off_reason is not None:
        target_r_official = min(max(0, int(off_reason)), total_out_sum)
    else:
        target_r_official = max(0, em_sum + max(0, int(round(sum(enc_w)))))
        target_r_official = min(target_r_official, total_out_sum)

    # If Thought+Message+ToolReq TokZ exceed full output, scale them jointly
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

    # Enc residual = full output − Thought − Message − ToolReq
    # (covers leftover pure Out + official reason − those TokZ)
    enc_pool = max(0, int(total_out_sum) - int(fixed_sum))
    out_reasoning = (
        _scale_ints(enc_w, enc_pool) if enc_pool > 0 and sum(enc_w) > 0 else [0] * n
    )
    if enc_pool > 0 and sum(out_reasoning) == 0:
        # no enc size signal → park residual on last active call
        out_reasoning = [0] * n
        for i in range(n - 1, -1, -1):
            if th_w_list[i] + em_w_list[i] + msg_w_list[i] > 0 or i == 0:
                out_reasoning[i] = enc_pool
                break

    # Fix drift so Σ(th+msg+em+enc) == total_out_sum
    got_fixed_enc = int(
        sum(out_thought) + sum(out_message) + sum(out_emit) + sum(out_reasoning)
    )
    if total_out_sum > 0 and got_fixed_enc != total_out_sum:
        need = total_out_sum - got_fixed_enc
        if need > 0:
            out_reasoning[-1] = int(out_reasoning[-1]) + need
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

    # Display / attr: reason budget = ToolReq + Enc (may exceed official reason)
    target_r = int(sum(out_emit) + sum(out_reasoning))
    pure_out = max(0, int(sum(out_thought) + sum(out_message)))

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
            out_reasoning[-1] += d
            outputs[-1] += d
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

    # Harness tree In = fixed LLM Out (re-enter) + tool pool (scaled separately).
    # Final call: Out → user (not next LLM); tools/hooks only.
    # Out→In mass (LLM Out [N] under Harness) = full billed Out TokF:
    #   Thought TokF + Reasoning TokF + Message TokF + ToolRequest TokF
    # (encrypted blob is server-replaced — do not use enc TokZ for re-entry.)
    enc_z_list = [max(0, int(round(enc_w[i]))) for i in range(n)]  # UI stamp only
    harness_w_tools = [float(w) for w in harness_w]
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

    out_in_fixed_sum = int(sum(int(round(x)) for x in out_to_harness_in_w))
    # Reserve User (+ reread) so harness/tools never absorb prompt In mass
    user_reserve = max(0, int(user_uncached_tokens or 0))
    reread_reserve = (
        max(0, int(reread_uncached_tokens or 0)) if context_reread else 0
    )
    # Scale tools into residual: off_unc − user − reread − Out→In
    if off_unc is not None and not cold and sum(harness_w_tools) > 0:
        tools_target = max(
            0,
            int(off_unc) - out_in_fixed_sum - user_reserve - reread_reserve,
        )
        tools_toks = _scale_ints(harness_w_tools, tools_target)
    elif sum(harness_w_tools) > 0:
        tools_toks = [max(0, int(round(w))) for w in harness_w_tools]
    else:
        tools_toks = [0] * n
    # Total harness_toks = tools share + fixed Out→In (per call)
    harness_toks = [
        int(tools_toks[i]) + int(round(out_to_harness_in_w[i]))
        for i in range(n)
    ]

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
        re_tok = int(out_reasoning[i])  # encrypted residual only
        em_tok = int(out_emit[i])
        msg_tok = int(out_message[i])
        # Reasoning budget = ToolRequest + Encrypted (Thought is pure Out)
        reason_budget_tok = em_tok + re_tok
        # Attribution "Reasoning" strip = encrypted only
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
        # llm_reasoning_* = encrypted only (Attribution: Reasoning)
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
                        if t_i < len(_per_tool):
                            c["tokens_in"] = int(_per_tool[t_i])
                            c["context_delta"] = int(_per_tool[t_i])
                            c["cost_in_usd"] = float(
                                _price_in(int(_per_tool[t_i]), _tier)
                            )
                            c["estimate_usd"] = float(c["cost_in_usd"])
                            c["estimate_note"] = (
                                f"tool result → In (tokenizer {tokenizer_mode()}) "
                                "prorata of official uncached"
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
                    in ("tool", "late_context", "hook", "llm_to_in")
                )
                if (has_tool_payload or has_llm_to_in) and tok_in <= 0:
                    tok_in = _h_tok
                usd_in = _price_in(tok_in, _tier)
                in_children = [
                    c
                    for c in sub
                    if not c.get("to_user")
                    and (
                        c.get("kind")
                        in ("tool", "late_context", "hook", "llm_to_in")
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
                        "Cached = this call's prompt prefix (totalinput@start). "
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
                    f"Thought summary — exact TokZ "
                    f"(tokenizer {tokenizer_mode()}); residual pure Out → Enc."
                )
                return ch

            if kind == "message":
                parts = _money_parts(tokens_out=_msg, cost_out=_msg_usd)
                ch.update(parts)
                ch["chars"] = _message_chars
                ch["estimate_output_tokens"] = _msg
                ch["estimate_note"] = (
                    f"assistant.content — exact TokZ "
                    f"(tokenizer {tokenizer_mode()}); residual pure Out → Enc."
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
                    f"tool request RawInput — tokenizer({tokenizer_mode()}) definitive; "
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
                        f"tool result → In (tokenizer {tokenizer_mode()} prorata)"
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
                    th_node["estimate_note"] = (
                        "Thought — exact TokZ; residual pure Out → Enc"
                    )
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
        if final_to_user:
            cache_t = 0
            cost_cache = 0.0
            cost_cache_logical = 0.0
            logical_cache = 0
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

        # Δctx = context *window* growth for this call (not API uncached bill).
        # Prefer totalTokens end−start when the stream moved.
        # - If raw > 0: floor by Out only (message can lag behind end snap)
        # - If raw == 0: Out + unscaled harness (final call / full stream lag)
        # NEVER use h_tok / unc_t: those are warm-scaled to official
        # Input−Cache (Σ multi-call bills) and inflated Δctx to 10k–100k.
        raw_delta = 0
        s0, s1 = step.get("context_start"), step.get("context_end")
        if isinstance(s0, int) and isinstance(s1, int):
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
                "Final call of the round → user. No Cached "
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
                "Tree In (call) = Harness In = LLM Out→In (mid-round) + tools. "
                "Cached = totalinput@stream start (context_start), last=0. "
                "White estimate_usd = In + Cached + Out (displayed; Out also in In). "
                "api_call_usd = paid@start uncached + cache + out."
            ),
        }
        step_ann["children"] = children_out
        step_ann["tools"] = tools_out
        annotated.append(step_ann)

    warm_in_scaled = False
    warm_user_est = 0
    reread_i = max(0, int(reread_uncached_tokens or 0)) if context_reread else 0
    user_i = max(0, int(user_uncached_tokens or 0))
    if not cold and off_unc is not None and annotated:
        if prior_i > 0 and steps:
            cs0 = steps[0].get("context_start")
            if isinstance(cs0, int) and cs0 > prior_i:
                warm_user_est = max(0, int(cs0) - prior_i)
        # Prefer explicit user_uncached; fall back to stream growth estimate
        if user_i <= 0 and warm_user_est > 0:
            user_i = int(warm_user_est)
        # Warm: harness target = off_unc − user − reread (never absorb User In)
        target_h = max(0, int(off_unc) - int(user_i) - int(reread_i))

        # Per-step fixed Out→In and tools-only weights
        out_fixed_per: list[int] = []
        tools_w_per: list[float] = []
        for s in annotated:
            o_fix = 0
            t_w = 0.0
            for ch in s.get("children") or []:
                if ch.get("kind") != "phase_harness":
                    continue
                for sub in ch.get("children") or []:
                    if sub.get("to_user"):
                        continue
                    ck = sub.get("kind")
                    tin = int(sub.get("tokens_in") or sub.get("context_delta") or 0)
                    if ck == "llm_to_in":
                        o_fix += tin
                    elif ck in ("tool", "late_context", "hook") or sub.get("name"):
                        t_w += float(max(0, tin))
            if o_fix <= 0:
                o_fix = int(s.get("llm_out_in_tokens") or 0)
            if t_w <= 0:
                # fall back: harness_in − out
                t_w = float(
                    max(0, int(s.get("harness_in_tokens") or 0) - o_fix)
                )
            out_fixed_per.append(int(o_fix))
            tools_w_per.append(float(t_w))

        out_fixed_sum = int(sum(out_fixed_per))
        tools_target = max(0, int(target_h) - out_fixed_sum)
        tools_sum = sum(tools_w_per)
        h_sum = out_fixed_sum + tools_sum

        if h_sum > 0 and (
            target_h != int(round(h_sum))
            or (context_reread and reread_i > 0)
            or (tools_sum > 0 and tools_target != int(round(tools_sum)))
        ):
            if tools_sum > 0:
                scaled_tools = _scale_ints(tools_w_per, tools_target)
            else:
                scaled_tools = [0] * len(annotated)
            tot_harness_in = 0
            tot_harness_in_usd = 0.0
            tot_out_to_harness_in = 0
            tot_out_to_harness_in_usd = 0.0
            for si, s in enumerate(annotated):
                o_fix = int(out_fixed_per[si])
                new_tools = int(scaled_tools[si]) if si < len(scaled_tools) else 0
                new_h = int(o_fix) + int(new_tools)
                tctx = step_tier_ctx(s, fallback=max(new_h, 1))
                new_h_usd = float(_price_in(new_h, tctx)) if new_h > 0 else 0.0
                children = list(s.get("children") or [])
                for ch in children:
                    if ch.get("kind") != "phase_harness":
                        continue
                    sub = list(ch.get("children") or [])
                    # Keep llm_to_in fixed; scale only tool/late/hook kids
                    tool_kids = [
                        c
                        for c in sub
                        if not c.get("to_user")
                        and c.get("kind") != "llm_to_in"
                        and (
                            c.get("kind") in ("tool", "late_context", "hook")
                            or c.get("name")
                        )
                    ]
                    for c in sub:
                        if c.get("kind") != "llm_to_in":
                            continue
                        c["tokens_in"] = int(o_fix)
                        c["context_delta"] = int(o_fix)
                        c["cost_in_usd"] = (
                            float(_price_in(o_fix, tctx)) if o_fix > 0 else 0.0
                        )
                        c["estimate_usd"] = float(c["cost_in_usd"])
                    if tool_kids:
                        kw = [
                            float(
                                max(
                                    0,
                                    int(
                                        c.get("tokens_in")
                                        or c.get("context_delta")
                                        or 0
                                    ),
                                )
                            )
                            for c in tool_kids
                        ]
                        if sum(kw) <= 0:
                            kw = [1.0] * len(tool_kids)
                        if new_tools > 0:
                            ks = _scale_ints(kw, new_tools)
                            for c, kt in zip(tool_kids, ks):
                                c["tokens_in"] = int(kt)
                                c["context_delta"] = int(kt)
                                c["cost_in_usd"] = float(_price_in(int(kt), tctx))
                                c["estimate_usd"] = float(
                                    float(c.get("cost_in_usd") or 0)
                                    + float(c.get("cost_cached_usd") or 0)
                                    + float(c.get("cost_out_usd") or 0)
                                )
                        else:
                            for c in tool_kids:
                                c["tokens_in"] = 0
                                c["context_delta"] = 0
                                c["cost_in_usd"] = 0.0
                    ch["tokens_in"] = new_h
                    ch["cost_in_usd"] = new_h_usd
                    ch["estimate_usd"] = new_h_usd
                    ch["llm_out_in_tokens"] = int(o_fix)
                    ch["llm_out_in_usd"] = (
                        float(_price_in(o_fix, tctx)) if o_fix > 0 else 0.0
                    )
                    ch["children"] = sub
                s["children"] = children
                s["tokens_in"] = new_h
                s["cost_in_usd"] = new_h_usd
                s["harness_in_tokens"] = new_h
                s["harness_in_usd"] = new_h_usd
                s["llm_out_in_tokens"] = int(o_fix)
                s["llm_out_in_usd"] = (
                    float(_price_in(o_fix, tctx)) if o_fix > 0 else 0.0
                )
                cache_usd = float(s.get("cost_cached_usd") or 0)
                out_usd = float(s.get("cost_out_usd") or 0)
                line_usd = float(new_h_usd + cache_usd + out_usd)
                s["cost_of_call_usd"] = line_usd
                s["estimate_usd"] = line_usd
                est = dict(s.get("estimate") or {})
                est["uncached_input_tokens"] = new_h
                est["logical_uncached_tokens"] = new_h
                est["cost_in_usd"] = new_h_usd
                est["estimate_usd"] = line_usd
                est["cost_of_call_usd"] = line_usd
                s["estimate"] = est
                tools_scaled: list[dict[str, Any]] = []
                for ch in children:
                    if ch.get("kind") == "phase_harness":
                        for sub in ch.get("children") or []:
                            if sub.get("kind") == "tool":
                                tools_scaled.append(sub)
                    elif ch.get("kind") == "tool":
                        tools_scaled.append(ch)
                s["tools"] = tools_scaled
                comp = dict(s.get("composition") or {})
                # Bar harness = full Call In (not tools-only)
                comp["harness_results"] = int(new_h)
                comp["harness_tools_only"] = int(new_tools)
                comp["llm_out_to_in"] = int(o_fix)
                s["composition"] = comp
                s["harness_scaled_to_official_unc"] = True
                s["harness_raw_tokens"] = int(out_fixed_per[si] + tools_w_per[si])
                tot_harness_in += new_h
                tot_harness_in_usd += new_h_usd
                tot_out_to_harness_in += int(o_fix)
                tot_out_to_harness_in_usd += (
                    float(_price_in(o_fix, tctx)) if o_fix > 0 else 0.0
                )
            warm_in_scaled = not bool(context_reread and reread_i > 0)
        elif h_sum > 0:
            warm_in_scaled = not bool(context_reread and reread_i > 0)

    cache_display_sum = (
        int(sum(int(s.get("tokens_cached") or 0) for s in annotated)) if annotated else 0
    )
    # Call caches sum to (off_cache − user_cache_share). Fit $ to that remainder
    # so we don't re-inflate call Cached to the full official bill.
    # Round Cached $ must still include user_cache_share (shown on user-prompt row;
    # previously only call remainder was priced → e.g. 256k tok @ $0.039 not $0.077).
    call_cache_tok = int(cache_display_sum)
    user_cache_usd = 0.0
    call_cache_usd = float(tot_cost_cache)
    if off_cache is not None:
        # Official round total still reports full cachedRead
        expected_call_cache = max(0, int(off_cache) - int(user_cache_share))
        if (
            not cold
            and annotated
            and call_cache_tok > 0
            and call_cache_tok != expected_call_cache
        ):
            # Re-normalize token weights if drift
            pass
        # Price cache **per LLM call** with that call's own tier (context_start).
        # Never use round peak: crossing 200k mid-round only raises rates for
        # calls whose prompt already sits above the threshold.
        if not cold and annotated and (call_cache_tok > 0 or expected_call_cache > 0):
            w_c = [float(max(0, int(s.get("tokens_cached") or 0))) for s in annotated]
            tok_sum = sum(w_c)
            scale_tok = 1.0
            if (
                tok_sum > 0
                and expected_call_cache > 0
                and abs(tok_sum - expected_call_cache) > 0
            ):
                scale_tok = float(expected_call_cache) / tok_sum
            tot_cost_cache = 0.0
            tot_cost = 0.0
            for s in annotated:
                c_tok = int(max(0, round(float(s.get("tokens_cached") or 0) * scale_tok)))
                tctx = step_tier_ctx(s, fallback=max(c_tok, 1))
                c_usd = float(_price_cache(c_tok, tctx))
                s["tokens_cached"] = c_tok
                s["cost_cached_usd"] = c_usd
                s["tier_context_tokens"] = int(tctx)
                s["tier"] = pick_tier(tctx)["name"]
                paid_in = float(s.get("paid_at_start_usd") or 0)
                out_usd = float(s.get("cost_out_usd") or 0)
                in_usd = float(s.get("cost_in_usd") or 0)
                api_bill = paid_in + c_usd + out_usd
                line_bill = in_usd + c_usd + out_usd
                # White total matches displayed In + Cached + Out
                s["estimate_usd"] = float(line_bill)
                s["cost_of_call_usd"] = float(line_bill)
                s["api_call_usd"] = float(api_bill)
                est = dict(s.get("estimate") or {})
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
                        ch["tokens_cached"] = int(s.get("tokens_cached") or 0)
                        ch["display_cached_tokens"] = int(s.get("tokens_cached") or 0)
                        ch["display_cached_usd"] = c_usd
                tot_cost_cache += c_usd
                tot_cost += api_bill
            call_cache_usd = float(tot_cost_cache)
        else:
            call_cache_usd = float(tot_cost_cache)
        # User continuity cache $: tier of first call (or prior), not round peak
        if not cold and user_cache_share > 0:
            user_tier_ctx = 1
            if annotated:
                user_tier_ctx = step_tier_ctx(
                    annotated[0], fallback=int(prior_i or 1) or 1
                )
            elif isinstance(prior_i, int) and prior_i > 0:
                user_tier_ctx = int(prior_i)
            user_cache_usd = float(_price_cache(int(user_cache_share), user_tier_ctx))
            tot_cost_cache = float(call_cache_usd) + float(user_cache_usd)
            tot_cost = float(tot_cost) + float(user_cache_usd)
        else:
            tot_cost_cache = float(call_cache_usd)
        # Round-level cached tokens = official (user share + call share)
        tot_cache = int(off_cache)

    paid_unc_sum = int(sum(paid_uncached)) if paid_uncached else 0
    caused_unc_sum = int(tot_harness_in)
    api_in_usd = float(tot_cost_in)
    tree_tok = int(tot_harness_in)
    tree_usd = float(tot_harness_in_usd)
    breakdown = {
        "uncached_in_tokens": paid_unc_sum,
        "uncached_in_usd": api_in_usd,
        "caused_uncached_tokens": caused_unc_sum,
        "tree_in_tokens": tree_tok,
        "tree_in_usd": tree_usd,
        "cached_tokens": int(tot_cache),
        "cached_tokens_display_sum": int(cache_display_sum),
        "cached_usd": float(tot_cost_cache),
        "output_tokens": tot_out,
        "output_usd": float(tot_cost_out),
        "total_usd": float(tot_cost),
        "harness_in_tokens": tot_harness_in,
        "harness_in_usd": float(tot_harness_in_usd),
        # Tools/hooks only (for Harness→LLM attribution — exclude Out re-entry)
        "harness_tools_in_tokens": max(
            0, int(tot_harness_in) - int(tot_out_to_harness_in)
        ),
        "harness_tools_in_usd": max(
            0.0, float(tot_harness_in_usd) - float(tot_out_to_harness_in_usd)
        ),
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
        "warm_in_scaled_to_official": bool(warm_in_scaled),
        "warm_user_est_tokens": int(warm_user_est),
        "context_reread": bool(context_reread),
        "reread_uncached_tokens": int(reread_i),
        "user_uncached_reserved_tokens": int(user_i if not cold else 0),
        # Continuity cache held on user prompt (token scale excludes from calls;
        # $ is re-added into round cached_usd so tokens ↔ $ match).
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
            "cost_cached_usd": float(tot_cost_cache),
            "cost_out_usd": float(tot_cost_out),
        },
        "breakdown": breakdown,
        "bootstrap_residual_tokens": int(bootstrap_residual_tokens),
        "note": (
            f"Tokenizer={tokenizer_mode()}. "
            "Reasoning=Thought+ToolReq+Enc residual; pure Out→messages only. "
            "Cached[i]=context_start (last=0). Tree Call In=harness. Round $=API bill."
        ),
        "prior_context_tokens": prior_context_tokens,
    }
