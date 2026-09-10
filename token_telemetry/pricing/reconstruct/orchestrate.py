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
    _inject_compact_out_in,
    _mid_round_split_points,
    _reconstruct_compact_segments,
    _step_compact_out_in,
    _step_stream_end,
    _step_stream_start,
    _step_total_input,
)
from token_telemetry.pricing.reconstruct.in_cache import (
    _fit_tools_to_delta_ctx,
    _omit_last_prefix,
    _reconstruct_inputs_and_cache,
    _rolling_prefix_caches,
)
from token_telemetry.pricing.reconstruct.output_enc import (
    _enc_tokz_weights,
    _reconstruct_output,
)
from token_telemetry.pricing.reconstruct.weights import (
    _clamp_paid_to_inputs,
    _collect_step_weights,
    _delta_ctx_for_fit,
    _tok_from_chars,
    _tools_z_from_steps,
)

_HARNESS_IN_KINDS = frozenset(
    ("tool", "late_context", "hook", "llm_to_in", "compact_out_in")
)


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
    last_out_tok = 0
    if n >= 1:
        last_out_tok = (
            int(out_thought[n - 1])
            + int(out_reasoning[n - 1])
            + int(out_message[n - 1])
            + int(out_emit[n - 1])
        )
    tools_toks = _fit_tools_to_delta_ctx(
        tools_z=tools_z,
        reentry=reentry_toks,
        compact_per=compact_per,
        user_in=user_i,
        delta_ctx=delta_ctx,
        last_out=int(last_out_tok),
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
                # TokZ = real encrypted_content tokenizer stamp (not chars//4).
                # Never write 0 — UI treats 0 as a stamp and hides tokZ.
                ch["encrypted_tokens"] = int(_enc_z)
                if _enc_z > 0:
                    ch["tokenizer_tokens"] = int(_enc_z)
                else:
                    ch.pop("tokenizer_tokens", None)
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
