"""Cache-miss / context-reread / session-restart attribution.

Free functions take a HierarchyBuilder-like object as first arg ``hb``.
HierarchyBuilder methods are thin wrappers so behavior stays identical.
"""

from __future__ import annotations

from typing import Any, Optional

from token_telemetry.tokenizer import (
    count_chars_as_tokens,
    count_tokens,
)


def _attach_prev_llm_answer(hb: Any, r: dict[str, Any]) -> None:
    """
    Split User uncached into:
      LLM answer round [N-1]  = Thought TokZ + Reasoning TokF + Message TokZ
      prompt                  = user_uncached − that In mass
    so prompt never double-counts the previous answer mass.

    TokZ = tokenizer stamp (summary / message). TokF for reasoning =
    billed encrypted residual (off_reason − Thought − ToolReq), not
    thought_encrypted_tokens (raw encrypt size — often much larger).
    """
    up = r.get("user_prompt")
    if not isinstance(up, dict) or up.get("kind") != "user_prompt":
        return
    prev: Optional[dict[str, Any]] = None
    if r in hb.rounds:
        idx = hb.rounds.index(r)
        if idx > 0:
            prev = hb.rounds[idx - 1]
    elif hb.rounds:
        prev = hb.rounds[-1]
    if not isinstance(prev, dict):
        up.pop("prev_llm_answer", None)
        return
    steps = [s for s in (prev.get("model_steps") or []) if isinstance(s, dict)]
    if not steps:
        up.pop("prev_llm_answer", None)
        return
    last = steps[-1]
    se = last.get("estimate") if isinstance(last.get("estimate"), dict) else {}
    comp = last.get("composition") if isinstance(last.get("composition"), dict) else {}

    def _pos(*vals: Any) -> int:
        for v in vals:
            try:
                n = int(v or 0)
            except (TypeError, ValueError):
                continue
            if n > 0:
                return n
        return 0

    # Thought / Message: TokZ only (never billed / scaled tokens_out)
    th_z = _pos(
        last.get("thought_summary_tokens"),
        last.get("thought_tokens"),
    )
    msg_z = _pos(last.get("message_tokens"))
    # Reasoning: TokF only (priced residual on the reasoning node / estimate)
    re_f = _pos(
        se.get("output_reasoning_tokens"),
        comp.get("reasoning_encrypted_out"),
    )
    # Optional pure TokZ of encrypted blob (meta only — not used for In)
    re_z = _pos(last.get("thought_encrypted_tokens"))
    em_z = _pos(se.get("output_emit_tokens"), comp.get("model_emit"))
    for ch in last.get("children") or []:
        if not isinstance(ch, dict) or ch.get("kind") != "phase_llm":
            continue
        for sub in ch.get("children") or []:
            if not isinstance(sub, dict):
                continue
            k = sub.get("kind")
            if k == "thought":
                tz = _pos(
                    sub.get("tokenizer_tokens"),
                    sub.get("summary_tokens"),
                )
                if tz:
                    th_z = max(th_z, tz)
            elif k == "reasoning":
                # TokF = billed Out share (residual); never tokenizer/encrypted size
                tf = _pos(
                    sub.get("tokens_out"),
                    sub.get("estimate_output_tokens"),
                )
                if tf:
                    re_f = max(re_f, tf)
                rz = _pos(sub.get("tokenizer_tokens"), sub.get("encrypted_tokens"))
                if rz:
                    re_z = max(re_z, rz)
            elif k == "message":
                tz = _pos(
                    sub.get("tokenizer_tokens"),
                    sub.get("message_tokens"),
                )
                if tz:
                    msg_z = max(msg_z, tz)
            elif k in ("tool_request", "tool_requests"):
                tz = _pos(
                    sub.get("tokenizer_tokens"),
                    sub.get("arg_tokens_est"),
                )
                if tz:
                    em_z = max(em_z, tz) if em_z else tz

    # In mass: Thought TokZ + Reasoning TokF + Message TokZ only.
    # Never include last-call hook tokZ (hooks are not LLM answer / not
    # re-fed as the assistant turn mass for User[N] continuity).
    answer_in = int(th_z) + int(re_f) + int(msg_z)
    # Pure TokZ stamp for meta ratio (enc size when known, else re residual)
    tokz_meta = int(th_z) + int(re_z if re_z > 0 else re_f) + int(msg_z)
    if answer_in <= 0:
        # last resort: full billed Out (model only — not harness hooks)
        answer_in = _pos(last.get("tokens_out"), se.get("output_tokens"))
        tokz_meta = answer_in
    if answer_in <= 0:
        up.pop("prev_llm_answer", None)
        return

    try:
        raw_user = int(up.get("tokens_in") or up.get("uncached_est") or 0)
    except (TypeError, ValueError):
        raw_user = 0
    # Stamp pure-prompt tokZ (query+skill / preview) before peel
    ud = up.get("user_detail") if isinstance(up.get("user_detail"), dict) else {}
    prompt_tz = int(ud.get("user_query_tokens") or 0) + int(
        ud.get("skill_information_tokens") or 0
    )
    if prompt_tz <= 0:
        preview = str(up.get("preview") or r.get("user_preview") or "")
        if preview:
            try:
                prompt_tz = int(count_tokens(preview))
            except Exception:
                prompt_tz = max(1, count_chars_as_tokens(len(preview)) or 1)
    if prompt_tz > 0:
        up["tokenizer_tokens"] = int(prompt_tz)
        up["prompt_tokenizer_tokens"] = int(prompt_tz)

    # Partition User uncached: answer In + residual prompt (no double-count)
    # When raw_user >= answer_in → raw includes answer mass → peel
    # When raw_user < answer_in → pure small prompt; answer is continuity only
    from_user_pool = bool(raw_user > 0 and raw_user >= answer_in)
    if from_user_pool:
        prompt_in = max(0, int(raw_user) - int(answer_in))
        user_tree = int(raw_user)
    else:
        prompt_in = int(raw_user)
        user_tree = int(raw_user)  # answer not added on top

    tier = int(
        last.get("context_end")
        or last.get("context_start")
        or r.get("context_start")
        or max(answer_in, raw_user, 1)
    )
    try:
        from token_telemetry.pricing import _price_in

        answer_usd = float(_price_in(answer_in, tier)) if answer_in else 0.0
        prompt_usd = float(_price_in(prompt_in, tier)) if prompt_in else 0.0
        user_usd = float(_price_in(user_tree, tier)) if user_tree else 0.0
    except Exception:
        answer_usd = 0.0
        prompt_usd = 0.0
        user_usd = float(up.get("cost_in_usd") or 0)

    out_usd = float(last.get("cost_out_usd") or se.get("cost_out_usd") or 0)
    # Cache miss re-bills prior context (includes last LLM answer) as In —
    # never list/count that answer again under User.
    absorbed = bool(
        r.get("cache_miss")
        or up.get("cache_miss")
        or r.get("session_restart")
        or up.get("session_restart")
        or up.get("context_reread")
        or r.get("context_reread")
    )
    up["prev_llm_answer"] = {
        "kind": "prev_llm_answer",
        "round_index": int(prev.get("index") or 0),
        "call_index": int(last.get("index") or len(steps)),
        "tokens_out": int(
            last.get("tokens_out") or se.get("output_tokens") or answer_in
        ),
        "tokens_thought": int(th_z),
        "tokens_reasoning": int(re_f),
        "tokens_reasoning_tokz": int(re_z),
        "tokens_message": int(msg_z),
        "tokens_tool_req": int(em_z),
        "tokenizer_tokens": int(tokz_meta),
        "tokens_in": int(answer_in),
        "cost_out_usd": float(out_usd),
        "cost_in_usd": float(answer_usd),
        "from_user_pool": bool(from_user_pool) and not absorbed,
        "absorbed_in_reread": bool(absorbed),
        "preview": (last.get("message_preview") or "")[:120],
        "note": (
            "Absorbed into cache-miss / context re-read In — not listed under User."
            if absorbed
            else (
                "Last LLM In = Thought TokZ + Reasoning TokF + Message TokZ "
                "(hooks excluded). "
                "Peeled from User uncached so prompt In has no double-count."
                if from_user_pool
                else (
                    "Last LLM In = Thought TokZ + Reasoning TokF + Message TokZ "
                    "(hooks excluded) — continuity display; "
                    "User uncached is pure prompt only."
                )
            )
        ),
    }
    up["prompt_tokens_in"] = int(prompt_in)
    up["prompt_cost_in_usd"] = float(prompt_usd)
    up["uncached_est_raw"] = int(raw_user)
    # Tree User In = partitioned total when answer taken from pool
    up["tokens_in"] = int(user_tree)
    up["uncached_est"] = int(user_tree)
    up["cost_in_usd"] = float(user_usd)
    cache_usd = float(up.get("cost_cached_usd") or 0)
    up["estimate_usd"] = float(user_usd + cache_usd)
    r["user_prompt"] = up


def _detect_context_reread(hb: Any, r: dict[str, Any]) -> Optional[dict[str, Any]]:
    """
    Detect a CALL-1 KV miss: first call re-bills prior as Input.

    Fire only when prior >= 1000, official usage is present, and the
    first-call window still holds prior (not a compact / new window).

    Signals (any one):
      A) classic_cache_miss: cachedRead tiny, uncached ≈ prior
      B) full_context_reread: uncached ≈ prior, tiny growth, window holds
      C) first_call_reread: window still ≈ prior, extra ≈ prior, later
         calls may stay warm (Σ cache large)
      D) idle_context_reread: long idle + large extra, window not collapsed

    Do not use off_unc − growth alone, and do not use first-call
    tokens_cached (usually unset before reconstruct).
    """
    if not r.get("completed"):
        return None
    if isinstance(r.get("system_prompt"), dict):
        return None
    usage = r.get("usage_raw") if isinstance(r.get("usage_raw"), dict) else {}
    try:
        off_in = int(usage.get("inputTokens") or 0)
        off_cache = int(usage.get("cachedReadTokens") or 0)
    except (TypeError, ValueError):
        return None
    if off_in <= 0:
        return None
    off_unc = max(0, off_in - min(off_cache, off_in))

    up = r.get("user_prompt") if isinstance(r.get("user_prompt"), dict) else {}
    prior = up.get("prior_context") if up else None
    if not isinstance(prior, int):
        prior = r.get("cache_baseline_at_start")
    if not isinstance(prior, int) or prior < 1000:
        return None

    end = r.get("context_end")
    if not isinstance(end, int):
        ends = [
            int(s["context_end"])
            for s in (r.get("model_steps") or [])
            if isinstance(s, dict) and isinstance(s.get("context_end"), int)
        ]
        end = max(ends) if ends else int(prior)
    growth = max(0, int(end) - int(prior))

    # Ensure idle_gap_ms when we can (reprice path may not have set it)
    idle = r.get("idle_gap_ms")
    if not isinstance(idle, int):
        idle = _compute_idle_gap_ms(hb, r)
        if isinstance(idle, int):
            r["idle_gap_ms"] = idle

    extra = max(0, off_unc - growth)

    steps = [s for s in (r.get("model_steps") or []) if isinstance(s, dict)]
    c0_start: Optional[int] = None
    if steps:
        raw_start = steps[0].get("stream_context_start")
        if not isinstance(raw_start, int):
            raw_start = steps[0].get("context_start")
        if isinstance(raw_start, int):
            c0_start = raw_start
    if not isinstance(c0_start, int):
        raw_start = r.get("stream_context_start")
        if not isinstance(raw_start, int):
            raw_start = r.get("context_start")
        if isinstance(raw_start, int):
            c0_start = raw_start
    # Compact / new window: start0 << prior is not a cache miss.
    if isinstance(c0_start, int) and c0_start < int(prior * 0.5):
        return None

    # Optional first-call cache stamp (metadata only; not a signal)
    c0_cache: Optional[int] = None
    c0_drop = 0
    if steps:
        raw_c0 = steps[0].get("tokens_cached")
        try:
            if raw_c0 is not None:
                c0_cache = int(raw_c0)
        except (TypeError, ValueError):
            c0_cache = None
        if isinstance(c0_cache, int) and c0_cache >= 0:
            c0_drop = max(0, int(prior) - int(c0_cache))

    window_holds = isinstance(c0_start, int) and c0_start >= int(prior * 0.85)
    classic = off_cache < int(prior * 0.15) and off_unc >= int(prior * 0.5)
    # Full re-bill: uncached ≈ prior, tiny growth, prefix still in window.
    full_reread = (
        off_unc >= int(prior * 0.85)
        and growth < int(prior * 0.15)
        and window_holds
    )
    # Call-1 re-bill while later calls stay warm (Σ cache can be large).
    first_call = (
        window_holds
        and extra >= int(prior * 0.7)
        and off_unc >= int(prior * 0.7)
        and growth < int(prior * 0.4)
    )
    idle_long = isinstance(idle, int) and idle >= 20 * 60 * 1000
    soft = (
        idle_long
        and extra >= int(prior * 0.5)
        and growth < int(prior * 0.3)
        and isinstance(c0_start, int)
        and c0_start >= int(prior * 0.5)
    )
    if not (classic or full_reread or first_call or soft):
        return None

    # Continuity re-read ≈ prior (not the whole round uncached / tools).
    reread_tokens = max(0, min(int(prior), extra))
    if classic:
        kind = "classic_cache_miss"
    elif full_reread:
        kind = "full_context_reread"
    elif first_call:
        kind = "first_call_reread"
    else:
        kind = "idle_context_reread"

    return {
        "kind": kind,
        "prior": int(prior),
        "growth": int(growth),
        "off_in": int(off_in),
        "off_cache": int(off_cache),
        "off_unc": int(off_unc),
        "reread_tokens": int(reread_tokens),
        "idle_gap_ms": idle if isinstance(idle, int) else None,
        "c0_cache": int(c0_cache) if isinstance(c0_cache, int) else None,
        "c0_drop": int(c0_drop) if c0_drop else None,
    }


def _compute_idle_gap_ms(hb: Any, r: dict[str, Any]) -> Optional[int]:
    """Milliseconds between previous round end and this round start."""
    prev: Optional[dict[str, Any]] = None
    if r in hb.rounds:
        idx = hb.rounds.index(r)
        if idx > 0:
            prev = hb.rounds[idx - 1]
    elif hb.rounds:
        prev = hb.rounds[-1]
    if not isinstance(prev, dict):
        return None
    end_ms = prev.get("completed_ms")
    start_ms = r.get("started_ms")
    if start_ms is None:
        start_ms = r.get("turn_start_ms")
    if not isinstance(end_ms, (int, float)) or not isinstance(start_ms, (int, float)):
        return None
    return max(0, int(start_ms) - int(end_ms))


def _apply_session_restart_cache_miss(hb: Any, r: dict[str, Any]) -> None:
    """
    Warm rounds assume prior context is Cached. After idle / KV drop the
    API re-bills prior as Input. Attribution:

      • warning line → re-read In (≈ prior / context_start of the round)
      • user prompt  → only the new user message (small)
      • harness      → residual tools (off_unc − reread − user_new)
      • user Cached  → 0 (prior was not served as cache)
    """
    up = r.get("user_prompt")
    if not isinstance(up, dict) or up.get("kind") != "user_prompt":
        return
    hit = r.get("context_reread")
    if not isinstance(hit, dict):
        hit = _detect_context_reread(hb, r)
    if not hit:
        return

    prior = int(hit["prior"])
    off_in = int(hit["off_in"])
    off_cache = int(hit["off_cache"])
    off_unc = int(hit["off_unc"])
    growth = int(hit["growth"])
    reread = int(hit["reread_tokens"])
    kind = str(hit.get("kind") or "context_reread")

    steps = [s for s in (r.get("model_steps") or []) if isinstance(s, dict)]
    # New user text only (never the re-read mass)
    new_tok = 0
    raw_new = up.get("uncached_est")
    try:
        if raw_new is not None and not up.get("session_restart"):
            new_tok = max(0, int(raw_new))
    except (TypeError, ValueError):
        new_tok = 0
    if steps:
        cs0 = steps[0].get("context_start")
        if isinstance(cs0, int) and cs0 > prior:
            new_tok = max(new_tok, int(cs0) - prior)
    if new_tok <= 0:
        user_chars = int(up.get("chars") or r.get("user_chars") or 0)
        if user_chars > 0:
            new_tok = max(1, count_chars_as_tokens(user_chars) or 1)

    # Clamp partition: reread + user_new + harness ≤ off_unc
    reread = min(int(reread), int(off_unc))
    new_tok = min(int(new_tok), max(0, int(off_unc) - reread))
    allow_h = max(0, int(off_unc) - reread - new_tok)

    # User row: new prompt only; no prior-as-Cached on a miss
    up_in = int(new_tok)
    up_cache = 0

    tier_ctx = int(
        (steps[0].get("context_start") if steps else 0)
        or up.get("context_at_first_call")
        or (prior + new_tok)
        or off_in
        or 1
    )
    try:
        from token_telemetry.pricing import _price_in, _price_cache
    except ImportError:
        try:
            from token_telemetry.pricing import _price_in

            _price_cache = None  # type: ignore
        except ImportError:
            _price_in = None  # type: ignore
            _price_cache = None  # type: ignore

    reread_usd = 0.0
    if _price_in is not None:
        user_usd = float(_price_in(up_in, tier_ctx)) if up_in else 0.0
        reread_usd = float(_price_in(reread, tier_ctx)) if reread else 0.0
        cache_usd = 0.0
    else:
        user_usd = float(up.get("cost_in_usd") or 0)
        cache_usd = 0.0

    if kind == "classic_cache_miss":
        warn = "Session Restart, no cache hit"
    elif kind == "first_call_reread":
        warn = "Context re-read (first-call cache miss)"
    elif kind in ("residual_context_reread", "partial_first_call_miss"):
        warn = "Context re-read (partial cache miss)"
    else:
        warn = "Context re-read (idle / KV miss)"
    up["session_restart"] = True
    up["cache_miss"] = True
    up["context_reread"] = True
    up["context_reread_kind"] = kind
    up["warning"] = warn
    # User prompt = new message only
    up["tokens_in"] = int(up_in)
    up["uncached_est"] = int(up_in)
    up["tokens_cached"] = 0
    up["cached_est"] = 0
    up["cost_in_usd"] = float(user_usd)
    up["cost_cached_usd"] = 0.0
    up["estimate_usd"] = float(user_usd)
    # Re-read lives on the warning / round, not the user prompt In
    up["reread_tokens"] = int(reread)
    up["reread_in_tokens"] = int(reread)
    up["reread_in_usd"] = float(reread_usd)
    gap = hit.get("idle_gap_ms")
    gap_note = ""
    if isinstance(gap, int) and gap > 0:
        gap_note = f" idle_gap={gap / 1000:.0f}s."
    up["note"] = (
        f"{warn}: ~{reread} tok prior re-billed as Input "
        f"(window growth {growth}; official uncached={off_unc}, "
        f"cachedRead={off_cache}).{gap_note} "
        f"Shown on warning; user prompt keeps only new message In."
    )
    up["expected_prior_cached"] = int(prior)
    up["official_cached_read"] = int(off_cache)
    up["official_uncached_input"] = int(off_unc)
    up["window_growth_tokens"] = int(growth)
    r["user_prompt"] = up
    r["session_restart"] = True
    r["context_reread"] = hit
    r["cache_miss"] = True
    r["reread_in_tokens"] = int(reread)
    r["reread_in_usd"] = float(reread_usd)

    bd = dict(r.get("breakdown") or {})
    bd["warm_in_scaled_to_official"] = False
    bd["context_reread"] = True
    bd["reread_tokens"] = int(reread)
    bd["reread_in_tokens"] = int(reread)
    bd["reread_in_usd"] = float(reread_usd)
    bd["user_in_tokens"] = int(up_in)
    bd["user_in_usd"] = float(user_usd)
    bd["user_cached_tokens"] = 0
    bd["user_cached_usd"] = 0.0

    harness_tok = int(bd.get("harness_in_tokens") or 0)
    if allow_h != harness_tok:
        bd["harness_in_tokens"] = int(allow_h)
        try:
            bd["harness_in_usd"] = (
                float(_price_in(allow_h, tier_ctx)) if allow_h and _price_in else 0.0
            )
        except Exception:
            bd["harness_in_usd"] = 0.0
        steps_h = [
            s
            for s in steps
            if int(s.get("harness_in_tokens") or s.get("tokens_in") or 0) > 0
        ]
        old_sum = (
            sum(
                int(s.get("harness_in_tokens") or s.get("tokens_in") or 0)
                for s in steps_h
            )
            or 1
        )
        allocated = 0
        for i, s in enumerate(steps_h):
            old = int(s.get("harness_in_tokens") or s.get("tokens_in") or 0)
            if i == len(steps_h) - 1:
                new_h = max(0, allow_h - allocated)
            else:
                new_h = int(round(allow_h * old / old_sum))
                allocated += new_h
            s["tokens_in"] = new_h
            s["harness_in_tokens"] = new_h
            try:
                s["cost_in_usd"] = (
                    float(_price_in(new_h, tier_ctx)) if new_h and _price_in else 0.0
                )
                s["harness_in_usd"] = s["cost_in_usd"]
            except Exception:
                pass
        harness_tok = int(allow_h)

    # Tree In = reread + user new + harness residual (= official uncached)
    tree = int(reread) + int(up_in) + int(harness_tok)
    bd["tree_in_tokens"] = tree
    try:
        bd["tree_in_usd"] = float(
            (reread_usd or 0)
            + (user_usd or 0)
            + float(bd.get("harness_in_usd") or 0)
        )
    except Exception:
        pass
    r["breakdown"] = bd
    r["tree_in_tokens"] = tree
