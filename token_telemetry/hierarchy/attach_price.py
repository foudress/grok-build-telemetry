"""Moved from finalize.py — same function bodies."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from token_telemetry.pricing import pricing_model_scope, reconstruct_model_step_usage
from token_telemetry.tokenizer import (
    count_chars_as_tokens,
    count_tokens,
    count_user_prompt_tokens,
)
from token_telemetry.hierarchy.bootstrap import (
    _is_compact_continuation,
    inject_tool_definitions_into_bootstrap,
    load_chat_history_reasonings,
    load_chat_history_tool_requests,
    load_chat_history_tool_results,
    parse_session_bootstrap,
    resolve_tool_definitions,
)
from token_telemetry.hierarchy.compact_out import compact_round_inplace
from token_telemetry.hierarchy.tools_meta import _tool_seq_from_id
from token_telemetry.util import as_int as _as_tok

from token_telemetry.hierarchy.tooldef_r1 import (
    _inject_system_message_residual,
    _is_cold_session_first,
)
from token_telemetry.hierarchy.thoughts_enc import _stamp_stream_window

def _attach_step_estimates(hb: Any, r: dict[str, Any]) -> None:
    if reconstruct_model_step_usage is None:
        return
    steps = r.get("model_steps") or []
    if not steps:
        r["step_usage"] = None
        return
    usage = r.get("usage_raw") if r.get("completed") else None
    prior = r.get("cache_baseline_at_start")
    if not isinstance(prior, int):
        # Fallback: previous completed round end (or post-compact)
        if r in hb.rounds:
            idx = hb.rounds.index(r)
            prev = hb.rounds[idx - 1] if idx > 0 else None
        else:
            prev = hb.rounds[-1] if hb.rounds else None
        if prev is not None:
            prior = prev.get("context_after_compact") or prev.get("context_end")
        elif isinstance(hb._cache_baseline, int):
            prior = hb._cache_baseline

    # Zero prior only for a true cold R1 (not a pruned list head).
    if _is_cold_session_first(r):
        prior = None

    # Warm R2+: mild stream under-count only (start still ≥ half of prior).
    # Never rewrite stream_context_start / raw — reconstruct + miss detect
    # need the harness snap. A collapse (start < 0.5×prior) is compact /
    # new window, not a floor-to-prior.
    if isinstance(prior, int) and prior > 0 and steps:
        s0 = steps[0]
        if isinstance(s0, dict):
            cs0 = s0.get("context_start")
            floor_lo = int(prior * 0.5)
            if not isinstance(cs0, int):
                s0["context_start"] = int(prior)
            elif floor_lo <= cs0 < prior:
                s0["context_start"] = int(prior)

    # Stamp harness snaps before reconstruct so cache weights see the raw window.
    # R1 F may already have bumped stream_context_start; do not overwrite.
    for s in steps:
        if isinstance(s, dict):
            _stamp_stream_window(s)

    # Detect full-context re-read before pricing so harness is not scaled
    # to the re-billed prior mass.
    if not isinstance(r.get("idle_gap_ms"), int):
        gap = hb._compute_idle_gap_ms(r)
        if isinstance(gap, int):
            r["idle_gap_ms"] = gap
    reread = hb._detect_context_reread(r)
    if reread:
        r["context_reread"] = reread

    # Reserve pure user-prompt In (not prev LLM Out) from harness scale pool
    up0 = r.get("user_prompt") if isinstance(r.get("user_prompt"), dict) else {}
    user_unc = 0
    try:
        user_unc = int(up0.get("tokens_in") or up0.get("uncached_est") or 0)
    except (TypeError, ValueError):
        user_unc = 0
    if user_unc <= 0 and isinstance(prior, int) and steps:
        cs0 = steps[0].get("context_start") if isinstance(steps[0], dict) else None
        if isinstance(cs0, int) and cs0 > prior:
            user_unc = max(0, int(cs0) - int(prior))

    # Peel child-session official usage out of the parent turn bill
    # (including R1). get_command wait-round only. Always peel from the
    # unpeeled original so reprice is idempotent.
    # Late import: session.__init__ pulls monitor → hierarchy (cycle).
    from token_telemetry.session.subagents import (
        attach_subagents_after_steps,
        child_ids_to_peel,
        collect_child_ids_from_round,
        peel_round_usage,
    )

    peel_meta: dict[str, Any] = {"children": [], "peeled": False}
    child_ids = child_ids_to_peel(r)
    if usage and child_ids:
        cache = getattr(hb, "_child_usage_cache", None)
        if cache is None:
            cache = {}
            hb._child_usage_cache = cache
        already: dict[str, Any] = {}
        if r in hb.rounds:
            prevs = hb.rounds[: hb.rounds.index(r)]
        else:
            prevs = list(hb.rounds)
        for prev in prevs:
            snap = prev.get("child_usage_at_peel")
            if isinstance(snap, dict):
                already.update(snap)
        src = r.get("usage_raw_unpeeled")
        if not isinstance(src, dict) or not src:
            src = dict(usage)
        peeled, peel_meta = peel_round_usage(
            src,
            parent_dir=getattr(hb, "_session_dir", None),
            child_ids=child_ids,
            cache=cache,
            already_peeled=dict(already),
        )
        r["usage_raw_unpeeled"] = dict(src)
        r["subagent_peel"] = peel_meta
        r["child_usage_at_peel"] = {
            c["session_id"]: c.get("usage")
            for c in (peel_meta.get("children") or [])
            if isinstance(c, dict) and c.get("session_id") and c.get("usage")
        }
        if peel_meta.get("peeled"):
            usage = peeled
            r["usage_raw"] = peeled
    attach_subagents_after_steps(r, peel_meta, hb=hb)

    fam = r.get("model_family") or getattr(hb, "_pricing_model", None)
    with pricing_model_scope(fam):
        # Finalize System window (ToolDef+Message bucket) before cache split
        # so C1 Cached = System In + User [1] uses the card size.
        hb._inject_system_message_residual(r, {})
        sys0 = r.get("system_prompt") if isinstance(r.get("system_prompt"), dict) else {}
        try:
            sys_unc = int(sys0.get("tokens_in") or sys0.get("logical_tokens") or 0)
        except (TypeError, ValueError):
            sys_unc = 0
        end_tok = r.get("context_end")
        try:
            end_tok_i = int(end_tok) if end_tok is not None else None
        except (TypeError, ValueError):
            end_tok_i = None
        compact_reentry = None
        cb = r.get("compact_before")
        if isinstance(cb, dict) and cb.get("kind") == "compaction":
            compact_reentry = dict(cb)
            # Between-rounds → User[N] owns Compact Out in UI (reconstruct keeps node).
            if compact_reentry.get("placement") != "mid_round":
                compact_reentry["_attribution"] = "user"
                if not compact_reentry.get("placement"):
                    compact_reentry["placement"] = "between_rounds"
        elif not r.get("mid_round_compacts"):
            prev = None
            if r in hb.rounds:
                idx = hb.rounds.index(r)
                prev = hb.rounds[idx - 1] if idx > 0 else None
            elif hb.rounds:
                prev = hb.rounds[-1]
            if isinstance(prev, dict):
                pst = [
                    s
                    for s in (prev.get("model_steps") or [])
                    if isinstance(s, dict)
                ]
                if pst:
                    cas = [
                        c
                        for c in (pst[-1].get("compacts_after") or [])
                        if isinstance(c, dict) and c.get("kind") == "compaction"
                    ]
                    if cas:
                        compact_reentry = dict(cas[-1])
                        if compact_reentry.get("placement") == "mid_round":
                            compact_reentry["_attribution"] = "harness"
                        else:
                            compact_reentry["_attribution"] = "user"
        recon = reconstruct_model_step_usage(
            steps,
            official_usage=usage,
            prior_context_tokens=prior if isinstance(prior, int) else None,
            user_uncached_tokens=int(user_unc),
            system_uncached_tokens=int(sys_unc),
            context_end_tokens=end_tok_i,
            compact_reentry=compact_reentry,
        )
        r["model_steps"] = recon["steps"]
        r["step_usage"] = {
            "method": recon["method"],
            "calibrated": recon["calibrated"],
            "totals": recon["totals"],
            "breakdown": recon.get("breakdown") or {},
            "note": recon.get("note"),
            "prior_context_tokens": prior,
            "bootstrap_residual_tokens": recon.get("bootstrap_residual_tokens") or 0,
        }
        # Surface cost parts on the round for compact UI headers
        totals = recon.get("totals") or {}
        bd = recon.get("breakdown") or {}
        r["cost_in_usd"] = totals.get("cost_in_usd")  # paid@start (API)
        r["cost_tree_in_usd"] = totals.get("cost_tree_in_usd") or bd.get("tree_in_usd")
        r["tree_in_tokens"] = totals.get("tree_in") or bd.get("tree_in_tokens")
        r["cost_cached_usd"] = totals.get("cost_cached_usd")
        r["cost_out_usd"] = totals.get("cost_out_usd")
        r["estimate_usd"] = totals.get("cost_usd")
        r["breakdown"] = bd
        r["bootstrap_residual_tokens"] = int(recon.get("bootstrap_residual_tokens") or 0)

        # Inject System Tool Definitions (jump / 1-call official−stream floor)
        hb._inject_system_message_residual(r, recon)

        # Compact cost = re-read(kept ctx) + deferred reload (tools/system)
        hb._fill_compact_cost(r)

        # Displayed call window stays on the harness stream snap (all rounds,
        # including R1). Reconstructed Input is stored as api_input_tokens.
        _anchor_call_context_to_input(r)
        # Last call has no harness after it — skip its ctx point and shift
        # each earlier call to the *next* prompt window (Call 1 no longer
        # plots the opening; System/User already have that).
        _apply_caused_context_display(r)

def _anchor_call_context_to_input(r: dict[str, Any]) -> None:
    """Keep displayed call context on the harness stream window.

    Never overwrite context_start with reconstructed Input. Store the API
    prompt on the step as api_input_tokens. stream_context_raw (when set)
    is the true harness snap; stream_context_start may be the F-bumped
    reconstruct weight for R1 call-1.
    """
    steps = [s for s in (r.get("model_steps") or []) if isinstance(s, dict)]
    if not steps:
        return

    for s in steps:
        if isinstance(s.get("context_start"), int) and s.get("stream_context_start") is None:
            s["stream_context_start"] = s["context_start"]
        if isinstance(s.get("context_end"), int) and s.get("stream_context_end") is None:
            s["stream_context_end"] = s["context_end"]

        se = s.get("estimate") if isinstance(s.get("estimate"), dict) else {}
        inp = se.get("input_tokens")
        if inp is None:
            inp = int(s.get("tokens_cached") or 0) + int(s.get("paid_at_start_tokens") or 0)
        try:
            inp_i = max(0, int(inp or 0))
        except (TypeError, ValueError):
            inp_i = 0
        s["api_input_tokens"] = inp_i

        raw = s.get("stream_context_raw")
        stream_start = s.get("stream_context_start")
        stream_end = s.get("stream_context_end")
        if isinstance(raw, int):
            start = raw
        elif isinstance(stream_start, int):
            start = stream_start
        else:
            start = s.get("context_start")
        end = stream_end if isinstance(stream_end, int) else s.get("context_end")
        if not isinstance(start, int):
            continue
        s["context_start"] = start
        if not isinstance(end, int):
            end = start
        s["context_end"] = max(start, end)
        s["context_delta"] = max(0, s["context_end"] - s["context_start"])

def _own_stream_window(s: dict[str, Any]) -> tuple[Optional[int], Optional[int]]:
    raw = s.get("stream_context_raw")
    start = raw if isinstance(raw, int) else s.get("context_start")
    end = s.get("context_end")
    if not isinstance(start, int):
        return None, end if isinstance(end, int) else None
    if not isinstance(end, int):
        end = start
    return int(start), int(end)

def _apply_caused_context_display(r: dict[str, Any]) -> None:
    """Skip last-call context (no harness after it) and shift the rest +1.

    Call i (i < last) displays call i+1's stream window — the prompt after
    this call's Out+tools. Cold Call 1 starts at 0 so System + User/Super
    Agent prompt are inside that first window. n==1 keeps its own window.
    """
    steps = [s for s in (r.get("model_steps") or []) if isinstance(s, dict)]
    n = len(steps)
    if n == 0:
        return
    cold = isinstance(r.get("system_prompt"), dict) and (
        r["system_prompt"].get("kind") == "system_prompt"
    )
    owns = [_own_stream_window(s) for s in steps]
    for i, s in enumerate(steps):
        own_s, own_e = owns[i]
        s["own_context_start"] = own_s
        s["own_context_end"] = own_e
        if n < 2:
            s["skip_context"] = False
            s["display_context_start"] = 0 if cold else own_s
            s["display_context_end"] = own_e
            continue
        if i == n - 1:
            s["skip_context"] = True
            s["display_context_start"] = None
            s["display_context_end"] = None
            s["context_growth_est"] = 0
            s["context_growth_raw"] = 0
            continue
        nxt_s, nxt_e = owns[i + 1]
        s["skip_context"] = False
        # Cold Call 1: window includes System + first user / Super Agent prompt.
        start = 0 if (cold and i == 0) else nxt_s
        s["display_context_start"] = start
        s["display_context_end"] = nxt_s if (cold and i == 0) else (
            nxt_e if nxt_e is not None else nxt_s
        )
        base = 0 if (cold and i == 0) else own_s
        if isinstance(nxt_s, int) and isinstance(base, int):
            s["context_growth_est"] = max(0, int(nxt_s) - int(base))
            s["context_growth_raw"] = max(0, int(nxt_s) - int(base))

def _enc_stamp_signature(hb: Any) -> tuple:
    """Fingerprint of encrypted_content stamps across completed rounds."""
    parts: list[int] = []
    for rr in hb.rounds:
        for s in rr.get("model_steps") or []:
            if isinstance(s, dict):
                parts.append(int(s.get("thought_encrypted_chars") or 0))
    return tuple(parts)

def _reprice_completed_rounds(hb: Any) -> None:
    """Rebuild step trees + pricing after encrypted stamps move."""
    for rr in hb.rounds:
        for step in rr.get("model_steps") or []:
            if isinstance(step, dict):
                hb._finalize_step(step)
        hb._attach_step_estimates(rr)
        from token_telemetry.hierarchy.gen_rate import attach_gen_rates

        attach_gen_rates(rr)
        try:
            hb._price_bootstrap_prompts(rr)
        except Exception:
            pass
        try:
            hb._apply_session_restart_cache_miss(rr)
        except Exception:
            pass
        try:
            hb._attach_prev_llm_answer(rr)
        except Exception:
            pass
        try:
            hb._merge_bootstrap_into_breakdown(rr)
        except Exception:
            pass
        compact_round_inplace(rr)
