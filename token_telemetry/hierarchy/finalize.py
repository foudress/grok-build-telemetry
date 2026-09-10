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
    _bootstrap_hist_tokens,
    _call1_out_tokens,
    _estimate_tooldef_message_bucket,
    _first_llm_total_tokens,
    _first_raw_stream_cs,
    _first_tool_call_total_tokens,
    _independent_tooldef_size,
    _inject_system_message_residual,
    _is_cold_session_first,
    _is_first_round,
    _official_input_tokens,
    _peel_tooldef_from_r1,
    _prejump_result_tokens,
    _r1_tree_in_tokens,
    _remainder_rows,
    _scan_first_tt_increase,
    _tooldef_high_total_tokens,
    _tooldef_jump_window,
    stamp_r1_tooldef_jump,
)
from token_telemetry.hierarchy.step_tree import _finalize_step, _is_kept_model_step
from token_telemetry.hierarchy.thoughts_enc import (
    _clear_enc_stamp,
    _enrich_round_thoughts,
    _enrich_session_thoughts,
    _load_reasonings_fresh,
    _load_tool_requests_fresh,
    _load_tool_results_fresh,
    _patch_reasoning_chars_on_trees,
    _reasoning_fp,
    _stamp_step_reasoning,
    _stamp_stream_window,
    _stamp_tool_chat_args,
    _stamp_tool_chat_results,
    _tool_results_sig,
    _unique_reasonings,
)
from token_telemetry.hierarchy.breakdown import (
    _merge_bootstrap_into_breakdown,
    _price_bootstrap_prompts,
)
from token_telemetry.hierarchy.attach_price import (
    _anchor_call_context_to_input,
    _apply_caused_context_display,
    _attach_step_estimates,
    _enc_stamp_signature,
    _own_stream_window,
    _reprice_completed_rounds,
)


def _prefer_real_user_preview(*cands: Any) -> str:
    """Keep the live user query; never show compact continuation glue."""
    kept: list[str] = []
    for c in cands:
        s = str(c or "").strip()
        if not s or _is_compact_continuation(s):
            continue
        kept.append(s)
    return kept[0] if kept else ""

def _finalize_round(hb: Any, r: dict[str, Any]) -> None:
    # Stamp thought sizes from chat_history (encrypted + summary) *before* tree build
    hb._enrich_round_thoughts(r)
    # Re-chain tools for clean serial display + rebuild children
    for step in r.get("model_steps") or []:
        hb._finalize_step(step)

    start = r.get("context_start")
    end = r.get("context_end")
    if isinstance(start, int) and isinstance(end, int):
        r["context_delta"] = end - start
    # If compact(s) on this round, surface total compact delta
    compact_d = 0
    for c in r.get("compactions") or []:
        if isinstance(c.get("context_delta"), int):
            compact_d += c["context_delta"]
    if compact_d:
        r["compact_delta"] = compact_d

    # Drop empty model steps
    kept_steps = []
    for step in r.get("model_steps") or []:
        if _is_kept_model_step(step):
            kept_steps.append(step)
    for i, step in enumerate(kept_steps, 1):
        step["index"] = i
        # strip internal cursor
        step.pop("_tool_cursor", None)
        # Keep LLM Out [N] labels in sync after empty-step drop
        for ch in step.get("children") or []:
            if not isinstance(ch, dict) or ch.get("kind") != "phase_harness":
                continue
            for sub in ch.get("children") or []:
                if isinstance(sub, dict) and sub.get("kind") == "llm_to_in":
                    sub["call_index"] = i
                    sub["label"] = f"LLM Out [{i}]"
    r["model_steps"] = kept_steps
    r["model_step_count"] = len(kept_steps)

    # User prompt node for continuity with previous round baseline
    prior = r.get("cache_baseline_at_start")
    first_cs = kept_steps[0].get("context_start") if kept_steps else None
    user_chars = int(r.get("user_chars") or 0)
    new_tok = None
    if isinstance(prior, int) and isinstance(first_cs, int):
        new_tok = max(0, first_cs - prior)
    elif isinstance(prior, int) and isinstance(r.get("context_start"), int):
        new_tok = max(0, int(r["context_start"]) - prior)
    # Stream often under-reports first R2+ totalTokens (< real prior). Floor
    # user uncached from message chars so we don't show unc=0 / cache-only.
    if (
        isinstance(prior, int)
        and prior > 0
        and (new_tok is None or new_tok <= 0)
        and user_chars > 0
    ):
        new_tok = max(1, count_chars_as_tokens(user_chars) or 1)

    # R1 System card from chat_history. Bump call-1 start by the remainder
    # for reconstruct weights; keep stream_context_raw as the harness snap.
    is_first = _is_first_round(hb, r)
    if is_first and isinstance(first_cs, int) and first_cs > 0:
        s0 = kept_steps[0] if kept_steps else None
        stream_cs = int(first_cs)
        if isinstance(s0, dict) and isinstance(s0.get("stream_context_raw"), int):
            stream_cs = int(s0["stream_context_raw"])
        if isinstance(s0, dict):
            s0["stream_context_raw"] = stream_cs
            if s0.get("stream_context_start") is None:
                s0["stream_context_start"] = stream_cs

        tool_defs = resolve_tool_definitions(hb._session_dir)
        # TokZ as In (no pro-rata fill of stream start — that swallowed subcats).
        boot = parse_session_bootstrap(
            hb._session_dir,
            target_tokens=stream_cs,
            hooks=hb._bootstrap_hooks,
        )
        boot = inject_tool_definitions_into_bootstrap(boot, tool_defs)
        bucket = _estimate_tooldef_message_bucket(r, boot, kept_steps)
        if isinstance(s0, dict):
            s0["stream_context_raw"] = stream_cs
            # Tool Definitions sit on System, not as a Call-1 reconstruct bump.
            s0["stream_context_start"] = stream_cs
            s0["tool_definitions_tokens"] = 0
            s0["tool_definitions_count"] = int(tool_defs.get("count") or 0)
            s0["tool_definitions_source"] = tool_defs.get("source")
            # Display window stays on the raw harness snap
            s0["context_start"] = stream_cs
        first_cs = stream_cs

        hb._session_bootstrap = boot
        r["session_bootstrap"] = boot
        td_src = str(tool_defs.get("source") or "")
        try:
            indep_tok = int(tool_defs.get("tokens") or 0)
        except (TypeError, ValueError):
            indep_tok = 0
        if td_src == "default":
            indep_tok = 0
        r["tool_definitions"] = {
            "tokens": int(bucket),
            "independent_tokens": int(indep_tok),
            "count": int(tool_defs.get("count") or 0),
            "source": tool_defs.get("source"),
        }
        # Stream start can lag official input; bump end so System + R1 In holds.
        hist_tok = _bootstrap_hist_tokens(boot)
        try:
            user_tok = int(boot.get("user_tokens") or 0)
        except (TypeError, ValueError):
            user_tok = 0
        needed_end = int(hist_tok) + int(bucket) + int(user_tok)
        end_now = r.get("context_end")
        if bucket > 0 and isinstance(end_now, int) and needed_end > end_now:
            r["context_end"] = needed_end
            start_now = r.get("context_start")
            if isinstance(start_now, int):
                r["context_delta"] = needed_end - start_now
            else:
                r["context_delta"] = needed_end
        # Compact-like system card (before the round in UI)
        r["system_prompt"] = {
            "kind": "system_prompt",
            "label": "System / tools / reminders / MCP / Tool Definitions",
            "logical_tokens": int(boot.get("system_tokens") or 0),
            "tokens_in": int(boot.get("system_tokens") or 0),
            "uncached_est": int(boot.get("system_tokens") or 0),
            "cached_est": 0,
            "tokens_cached": 0,
            "parts": boot.get("parts") or [],
            "chars": int(boot.get("system_chars") or 0),
            "note": boot.get("note"),
            "source": boot.get("source"),
            "tool_definitions_tokens": int(boot.get("tool_definitions_tokens") or 0),
        }
        ud = boot.get("user_detail") or {}
        true_first = (
            int(kept_steps[0]["context_start"])
            if kept_steps and isinstance(kept_steps[0].get("context_start"), int)
            else stream_cs
        )
        r["user_prompt"] = {
            "kind": "user_prompt",
            "preview": _prefer_real_user_preview(
                r.get("user_preview"), boot.get("user_preview")
            ),
            "chars": int(boot.get("user_chars") or user_chars),
            "prior_context": None,
            "context_at_first_call": true_first,
            "stream_context_start": stream_cs,
            "cached_est": 0,
            "uncached_est": int(boot.get("user_tokens") or 0),
            "tokens_in": int(boot.get("user_tokens") or 0),
            "tokens_cached": 0,
            "input_est": true_first,
            "user_detail": ud,
            "note": (
                "Prompt index 0: <user_query> + <skill_information> "
                "(full skill load), sized from chat_history JSON. "
                "True first prompt also includes silent Tool Definitions "
                f"({int(bucket)} tok from the first totalTokens jump)."
            ),
        }
    elif is_first:
        # Live R1: no first LLM totalTokens yet — still show Round 1 + System.
        tool_defs = resolve_tool_definitions(hb._session_dir)
        boot = parse_session_bootstrap(
            hb._session_dir,
            target_tokens=None,
            hooks=hb._bootstrap_hooks,
        )
        boot = inject_tool_definitions_into_bootstrap(boot, tool_defs)
        hb._session_bootstrap = boot
        r["session_bootstrap"] = boot
        r["system_prompt"] = {
            "kind": "system_prompt",
            "label": "System / tools / reminders / MCP / Tool Definitions",
            "logical_tokens": int(boot.get("system_tokens") or 0),
            "tokens_in": int(boot.get("system_tokens") or 0),
            "uncached_est": int(boot.get("system_tokens") or 0),
            "cached_est": 0,
            "tokens_cached": 0,
            "parts": boot.get("parts") or [],
            "chars": int(boot.get("system_chars") or 0),
            "note": boot.get("note"),
            "source": boot.get("source"),
            "tool_definitions_tokens": int(boot.get("tool_definitions_tokens") or 0),
        }
        r["user_prompt"] = {
            "kind": "user_prompt",
            "preview": _prefer_real_user_preview(
                r.get("user_preview"), boot.get("user_preview")
            ),
            "chars": int(boot.get("user_chars") or user_chars),
            "prior_context": None,
            "context_at_first_call": None,
            "cached_est": 0,
            "uncached_est": int(boot.get("user_tokens") or 0),
            "tokens_in": int(boot.get("user_tokens") or 0),
            "tokens_cached": 0,
            "user_detail": boot.get("user_detail") or {},
            "note": "Live Round 1 (before first LLM context snap).",
        }
    elif not is_first:
        r["system_prompt"] = None
        r["session_bootstrap"] = None
        r["user_prompt"] = {
            "kind": "user_prompt",
            "preview": r.get("user_preview") or "",
            "user_text": r.get("user_text") or "",
            "chars": user_chars,
            "prior_context": prior,
            "context_at_first_call": first_cs,
            "cached_est": prior,
            "uncached_est": new_tok,
            "input_est": (
                (int(prior or 0) + int(new_tok or 0))
                if prior is not None or new_tok is not None
                else None
            ),
            "note": (
                "User message opens the round. cached_est ≈ end of previous round "
                "(or post-compact). uncached_est ≈ growth until first LLM call "
                "(user text + system glue)."
            ),
        }

    # Surface user-section hooks on the user_prompt node (UI after prompt)
    up_hooks = [
        h for h in (r.get("user_hooks") or [])
        if isinstance(h, dict)
    ]
    if isinstance(r.get("user_prompt"), dict):
        r["user_prompt"]["hooks"] = up_hooks

    fam = r.get("model_family") or getattr(hb, "_pricing_model", None)
    with pricing_model_scope(fam):
        hb._attach_step_estimates(r)
        hb._price_bootstrap_prompts(r)
        hb._apply_session_restart_cache_miss(r)
        hb._attach_prev_llm_answer(r)
        hb._merge_bootstrap_into_breakdown(r)
    from token_telemetry.hierarchy.gen_rate import attach_gen_rates

    attach_gen_rates(r)
