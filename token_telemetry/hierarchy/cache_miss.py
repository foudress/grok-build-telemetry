"""Cache-miss / context-reread / session-restart attribution.

Free functions take a HierarchyBuilder-like object as first arg ``hb``.
HierarchyBuilder methods are thin wrappers so behavior stays identical.
"""

from __future__ import annotations

from typing import Any, Optional

from token_telemetry.tokenizer import (
    count_chars_as_tokens,
    count_tokens,
    count_user_prompt_tokens,
)


def _between_rounds_compact(r: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Compact card between prev round and this one (not mid-round on a step)."""
    cb = r.get("compact_before")
    if not isinstance(cb, dict) or cb.get("kind") != "compaction":
        return None
    if cb.get("placement") == "mid_round":
        return None
    return cb


def _attach_user_compact_out(up: dict[str, Any], compact: dict[str, Any]) -> None:
    """Stamp Compact Out on User[N] (between-rounds re-entry — not Call-1 harness)."""
    try:
        out_tok = int(compact.get("out_tokens") or compact.get("tokens_after") or 0)
    except (TypeError, ValueError):
        out_tok = 0
    if out_tok <= 0:
        up.pop("compact_out", None)
        return
    try:
        out_usd = float(compact.get("out_usd") or 0)
    except (TypeError, ValueError):
        out_usd = 0.0
    if out_usd <= 0:
        try:
            from token_telemetry.pricing import _price_in

            out_usd = float(_price_in(out_tok, max(out_tok, 1)))
        except Exception:
            out_usd = 0.0
    n = compact.get("n") or compact.get("index") or compact.get("compact_index")
    up["compact_out"] = {
        "kind": "compact_out",
        "compact_index": int(n) if isinstance(n, (int, float)) and int(n) > 0 else None,
        "tokens_in": int(out_tok),
        "tokenizer_tokens": int(out_tok),
        "cost_in_usd": float(out_usd),
        "note": (
            "Compacted history re-enters as User In "
            "(already includes prior LLM answer — not listed again)."
        ),
    }


def _attach_prev_llm_answer(hb: Any, r: dict[str, Any]) -> None:
    """
    Split User uncached into:
      LLM answer round [N-1]  = Thought TokZ + Reasoning TokF + Message TokZ
      prompt                  = user_uncached − that In mass
    so prompt never double-counts the previous answer mass.

    Between-rounds compact: skip LLM answer (already inside Compact Out) and
    attach Compact Out under User[N] instead of Call-1 harness.

    TokZ = tokenizer stamp (summary / message). TokF for reasoning =
    billed encrypted residual (off_reason − Thought − ToolReq), not
    thought_encrypted_tokens (raw encrypt size — often much larger).
    """
    up = r.get("user_prompt")
    if not isinstance(up, dict) or up.get("kind") != "user_prompt":
        return

    # Between-rounds compact: User = prompt + Compact Out (no LLM answer R[n-1]).
    between = _between_rounds_compact(r)
    if between is not None:
        up.pop("prev_llm_answer", None)
        _attach_user_compact_out(up, between)
        try:
            raw_user = int(up.get("tokens_in") or up.get("uncached_est") or 0)
        except (TypeError, ValueError):
            raw_user = 0
        ud = up.get("user_detail") if isinstance(up.get("user_detail"), dict) else {}
        prompt_tz = int(ud.get("user_query_tokens") or 0) + int(
            ud.get("skill_information_tokens") or 0
        )
        if prompt_tz <= 0:
            full = str(r.get("user_text") or up.get("user_text") or "")
            if full:
                try:
                    prompt_tz = int(count_user_prompt_tokens(full))
                except Exception:
                    prompt_tz = max(1, count_chars_as_tokens(len(full)) or 1)
            else:
                preview = str(up.get("preview") or r.get("user_preview") or "")
                if preview:
                    try:
                        prompt_tz = int(count_user_prompt_tokens(preview))
                    except Exception:
                        prompt_tz = max(1, count_chars_as_tokens(len(preview)) or 1)
        prompt_in = int(raw_user)
        prompt_disp = int(prompt_tz) if prompt_tz > 0 else int(prompt_in)
        tier = max(
            int(r.get("context_start") or 0),
            int((up.get("compact_out") or {}).get("tokens_in") or 0),
            prompt_in,
            1,
        )
        try:
            from token_telemetry.pricing import _price_in

            prompt_usd = float(_price_in(prompt_in, tier)) if prompt_in else 0.0
            user_usd = float(_price_in(raw_user, tier)) if raw_user else 0.0
        except Exception:
            prompt_usd = float(up.get("cost_in_usd") or 0)
            user_usd = float(up.get("cost_in_usd") or 0)
        if prompt_tz > 0:
            up["tokenizer_tokens"] = int(prompt_tz)
            up["prompt_tokenizer_tokens"] = int(prompt_tz)
        up["prompt_tokens_in"] = int(prompt_disp)
        up["prompt_cost_in_usd"] = float(prompt_usd)
        up["uncached_est_raw"] = int(raw_user)
        up["tokens_in"] = int(raw_user)
        up["uncached_est"] = int(raw_user)
        up["cost_in_usd"] = float(user_usd)
        co = up.get("compact_out") if isinstance(up.get("compact_out"), dict) else {}
        co_tok = int(co.get("tokens_in") or 0)
        co_usd = float(co.get("cost_in_usd") or 0)
        up["display_in_tokens"] = int(prompt_disp) + int(co_tok)
        up["display_in_usd"] = float(prompt_usd) + float(co_usd)
        cache_usd = float(up.get("cost_cached_usd") or 0)
        up["estimate_usd"] = float(user_usd + cache_usd)
        r["user_prompt"] = up
        return

    up.pop("compact_out", None)
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
    raw_reserved = int(raw_user)
    absorbed = False
    bd_miss = r.get("breakdown") if isinstance(r.get("breakdown"), dict) else {}
    try:
        miss_tok = int(bd_miss.get("cache_miss_in_tokens") or 0)
    except (TypeError, ValueError):
        miss_tok = 0
    try:
        paid_unc = int(
            bd_miss.get("paid_uncached_tokens")
            or bd_miss.get("uncached_in_tokens")
            or 0
        )
    except (TypeError, ValueError):
        paid_unc = 0
    # context_delta reservation can exceed API uncached (R3: 3686 vs paid 2051).
    # Continuity / User In must use paid mass only or tree overshoots the card.
    if paid_unc > 0 and raw_user > paid_unc:
        raw_user = int(paid_unc)

    # Stamp pure-prompt tokZ from full user text (never the 160-char preview)
    ud = up.get("user_detail") if isinstance(up.get("user_detail"), dict) else {}
    prompt_tz = int(ud.get("user_query_tokens") or 0) + int(
        ud.get("skill_information_tokens") or 0
    )
    if prompt_tz <= 0:
        full = str(r.get("user_text") or up.get("user_text") or "")
        if full:
            try:
                prompt_tz = int(count_user_prompt_tokens(full))
            except Exception:
                prompt_tz = max(1, count_chars_as_tokens(len(full)) or 1)
        else:
            preview = str(up.get("preview") or r.get("user_preview") or "")
            if preview:
                try:
                    prompt_tz = int(count_user_prompt_tokens(preview))
                except Exception:
                    prompt_tz = max(1, count_chars_as_tokens(len(preview)) or 1)
    if prompt_tz > 0:
        up["tokenizer_tokens"] = int(prompt_tz)
        up["prompt_tokenizer_tokens"] = int(prompt_tz)

    # Partition User uncached vs prev LLM answer (conserve API off_unc / tree_in).
    # from_user_pool: paid raw already includes answer → display peel only; miss untouched.
    # else: only move mass that actually sits in cache_miss (warm miss≈0 → no inflate).
    from_user_pool = bool(raw_user > 0 and raw_user >= answer_in)

    if from_user_pool:
        prompt_in = max(0, int(raw_user) - int(answer_in))
        user_tree = int(raw_user)
        answer_billed = int(answer_in)
        peel = 0
    else:
        prompt_in = int(raw_user)
        peel = min(int(answer_in), max(0, int(miss_tok)))
        user_tree = int(raw_user) + int(peel)
        answer_billed = int(peel)
        if peel > 0:
            miss_tok = max(0, int(miss_tok) - int(peel))
            bd_miss["cache_miss_in_tokens"] = int(miss_tok)
            if miss_tok <= 0:
                r["cache_miss"] = False

    tier = int(
        last.get("context_end")
        or last.get("context_start")
        or r.get("context_start")
        or max(answer_in, raw_user, 1)
    )
    try:
        from token_telemetry.pricing import _price_in

        answer_usd = float(_price_in(answer_billed, tier)) if answer_billed else 0.0
        prompt_usd = float(_price_in(prompt_in, tier)) if prompt_in else 0.0
        user_usd = float(_price_in(user_tree, tier)) if user_tree else 0.0
        if (not from_user_pool) and peel > 0:
            bd_miss["cache_miss_in_usd"] = (
                float(_price_in(miss_tok, tier)) if miss_tok else 0.0
            )
    except Exception:
        answer_usd = 0.0
        prompt_usd = 0.0
        user_usd = float(up.get("cost_in_usd") or 0)

    if (not from_user_pool) and peel > 0:
        r["breakdown"] = bd_miss

    out_usd = float(last.get("cost_out_usd") or se.get("cost_out_usd") or 0)
    if from_user_pool:
        note = (
            "Last LLM In = Thought TokZ + Reasoning TokF + Message TokZ "
            "(hooks excluded). "
            "Peeled from User uncached so prompt In has no double-count."
        )
    elif peel > 0:
        note = (
            "Last LLM In = Thought TokZ + Reasoning TokF + Message TokZ "
            "(hooks excluded) — billed User In = mass moved out of KV miss "
            f"({peel}/{answer_in})."
        )
    else:
        note = (
            "Last LLM In meta only — prior answer not in this round's uncached "
            "bill (warm cache); not added to User In / tree_in."
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
        # Billed continuity In (tree); full hybrid stamp kept as tokens_in_full.
        "tokens_in": int(answer_billed),
        "tokens_in_full": int(answer_in),
        "cost_out_usd": float(out_usd),
        "cost_in_usd": float(answer_usd),
        "from_user_pool": bool(from_user_pool) and not absorbed,
        "absorbed_in_reread": bool(absorbed),
        "preview": (last.get("message_preview") or "")[:120],
        "note": note,
    }
    prompt_disp = int(prompt_tz) if prompt_tz > 0 else int(prompt_in)
    up["prompt_tokens_in"] = int(prompt_disp)
    up["prompt_cost_in_usd"] = float(prompt_usd)
    # Preserve pre-clamp reservation for debugging (context_delta may exceed paid).
    up["uncached_est_raw"] = int(raw_reserved)
    # User In = prompt + billed continuity only (peel-capped; never invent warm In).
    up["tokens_in"] = int(user_tree)
    up["uncached_est"] = int(user_tree)
    up["cost_in_usd"] = float(user_usd)
    display_answer = int(answer_billed)
    display_in = int(prompt_disp) + int(display_answer)
    display_usd = float(prompt_usd) + float(answer_usd)
    up["display_in_tokens"] = int(display_in)
    up["display_in_usd"] = float(display_usd)
    cache_usd = float(up.get("cost_cached_usd") or 0)
    up["estimate_usd"] = float(user_usd + cache_usd)
    # Keep breakdown.user_* in sync before finalize re-runs (live open rounds).
    if isinstance(bd_miss, dict):
        bd_miss["user_in_tokens"] = int(user_tree)
        bd_miss["user_in_usd"] = float(user_usd)
        r["breakdown"] = bd_miss
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
    # Compact on this round (between-rounds or mid-round): extra is compact
    # Out + rehydrate, not a KV miss / idle reread.
    if isinstance(r.get("compact_before"), dict) and r["compact_before"].get(
        "kind"
    ) == "compaction":
        return None
    if r.get("mid_round_compacts"):
        return None
    if any(
        isinstance(s, dict)
        and any(
            isinstance(c, dict) and c.get("kind") == "compaction"
            for c in (s.get("compacts_after") or [])
        )
        for s in steps
    ):
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
    """Chip on round KV miss. Keep User Cached; do not plant miss under User.

    Miss tokens come from reconstruct (§0.5): off_unc − user − Σ harness
    (R1 also subtracts System). Detector stays a hint (idle gap, compact≠miss).

    We cannot know which prefix missed, so User Cached stays visible even when
    Σ(User Cached + call Cached) may exceed the round Cached total.
    R1 User Cached stays 0 via finalize — this path does not invent it.
    """
    up = r.get("user_prompt")
    if not isinstance(up, dict) or up.get("kind") != "user_prompt":
        return
    bd = dict(r.get("breakdown") or {})
    try:
        miss = int(bd.get("cache_miss_in_tokens") or 0)
    except (TypeError, ValueError):
        miss = 0
    hit = r.get("context_reread")
    if not isinstance(hit, dict):
        hit = _detect_context_reread(hb, r)
        if hit:
            r["context_reread"] = hit
    if miss <= 0:
        return

    kind = ""
    if isinstance(hit, dict):
        kind = str(hit.get("kind") or "")
    if kind == "classic_cache_miss":
        warn = "Session Restart, no cache hit"
    elif kind == "first_call_reread":
        warn = "Context re-read (first-call cache miss)"
    elif kind in ("residual_context_reread", "partial_first_call_miss"):
        warn = "Context re-read (partial cache miss)"
    else:
        warn = "Context re-read (idle / KV miss)"

    # Keep User Cached as attributed (do not zero on miss).
    try:
        user_usd = float(up.get("cost_in_usd") or 0)
    except (TypeError, ValueError):
        user_usd = 0.0
    try:
        cache_usd = float(up.get("cost_cached_usd") or 0)
    except (TypeError, ValueError):
        cache_usd = 0.0
    up["estimate_usd"] = float(user_usd + cache_usd)
    up.pop("reread_in_tokens", None)
    up.pop("reread_in_usd", None)
    up["warning"] = warn
    if kind:
        up["context_reread_kind"] = kind
    up["note"] = (
        f"{warn}: round KV miss {miss} tok "
        "(off_unc − user − harness). Not under User; User Cached kept."
    )
    r["user_prompt"] = up
    r["session_restart"] = True
    r["cache_miss"] = True
    try:
        bd["user_cached_tokens"] = int(
            up.get("tokens_cached") or up.get("cached_est") or 0
        )
    except (TypeError, ValueError):
        bd["user_cached_tokens"] = 0
    bd["user_cached_usd"] = float(up.get("cost_cached_usd") or 0)
    r["breakdown"] = bd
