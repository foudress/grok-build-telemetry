"""Finalize / pricing-attach orchestration for HierarchyBuilder (S7d).

Free functions take a HierarchyBuilder-like object as first arg ``hb``.
HierarchyBuilder methods are thin wrappers so behavior stays identical.

R1/System window identity (2026-08-13 override):
System + R1 In = context_end. Tool defs + Message is the remainder
after history parts — not official multi-call Σ off_unc, not a hardcoded 8.2k.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from token_telemetry.pricing import pricing_model_scope, reconstruct_model_step_usage
from token_telemetry.tokenizer import (
    count_chars_as_tokens,
    count_tokens,
)

from token_telemetry.hierarchy.bootstrap import (
    _is_compact_continuation,
    inject_tool_definitions_into_bootstrap,
    load_chat_history_reasonings,
    load_chat_history_tool_results,
    parse_session_bootstrap,
    resolve_tool_definitions,
)
from token_telemetry.hierarchy.compact_out import compact_round_inplace
from token_telemetry.hierarchy.tools_meta import _tool_seq_from_id


_DROP_SYS_PARTS = frozenset(
    {"message", "hooks", "tool_definitions", "tool_defs_message"}
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


def _bootstrap_hist_tokens(boot: dict[str, Any]) -> int:
    """System-card history tokens (exclude tool_definitions / message / hooks)."""
    return sum(
        int(p.get("tokens") or 0)
        for p in (boot.get("parts") or [])
        if isinstance(p, dict) and p.get("kind") not in _DROP_SYS_PARTS
    )


def _estimate_tooldef_message_bucket(
    r: dict[str, Any],
    boot: dict[str, Any],
    steps: list[dict[str, Any]],
) -> int:
    """Call-1 reconstruct bump: context_end − user − harness − history."""
    hist = _bootstrap_hist_tokens(boot)
    try:
        user = int(boot.get("user_tokens") or 0)
    except (TypeError, ValueError):
        user = 0
    harness_est = sum(
        int(s.get("harness_pool_tokens") or 0)
        for s in (steps or [])
        if isinstance(s, dict)
    )
    end = r.get("context_end")
    if isinstance(end, int) and end > 0:
        return max(0, int(end) - int(user) - int(harness_est) - int(hist))
    return 0


def _stamp_stream_window(step: dict[str, Any]) -> None:
    """Save harness snaps before reconstruct / F bump. Never overwrite."""
    if not isinstance(step, dict):
        return
    cs = step.get("context_start")
    ce = step.get("context_end")
    if isinstance(cs, int) and step.get("stream_context_start") is None:
        step["stream_context_start"] = cs
    if isinstance(ce, int) and step.get("stream_context_end") is None:
        step["stream_context_end"] = ce


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
        if (
            step.get("thought_chunks")
            or step.get("message_chunks")
            or step.get("tools")
            or abs(int(step.get("context_delta") or 0)) > 0
            or int(step.get("model_emit_delta") or 0) > 0
        ):
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

    # Session bootstrap (first round only): System card + full user prompt
    # from chat_history (user_query + skill_information), not stream chars.
    # Silent ToolDef+Message is the window remainder (not a hardcoded 8.2k).
    # Bump call-1 stream_context_start by that bucket for reconstruct weights;
    # keep the raw harness snap in stream_context_raw for display.
    is_first = prior is None and len(hb.rounds) == 0
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
        # Scale history messages to *raw* stream start only (no tools in history)
        boot = parse_session_bootstrap(
            hb._session_dir,
            target_tokens=stream_cs,
            hooks=hb._bootstrap_hooks,
        )
        boot = inject_tool_definitions_into_bootstrap(boot, tool_defs)
        bucket = _estimate_tooldef_message_bucket(r, boot, kept_steps)
        if isinstance(s0, dict):
            s0["stream_context_raw"] = stream_cs
            s0["stream_context_start"] = stream_cs + bucket
            s0["tool_definitions_tokens"] = bucket
            s0["tool_definitions_count"] = int(tool_defs.get("count") or 0)
            s0["tool_definitions_source"] = tool_defs.get("source")
            # Display window stays on the raw harness snap
            s0["context_start"] = stream_cs
        first_cs = stream_cs

        hb._session_bootstrap = boot
        r["session_bootstrap"] = boot
        r["tool_definitions"] = {
            "tokens": int(bucket),
            "count": int(tool_defs.get("count") or 0),
            "source": tool_defs.get("source"),
        }
        # Compact-like system card (before the round in UI)
        r["system_prompt"] = {
            "kind": "system_prompt",
            "label": "System / tools / reminders / MCP / Message",
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
                "True first prompt also includes silent ToolDef+Message "
                f"({int(bucket)} tok window remainder)."
            ),
        }
    else:
        r["system_prompt"] = None
        r["session_bootstrap"] = None
        r["user_prompt"] = {
            "kind": "user_prompt",
            "preview": r.get("user_preview") or "",
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


def _r1_tree_in_tokens(r: dict[str, Any]) -> int:
    """User uncached + Σ LLM call In. recon.tree_in is harness-only — do not use it."""
    up = r.get("user_prompt") if isinstance(r.get("user_prompt"), dict) else {}
    user_in = int(
        (up or {}).get("tokens_in")
        or (up or {}).get("uncached_est")
        or 0
    )
    steps_m = [s for s in (r.get("model_steps") or []) if isinstance(s, dict)]
    sum_call = sum(
        int(s.get("tokens_in") or s.get("harness_in_tokens") or 0)
        for s in steps_m
    )
    return int(user_in) + int(sum_call)


def _inject_system_message_residual(
    hb: Any, r: dict[str, Any], recon: dict[str, Any]
) -> None:
    """
    Window identity (R1):

        System + R1 In = context_end

    Last LLM Out is next-round In (already folded into later-call In when
    not last). History parts stay tokenized from chat_history. Remainder
    is one bucket — silent tool schemas + unexplained glue:

        ToolDef+Message = max(0, context_end − R1_tree − history)

    History = System + User info + Reminders/skills + MCP (+ other).
    Never use official multi-call Σ off_unc (that inflates Message).
    Never keep a hardcoded 8.2k tool-def part on the System card.
    """
    sys_p = r.get("system_prompt")
    if not isinstance(sys_p, dict) or sys_p.get("kind") != "system_prompt":
        return

    parts = [
        p
        for p in (sys_p.get("parts") or [])
        if isinstance(p, dict) and p.get("kind") not in _DROP_SYS_PARTS
    ]
    known_hist = sum(int(p.get("tokens") or 0) for p in parts)
    r1_tree = _r1_tree_in_tokens(r)

    end = r.get("context_end")
    if isinstance(end, int) and end > 0:
        bucket = max(0, int(end) - int(r1_tree) - int(known_hist))
    else:
        bucket = 0

    tool_meta = r.get("tool_definitions") if isinstance(r.get("tool_definitions"), dict) else {}
    if bucket > 0:
        count = int(tool_meta.get("count") or 0)
        parts.append(
            {
                "kind": "tool_defs_message",
                "label": "Tool definitions + Message",
                "tokens": int(bucket),
                "tokenizer_tokens": int(bucket),
                "chars": 0,
                "tool_count": count or None,
                "preview": (
                    f"{count} tools · context_end − R1 In − history"
                    if count
                    else "context_end − R1 In − history"
                ),
                "messages": 0,
                "note": (
                    "ToolDef+Message = max(0, context_end − R1_In − "
                    "System − User info − Reminders − MCP). "
                    "System + R1 In = context_end. Last LLM Out is next-round In."
                ),
            }
        )

    new_tok = int(known_hist) + int(bucket)
    sys_p["parts"] = parts
    sys_p["tokens_in"] = new_tok
    sys_p["logical_tokens"] = new_tok
    sys_p["uncached_est"] = new_tok
    sys_p["message_residual_tokens"] = int(bucket)
    sys_p["tool_definitions_tokens"] = int(bucket)
    sys_p["note"] = (
        "System + R1 In = context_end. History parts from chat_history; "
        "Tool definitions + Message is the window remainder "
        "(not a hardcoded 8.2k, not official Σ uncached)."
    )

    boot = r.get("session_bootstrap")
    if isinstance(boot, dict):
        boot["system_tokens"] = int(new_tok)
        boot["message_residual_tokens"] = int(bucket)
        boot["tool_definitions_tokens"] = int(bucket)
        bparts = [
            p
            for p in (boot.get("parts") or [])
            if isinstance(p, dict) and p.get("kind") not in _DROP_SYS_PARTS
        ]
        if bucket > 0:
            bparts.append(parts[-1])
        boot["parts"] = bparts
    r["bootstrap_residual_tokens"] = int(bucket)
    if isinstance(r.get("step_usage"), dict):
        r["step_usage"]["bootstrap_residual_tokens"] = int(bucket)

def _merge_bootstrap_into_breakdown(hb: Any, r: dict[str, Any]) -> None:
    """Surface System / User on round.breakdown; tree In includes user uncached."""
    bd = dict(r.get("breakdown") or {})
    su = r.get("step_usage")
    if isinstance(su, dict) and isinstance(su.get("breakdown"), dict):
        bd = {**su["breakdown"], **bd}
    sys_p = r.get("system_prompt")
    up = r.get("user_prompt")
    if isinstance(sys_p, dict) and sys_p.get("kind") == "system_prompt":
        bd["system_in_tokens"] = int(sys_p.get("tokens_in") or 0)
        bd["system_in_usd"] = float(sys_p.get("cost_in_usd") or 0)
        if sys_p.get("message_residual_tokens"):
            bd["system_message_tokens"] = int(sys_p["message_residual_tokens"])
    user_tok = 0
    user_usd = 0.0
    if isinstance(up, dict):
        user_tok = int(up.get("tokens_in") or up.get("uncached_est") or 0)
        user_usd = float(up.get("cost_in_usd") or 0)
        bd["user_in_tokens"] = user_tok
        bd["user_in_usd"] = user_usd
        bd["user_cached_tokens"] = int(
            up.get("tokens_cached") or up.get("cached_est") or 0
        )
        bd["user_cached_usd"] = float(up.get("cost_cached_usd") or 0)
    # Round In = User (+ reread) + Σ LLM call In.
    # Prefer sum of model_steps tokens_in (authoritative call tree) over
    # breakdown harness total so User + Calls always equals Round In.
    steps_m = [s for s in (r.get("model_steps") or []) if isinstance(s, dict)]
    sum_call = 0
    sum_call_usd = 0.0
    for s in steps_m:
        sum_call += int(s.get("tokens_in") or s.get("harness_in_tokens") or 0)
        sum_call_usd += float(s.get("cost_in_usd") or s.get("harness_in_usd") or 0)
    harness_tok = int(sum_call) if steps_m else int(bd.get("harness_in_tokens") or 0)
    harness_usd = (
        float(sum_call_usd) if steps_m else float(bd.get("harness_in_usd") or 0)
    )
    bd["harness_in_tokens"] = harness_tok
    bd["harness_in_usd"] = harness_usd
    reread_tok = int(
        bd.get("reread_in_tokens")
        or bd.get("reread_tokens")
        or r.get("reread_in_tokens")
        or 0
    )
    reread_usd = float(bd.get("reread_in_usd") or r.get("reread_in_usd") or 0)
    cold = bool(bd.get("cold_round")) or (
        isinstance(sys_p, dict) and sys_p.get("kind") == "system_prompt"
    )
    if reread_tok > 0 or bd.get("context_reread") or r.get("session_restart"):
        bd["tree_in_tokens"] = int(reread_tok) + int(user_tok) + harness_tok
        bd["tree_in_usd"] = float(reread_usd) + float(user_usd) + harness_usd
        bd["reread_in_tokens"] = int(reread_tok)
        bd["reread_in_usd"] = float(reread_usd)
    else:
        bd["tree_in_tokens"] = int(user_tok) + harness_tok
        bd["tree_in_usd"] = float(user_usd) + harness_usd
    if cold:
        # R1 window starts at 0 (no prior call). System lives on its own card.
        # Do not peel context_start to System size — that hid ctx 0→end.
        end = r.get("context_end")
        r["context_start"] = 0
        if isinstance(end, int):
            r["context_delta"] = int(end)
        bd["context_start_peeled_system"] = False
        # R1 white total = tree In + Cached + Out (System has its own card).
        # Do not keep full API bill (System + round) on estimate_usd.
        api_tot = float(
            bd.get("total_usd")
            or r.get("estimate_usd")
            or 0
        )
        if api_tot > 0:
            bd["api_total_usd"] = api_tot
        cache_usd = float(
            bd.get("cached_usd")
            if bd.get("cached_usd") is not None
            else (r.get("cost_cached_usd") or 0)
        )
        out_usd = float(
            bd.get("output_usd")
            if bd.get("output_usd") is not None
            else (r.get("cost_out_usd") or 0)
        )
        tree_usd = float(bd.get("tree_in_usd") or 0)
        round_tot = tree_usd + cache_usd + out_usd
        r["estimate_usd"] = round_tot
        bd["total_usd"] = round_tot
        bd["round_total_peeled_system"] = True
        # Session KPI = System card + peeled R1 (no System twice, no missing System).
        sys_usd = float(bd.get("system_in_usd") or 0)
        if isinstance(sys_p, dict):
            sys_usd = float(sys_p.get("cost_in_usd") or sys_usd or 0)
        bd["api_total_usd"] = float(sys_usd) + float(round_tot)
    r["breakdown"] = bd
    r["tree_in_tokens"] = bd.get("tree_in_tokens")
    r["cost_tree_in_usd"] = bd.get("tree_in_usd")
    if isinstance(su, dict):
        su = dict(su)
        su["breakdown"] = bd
        tot = dict(su.get("totals") or {})
        tot["tree_in"] = bd.get("tree_in_tokens")
        tot["tree_in_usd"] = bd.get("tree_in_usd")
        tot["cost_tree_in_usd"] = bd.get("tree_in_usd")
        # Keep API bill under api_cost_usd; surface peeled R1 total on cost_usd
        if bd.get("round_total_peeled_system") and r.get("estimate_usd") is not None:
            if tot.get("cost_usd") is not None and bd.get("api_total_usd") is None:
                bd["api_total_usd"] = float(tot["cost_usd"])
            tot["api_cost_usd"] = bd.get("api_total_usd") or tot.get("cost_usd")
            tot["cost_usd"] = float(r["estimate_usd"])
            tot["cost_tree_in_usd"] = bd.get("tree_in_usd")
        su["totals"] = tot
        r["step_usage"] = su

def _load_reasonings_fresh(hb: Any) -> list[dict[str, Any]]:
    """Reload chat_history reasonings when the file changes (mtime)."""
    if not hb._session_dir:
        return []
    ch_path = Path(hb._session_dir) / "chat_history.jsonl"
    mtime: Optional[float] = None
    try:
        if ch_path.is_file():
            mtime = float(ch_path.stat().st_mtime)
    except OSError:
        mtime = None
    if (
        hb._reasonings_cache is None
        or mtime is None
        or mtime != hb._reasonings_mtime
    ):
        hb._reasonings_cache = load_chat_history_reasonings(hb._session_dir)
        hb._reasonings_mtime = mtime
    return list(hb._reasonings_cache or [])

def _load_tool_results_fresh(hb: Any) -> dict[str, dict[str, Any]]:
    """Reload chat_history tool_result content tokens when file changes."""
    if not hb._session_dir:
        return {}
    ch_path = Path(hb._session_dir) / "chat_history.jsonl"
    mtime: Optional[float] = None
    try:
        if ch_path.is_file():
            mtime = float(ch_path.stat().st_mtime)
    except OSError:
        mtime = None
    if (
        hb._tool_results_cache is None
        or mtime is None
        or mtime != hb._tool_results_mtime
    ):
        hb._tool_results_cache = load_chat_history_tool_results(hb._session_dir)
        hb._tool_results_mtime = mtime
    return dict(hb._tool_results_cache or {})

def _stamp_tool_chat_results(hb: Any, tools: list[dict[str, Any]]) -> None:
    """Attach chat_history tool_result.content tokenizer size onto harness tools.

    chat_history is authoritative for result **chars** and tokenizer **weights**
    (stream tool_call_update envelopes under-count / truncate tools like grep).
    """
    by_id = hb._load_tool_results_fresh()
    if not by_id:
        return
    for t in tools:
        if not isinstance(t, dict):
            continue
        tid = t.get("tool_call_id")
        if not tid:
            continue
        hit = by_id.get(str(tid))
        if not hit:
            continue
        ch_chars = int(hit.get("content_chars") or 0)
        ch_tok = int(hit.get("content_tokens") or 0)
        t["ch_result_chars"] = ch_chars
        t["ch_result_tokens"] = ch_tok
        # Prefer history body for UI chars + harness pro-rata weights.
        if ch_chars > 0:
            t["result_chars"] = ch_chars
        if ch_tok > 0:
            t["result_tokens_est"] = ch_tok
            t["weight_source"] = "chat_history_tokenizer"
        if hit.get("preview") and not t.get("result_preview"):
            t["result_preview"] = hit["preview"]

def _stamp_step_reasoning(step: dict[str, Any], rs: dict[str, Any]) -> None:
    """Apply one chat_history reasoning item onto a model step."""
    full_c = int(rs.get("full_chars") or 0)
    enc_c = int(rs.get("encrypted_chars") or 0)
    sum_c = int(rs.get("summary_chars") or 0)
    sum_text = rs.get("summary_text") or ""
    if isinstance(sum_text, str) and sum_text:
        sum_tok = int(rs.get("summary_tokens") or count_tokens(sum_text))
    else:
        sum_tok = int(rs.get("summary_tokens") or 0)
    enc_tok = int(rs.get("encrypted_tokens") or 0)
    if enc_tok <= 0 and enc_c > 0:
        enc_tok = count_chars_as_tokens(enc_c)
    # thought_chars / summary = readable Out weight only.
    # encrypted_content sizes weight the Reasoning share of reasoningTokens.
    stream_c = int(step.get("thought_chars") or 0)
    stream_text = step.get("thought_summary_text") or ""
    if sum_c >= stream_c and sum_text:
        step["thought_summary_text"] = sum_text
        step["thought_summary_chars"] = sum_c
        step["thought_summary_tokens"] = sum_tok
        step["thought_chars"] = sum_c
    else:
        step["thought_summary_chars"] = max(
            int(step.get("thought_summary_chars") or 0), sum_c, stream_c
        )
        step["thought_chars"] = max(stream_c, sum_c)
        if stream_text:
            step["thought_summary_tokens"] = count_tokens(stream_text)
        elif sum_tok > 0:
            step["thought_summary_tokens"] = sum_tok
    # Absolute assign (session remap clears first)
    step["thought_encrypted_chars"] = int(enc_c)
    step["thought_encrypted_tokens"] = int(enc_tok)
    step["thought_full_json_chars"] = int(full_c)
    if rs.get("preview") and not step.get("thought_preview"):
        step["thought_preview"] = rs["preview"]
    if not step.get("thought_chunks"):
        step["thought_chunks"] = 1

def _enrich_session_thoughts(hb: Any) -> None:
    """
    Map chat_history reasoning items (encrypted_content + summary) onto
    model_steps across the whole session.

    chat_history items map 1:1 onto model_steps in order. Extra rows merge
    into the last step. Reconstruct still splits official Enc residual
    across calls that thought when a stamp is missing.
    """
    if not hb._session_dir:
        return
    reasonings = hb._load_reasonings_fresh()
    if not reasonings:
        return

    rounds: list[dict[str, Any]] = list(hb.rounds)
    if hb._open is not None and hb._open not in rounds:
        rounds.append(hb._open)

    steps_flat: list[dict[str, Any]] = []
    for rr in rounds:
        for s in rr.get("model_steps") or []:
            if isinstance(s, dict):
                steps_flat.append(s)
    if not steps_flat:
        return

    # Clear prior history stamps so remap is idempotent
    for s in steps_flat:
        s["thought_encrypted_chars"] = 0
        s["thought_encrypted_tokens"] = 0
        s["thought_full_json_chars"] = 0

    r_n, s_n = len(reasonings), len(steps_flat)
    # 1:1 chronological. Extra reasonings merge into the last step.
    # Reconstruct still splits official Enc residual by thought when a
    # step has no history stamp — do not even-spread (that skips calls).
    for i, rs in enumerate(reasonings):
        if i < s_n:
            hb._stamp_step_reasoning(steps_flat[i], rs)
        else:
            last = steps_flat[-1]
            last["thought_encrypted_chars"] = int(
                last.get("thought_encrypted_chars") or 0
            ) + int(rs.get("encrypted_chars") or 0)
            last["thought_encrypted_tokens"] = int(
                last.get("thought_encrypted_tokens") or 0
            ) + int(rs.get("encrypted_tokens") or 0)

def _patch_reasoning_chars_on_trees(hb: Any) -> None:
    """Push step thought_encrypted/summary chars+TokZ onto existing LLM children."""
    rounds: list[dict[str, Any]] = list(hb.rounds)
    if hb._open is not None and hb._open not in rounds:
        rounds.append(hb._open)
    for rr in rounds:
        for step in rr.get("model_steps") or []:
            if not isinstance(step, dict):
                continue
            enc_c = int(step.get("thought_encrypted_chars") or 0)
            enc_tok = int(step.get("thought_encrypted_tokens") or 0)
            if enc_tok <= 0 and enc_c > 0:
                enc_tok = count_chars_as_tokens(enc_c)
            sum_c = int(
                step.get("thought_summary_chars")
                or step.get("thought_chars")
                or 0
            )
            sum_tok = int(step.get("thought_summary_tokens") or 0)
            for ch in step.get("children") or []:
                if not isinstance(ch, dict):
                    continue
                kids = ch.get("children") if ch.get("kind") == "phase_llm" else None
                if kids is None and ch.get("kind") in ("reasoning", "thought"):
                    kids = [ch]
                if not kids:
                    continue
                for c in kids:
                    if not isinstance(c, dict):
                        continue
                    if c.get("kind") == "reasoning":
                        c["chars"] = enc_c
                        c["encrypted_chars"] = enc_c
                        c["encrypted_tokens"] = int(enc_tok)
                        c["tokenizer_tokens"] = int(enc_tok)
                    elif c.get("kind") == "thought":
                        c["chars"] = sum_c
                        c["summary_chars"] = sum_c
                        if sum_tok > 0:
                            c["summary_tokens"] = sum_tok
                            c["tokenizer_tokens"] = sum_tok

def _enrich_round_thoughts(hb: Any, r: dict[str, Any]) -> None:
    """Stamp encrypted/summary sizes from chat_history (session-wide map)."""
    # Full remount keeps late rounds supplied when history grows.
    hb._enrich_session_thoughts()
    # Ensure this round's stream-only steps still have summary floors
    for step in r.get("model_steps") or []:
        if not isinstance(step, dict):
            continue
        stream_c = int(step.get("thought_chars") or 0)
        if stream_c > 0:
            step["thought_summary_chars"] = max(
                int(step.get("thought_summary_chars") or 0), stream_c
            )
    hb._patch_reasoning_chars_on_trees()

def _price_bootstrap_prompts(hb: Any, r: dict[str, Any]) -> None:
    """
    Price system card (R1) + user prompt on *every* round.

    Round In tree total = user uncached In + sum(LLM call In).
    Later rounds: prior context = Cached on user row; new growth = In.
    """
    try:
        from token_telemetry.pricing import _price_in, _price_cache
    except ImportError:
        try:
            from token_telemetry.pricing import _price_in
            _price_cache = None  # type: ignore
        except ImportError:
            return

    steps = r.get("model_steps") or []
    sys_p = r.get("system_prompt")
    up = r.get("user_prompt")
    if not isinstance(up, dict):
        return

    tier_ctx = int(
        (steps[0].get("context_start") if steps else 0)
        or (r.get("context_start") or 0)
        or 1
    )

    if isinstance(sys_p, dict) and sys_p.get("kind") == "system_prompt":
        sys_log = int(sys_p.get("logical_tokens") or sys_p.get("tokens_in") or 0)
        user_log = int(up.get("uncached_est") or up.get("tokens_in") or 0)
        tier_ctx = int(tier_ctx or (sys_log + user_log) or 1)
        sys_usd = _price_in(sys_log, tier_ctx)
        user_usd = _price_in(user_log, tier_ctx)
        parts = list(sys_p.get("parts") or [])
        if parts and sys_log > 0:
            for p in parts:
                ptok = int(p.get("tokens") or 0)
                p["cost_in_usd"] = float(_price_in(ptok, tier_ctx))
                p["tokens_in"] = ptok
        sys_p["parts"] = parts
        sys_p["tokens_in"] = sys_log
        sys_p["uncached_est"] = sys_log
        sys_p["logical_tokens"] = sys_log
        sys_p["tokens_cached"] = 0
        sys_p["cost_in_usd"] = float(sys_usd)
        sys_p["cost_cached_usd"] = 0.0
        sys_p["estimate_usd"] = float(sys_usd)
        up["tokens_in"] = user_log
        up["uncached_est"] = user_log
        up["tokens_cached"] = 0
        up["cached_est"] = 0
        up["cost_in_usd"] = float(user_usd)
        up["cost_cached_usd"] = 0.0
        up["estimate_usd"] = float(user_usd)
        ud = up.get("user_detail") if isinstance(up.get("user_detail"), dict) else {}
        tz = int(ud.get("user_query_tokens") or 0) + int(
            ud.get("skill_information_tokens") or 0
        )
        if tz <= 0 and user_log > 0:
            tz = int(user_log)
        if tz > 0:
            up["tokenizer_tokens"] = int(tz)
            up["prompt_tokenizer_tokens"] = int(tz)
        r["system_prompt"] = sys_p
        r["user_prompt"] = up
        if hb._session_bootstrap:
            hb._session_bootstrap["priced"] = True
        return

    # Later rounds: user uncached (new) + continuity cache.
    # Prefer reconstruct's user_cache_share so user + Σ call cache == official.
    user_log = int(up.get("uncached_est") or up.get("tokens_in") or 0)
    try:
        cache_log = int(up.get("cached_est") or up.get("tokens_cached") or 0)
    except (TypeError, ValueError):
        cache_log = 0
    bd0 = r.get("breakdown") if isinstance(r.get("breakdown"), dict) else {}
    try:
        share = int(bd0.get("user_cache_share_tokens") or 0)
    except (TypeError, ValueError):
        share = 0
    if share > 0:
        cache_log = share
    elif isinstance(up.get("prior_context"), int) and cache_log <= 0:
        cache_log = int(up["prior_context"] or 0)
    tier_ctx = int(
        tier_ctx
        or (cache_log + user_log)
        or 1
    )
    user_usd = float(_price_in(user_log, tier_ctx))
    cache_usd = 0.0
    if cache_log > 0 and _price_cache is not None:
        cache_usd = float(_price_cache(cache_log, tier_ctx))
    elif cache_log > 0:
        # fallback: same as live_dashboard estimate_cost_usd cache part
        try:
            from token_telemetry.pricing import estimate_cost_usd
            est = estimate_cost_usd(
                input_tokens=cache_log + user_log,
                output_tokens=0,
                cached_read_tokens=cache_log,
                peak_context_tokens=cache_log + user_log,
                model_calls=1,
            )
            user_usd = float(est["cost_usd"]["uncached_input"])
            cache_usd = float(est["cost_usd"]["cached_input"])
        except Exception:
            pass
    up["tokens_in"] = user_log
    up["uncached_est"] = user_log
    up["tokens_cached"] = cache_log
    up["cached_est"] = cache_log
    up["cost_in_usd"] = user_usd
    up["cost_cached_usd"] = cache_usd
    up["estimate_usd"] = float(user_usd + cache_usd)
    if not up.get("tokenizer_tokens"):
        ud = up.get("user_detail") if isinstance(up.get("user_detail"), dict) else {}
        tz = int(ud.get("user_query_tokens") or 0) + int(
            ud.get("skill_information_tokens") or 0
        )
        if tz <= 0:
            prevw = str(up.get("preview") or r.get("user_preview") or "")
            if prevw:
                try:
                    tz = int(count_tokens(prevw))
                except Exception:
                    tz = max(1, count_chars_as_tokens(len(prevw)) or 1)
        if tz > 0:
            up["tokenizer_tokens"] = int(tz)
            up["prompt_tokenizer_tokens"] = int(tz)
    r["user_prompt"] = up

def _finalize_step(hb: Any, step: dict[str, Any]) -> None:
    s0 = step.get("context_start")
    s1 = step.get("context_end")
    _stamp_stream_window(step)
    if isinstance(s0, int) and isinstance(s1, int):
        # Compact / totalTokens noise can briefly drop end below start —
        # never show a negative window growth on the call line.
        step["context_delta"] = max(0, s1 - s0)

    raw_tools = list(step.get("tools") or [])
    cleaned: list[dict[str, Any]] = []
    for t in raw_tools:
        name = t.get("name") or "tool"
        tt_obs = int(t.get("tt_delta_observed") or t.get("context_delta") or 0)
        if name == "tool" and tt_obs == 0 and not t.get("title") and not t.get("result_chars"):
            continue
        tid = t.get("tool_call_id")
        cleaned.append(
            {
                "kind": "tool",
                "tool_call_id": tid,
                "tool_seq": t.get("tool_seq")
                if t.get("tool_seq") is not None
                else _tool_seq_from_id(tid if isinstance(tid, str) else None),
                "name": name,
                "title": t.get("title"),
                "status": t.get("status"),
                "path": t.get("path"),
                "offset": t.get("offset"),
                "limit": t.get("limit"),
                "result_chars": int(t.get("result_chars") or 0),
                "result_lines": int(t.get("result_lines") or 0),
                "result_tokens_est": int(t.get("result_tokens_est") or 0),
                "result_preview": t.get("result_preview"),
                "ch_result_chars": int(t.get("ch_result_chars") or 0),
                "ch_result_tokens": int(t.get("ch_result_tokens") or 0),
                "arg_chars": int(t.get("arg_chars") or 0),
                "arg_tokens_est": int(t.get("arg_tokens_est") or 0),
                "tt_delta_observed": max(0, tt_obs),
                "context_delta": max(0, tt_obs),  # may be redistributed below
                "context_before": t.get("context_before"),
                "context_after": t.get("context_after"),
                "declare_ctx": t.get("declare_ctx"),
                "plan": t.get("plan"),
                "is_plan": bool(t.get("is_plan") or t.get("plan")),
                "subagent_id": t.get("subagent_id"),
                "subagent_ids": list(t.get("subagent_ids") or []) or None,
                "subagent_type": t.get("subagent_type"),
                "subagent_description": t.get("subagent_description"),
            }
        )
    # Stamp chat_history tool_result (chars + tokenizer weights; authoritative)
    hb._stamp_tool_chat_results(cleaned)
    for t in cleaned:
        # also keep on step.tools for later reprice
        tid = t.get("tool_call_id")
        if not tid:
            continue
        for raw_t in raw_tools:
            if raw_t.get("tool_call_id") == tid:
                raw_t["ch_result_chars"] = t.get("ch_result_chars")
                raw_t["ch_result_tokens"] = t.get("ch_result_tokens")
                raw_t["result_chars"] = t.get("result_chars")
                raw_t["result_tokens_est"] = t.get("result_tokens_est")
                if t.get("weight_source"):
                    raw_t["weight_source"] = t.get("weight_source")
                break

    emit_d = int(step.get("model_emit_delta") or 0)
    # Fallback emit from tool arg sizes (search_replace old/new often huge)
    # Note: arg sizes are for LLM→Harness Out only — never harness In weights.
    arg_emit = sum(int(t.get("arg_tokens_est") or 0) for t in cleaned)
    if emit_d <= 0 and arg_emit > 0:
        emit_d = arg_emit
        step["model_emit_delta"] = emit_d
        step["model_emit_from_args"] = True
    elif emit_d > 0 and arg_emit > emit_d * 2:
        # Counter under-reported tool-call stream; prefer arg estimate for pricing weights
        step["model_emit_arg_tokens"] = arg_emit

    late = max(0, int(step.get("late_context_delta") or 0))
    tt_tools_sum = sum(int(t.get("tt_delta_observed") or 0) for t in cleaned)
    # Tokenizer result weights (history when stamped; else stream envelope)
    content_sum = sum(int(t.get("result_tokens_est") or 0) for t in cleaned)

    # --- One tool-In estimator: tokenizer result weights + late into tools ---
    ambiguous = len(cleaned) > 1
    attribution = "token_counter"
    late_absorbed = 0

    def _raw_tool_weight(t: dict[str, Any]) -> int:
        # ch_result_tokens preferred; stamp already copied into result_tokens_est
        content = max(
            0,
            int(t.get("ch_result_tokens") or 0)
            or int(t.get("result_tokens_est") or 0),
        )
        tt = max(0, int(t.get("tt_delta_observed") or 0))
        # Status-only / empty payload but counter moved → tt is the signal
        if content < 48 and tt > max(content * 3, 32):
            return tt
        if content > 0:
            return content
        return tt

    def _weights_for(tools: list[dict[str, Any]]) -> list[int]:
        """
        Split harness pool by tokenizer result tokens (not args).

        Prefer ch_result_tokens / result_tokens_est. Fallback to tt only
        for status-only tools ("ok" / tiny payload) that still moved the
        counter (search_replace, etc.). Never use arg_chars/arg_tokens.
        """
        ws = [_raw_tool_weight(t) for t in tools]
        if sum(ws) <= 0:
            return [1] * len(tools)
        return [max(1, w) if w > 0 else 0 for w in ws]

    def _allocate_pool(tools: list[dict[str, Any]], pool: int, attr: str) -> None:
        if not tools or pool <= 0:
            for t in tools:
                t["context_delta"] = 0
                t["attribution"] = attr
            return
        weights = _weights_for(tools)
        wsum = sum(weights) or 1
        allocated = 0
        last_i = max((i for i, w in enumerate(weights) if w > 0), default=len(tools) - 1)
        for i, t in enumerate(tools):
            if weights[i] <= 0:
                t["context_delta"] = 0
                t["attribution"] = attr
                continue
            if i == last_i:
                d = max(0, pool - allocated)
            else:
                d = int(round(pool * weights[i] / wsum))
                allocated += d
            t["context_delta"] = max(0, d)
            t["attribution"] = attr

    if cleaned:
        signal = sum(_raw_tool_weight(t) for t in cleaned)
        pool = int(signal)
        if late > 0:
            pool += late
            late_absorbed = late
            attribution = "weighted_incl_late"
        elif signal > 0:
            attribution = "weighted_split"
        _allocate_pool(cleaned, max(0, pool), attribution)
    elif late > 0:
        # No tools: fold late into step (never a residual UI node)
        late_absorbed = late

    harness_pool = sum(int(t.get("context_delta") or 0) for t in cleaned)

    # Serial chain display after final deltas (each tool only its own slice)
    cursor = step.get("tools_phase_start")
    if cursor is None:
        cursor = s0
    for t in cleaned:
        d = int(t.get("context_delta") or 0)
        if isinstance(cursor, int):
            t["context_before"] = cursor
            t["context_after"] = cursor + d
            cursor = t["context_after"]
        t["kind"] = "tool"

    step["tools"] = cleaned
    step["harness_pool_tokens"] = harness_pool
    step["harness_attribution"] = attribution
    step["harness_ambiguous"] = ambiguous
    step["late_absorbed"] = late_absorbed
    # Never surface late residual in UI — always 0 after redistribute
    step["late_context_delta_display"] = 0

    # Tokenizer weights for Thought / Message (full buffer when available)
    th_text = step.get("thought_summary_text") or ""
    if isinstance(th_text, str) and th_text:
        step["thought_summary_tokens"] = count_tokens(th_text)
        step["thought_summary_chars"] = len(th_text)
        step["thought_chars"] = len(th_text)
    elif not step.get("thought_summary_tokens"):
        step["thought_summary_tokens"] = count_chars_as_tokens(
            int(step.get("thought_summary_chars") or step.get("thought_chars") or 0)
        )
    msg_text = step.get("message_text") or ""
    if isinstance(msg_text, str) and msg_text:
        step["message_tokens"] = count_tokens(msg_text)
        step["message_chars"] = len(msg_text)
    elif not step.get("message_tokens"):
        step["message_tokens"] = count_chars_as_tokens(
            int(step.get("message_chars") or 0)
        )

    # Composition of context growth within this call (provisional tokZ).
    # Pricing overwrites with billed pure-Out / reason / harness shares.
    thought_tok_est = max(0, int(step.get("thought_summary_tokens") or 0))
    message_tok_est = max(0, int(step.get("message_tokens") or 0))
    harness_results = sum(int(t.get("context_delta") or 0) for t in cleaned)
    step["composition"] = {
        "thought_out": thought_tok_est,
        "model_emit": emit_d,
        "message_out": message_tok_est,
        "harness_results": harness_results,
        "late_residual": 0,
        "total": (
            thought_tok_est
            + emit_d
            + message_tok_est
            + harness_results
        ),
    }

    # --- Phase tree: LLM then harness ---
    # UI order (fixed): Thought → Reasoning[enc] → Message → Tool request[id]…
    # Thought/Message/ToolReq = exact TokZ; Enc = residual of full off_out.
    llm_children: list[dict[str, Any]] = []
    enc_c = int(step.get("thought_encrypted_chars") or 0)
    sum_c = int(
        step.get("thought_summary_chars") or step.get("thought_chars") or 0
    )
    sum_tok = int(step.get("thought_summary_tokens") or thought_tok_est or 0)

    # 1) Thought
    if sum_c > 0 or step.get("thought_chunks") or step.get("thought_preview") or sum_tok:
        llm_children.append(
            {
                "kind": "thought",
                "label": "Thought",
                "chunks": step.get("thought_chunks") or 0,
                "chars": sum_c,
                "summary_chars": sum_c,
                "summary_tokens": sum_tok,
                "tokenizer_tokens": sum_tok,
                "preview": step.get("thought_preview"),
                "context_delta": 0,
                "estimate_note": (
                    "summary_text — tokenizer definitive; inside reasoningTokens"
                ),
            }
        )

    # 2) Reasoning [encrypted] — residual filled by pricing; show when we
    # have enc chars or a thought/tools reason activity.
    # Always stamp encrypted TokZ (never leave only chars → UI chars//4 lies).
    enc_tok = int(step.get("thought_encrypted_tokens") or 0)
    if enc_tok <= 0 and enc_c > 0:
        enc_tok = count_chars_as_tokens(enc_c)
    if enc_c > 0 or enc_tok > 0 or sum_tok > 0 or emit_d > 0 or cleaned:
        llm_children.append(
            {
                "kind": "reasoning",
                "label": "Reasoning",
                "chars": enc_c,
                "encrypted_chars": enc_c,
                "encrypted_tokens": int(enc_tok),
                "tokenizer_tokens": int(enc_tok),
                "context_delta": 0,
                "estimate_note": (
                    "encrypted_content TokZ (tokenizer); residual Out by pricing"
                ),
            }
        )

    # 3) Message (pure Out)
    if step.get("message_chunks") or message_tok_est:
        llm_children.append(
            {
                "kind": "message",
                "chunks": step.get("message_chunks") or [],
                "chars": step.get("message_chars") or 0,
                "message_tokens": int(step.get("message_tokens") or 0),
                "tokenizer_tokens": int(step.get("message_tokens") or 0),
                "preview": step.get("message_preview"),
                "context_delta": 0,
                "estimate_note": "assistant.content — pure Out pro-rata",
            }
        )

    # 4) Tool request [id] — one line per tool (RawInput tokenizer definitive)
    for t in cleaned:
        arg_tok = int(t.get("arg_tokens_est") or 0)
        arg_ch = int(t.get("arg_chars") or 0)
        if arg_tok <= 0 and arg_ch <= 0 and not t.get("name"):
            continue
        plan = t.get("plan") if isinstance(t.get("plan"), dict) else None
        is_plan = bool(t.get("is_plan") or plan)
        llm_children.append(
            {
                "kind": "tool_request",
                "label": "plan request" if is_plan else "tool request",
                "name": t.get("name"),
                "title": t.get("title"),
                "tool_call_id": t.get("tool_call_id"),
                "tool_seq": t.get("tool_seq"),
                "path": t.get("path"),
                "offset": t.get("offset"),
                "limit": t.get("limit"),
                "arg_chars": arg_ch,
                "arg_tokens_est": arg_tok,
                "tokenizer_tokens": arg_tok,
                "chars": arg_ch,
                "context_delta": 0,
                "plan": plan,
                "is_plan": is_plan,
                "estimate_note": (
                    "plan request RawInput — tokenizer definitive; "
                    "inside reasoningTokens"
                    if is_plan
                    else (
                        "tool request RawInput — tokenizer definitive; "
                        "inside reasoningTokens"
                    )
                ),
            }
        )

    # Out→In provisional (pricing overwrites with full billed Out TokF):
    # Thought + Reasoning + Message + ToolReq — all TokF after price.
    # Pre-price: thought+toolreq+message TokZ (enc residual not yet known).
    llm_to_ctx = thought_tok_est + emit_d + message_tok_est
    step["llm_to_ctx_tokens"] = llm_to_ctx

    # Tool name summary for LLM Out → In line (read_file x2 · grep x3)
    name_counts: dict[str, int] = {}
    for t in cleaned:
        nm = str(t.get("name") or "tool").strip() or "tool"
        name_counts[nm] = int(name_counts.get(nm) or 0) + 1
    tool_summary_parts: list[str] = []
    for nm, cnt in name_counts.items():
        tool_summary_parts.append(f"{nm} x{cnt}" if cnt > 1 else nm)
    tool_summary = " · ".join(tool_summary_parts)

    harness_children: list[dict[str, Any]] = []
    # First line: LLM Out [N] of this call → next-call In (provisional tok; pricing overwrites)
    call_idx = step.get("index")
    if call_idx is None:
        call_idx = step.get("step_index")
    try:
        call_n = int(call_idx) if call_idx is not None else 0
    except (TypeError, ValueError):
        call_n = 0
    if llm_to_ctx > 0 or cleaned:
        harness_children.append(
            {
                "kind": "llm_to_in",
                "label": f"LLM Out [{call_n}]",
                "call_index": call_n,
                "tool_summary": tool_summary,
                "tool_names": list(name_counts.keys()),
                "tool_name_counts": dict(name_counts),
                # Provisional: tokenizer-side Out composition; pricing sets billed Out
                "tokens_in": int(llm_to_ctx),
                "context_delta": int(llm_to_ctx),
                "tokens_out_source": int(llm_to_ctx),
                "tokenizer_tokens": int(llm_to_ctx),
                "estimate_note": (
                    "LLM Out of this call re-enters next prompt as uncached In "
                    "(with tool results). Separated so tools are not scaled to absorb Out."
                ),
            }
        )
    harness_children.extend(cleaned)
    # late residual is always redistributed into tools — never a harness child
    # Hooks that ran during this model step (track everywhere).
    # Mid-call hooks can feed the next LLM; stop/end hooks go to the user
    # and must not inflate harness_pool / next-call In / Cached.
    hook_tokens = 0
    hook_tokens_to_llm = 0
    for h in step.get("hooks") or []:
        if not isinstance(h, dict):
            continue
        h_chars = int(h.get("chars") or 0)
        h_tok = int(
            h.get("tokens_est") or max(0, count_chars_as_tokens(h_chars))
        )
        ev = str(h.get("event_name") or "hook").lower()
        to_user = ev in ("stop", "session_stop", "agent_stop")
        hook_tokens += h_tok
        if not to_user:
            hook_tokens_to_llm += h_tok
        harness_children.append(
            {
                "kind": "hook",
                "event_name": h.get("event_name") or "hook",
                "run_names": h.get("run_names") or [],
                "chars": h_chars,
                # tokens kept for display; pricing zeros In if to_user / last call
                "tokens_in": h_tok,
                "context_delta": 0 if to_user else h_tok,
                "to_user": to_user,
                "elapsed_ms": h.get("elapsed_ms"),
                "estimate_note": (
                    "hook → user (not returned to LLM)"
                    if to_user
                    else "hook_execution payload (JSON chars/4) → may feed next LLM"
                ),
            }
        )
    if hook_tokens > 0:
        # Only hooks that can feed the next model call enlarge harness pool
        harness_pool = int(harness_pool) + hook_tokens_to_llm
        step["harness_pool_tokens"] = harness_pool
        step["hook_tokens"] = hook_tokens
        step["hook_tokens_to_llm"] = hook_tokens_to_llm

    # Tools/hooks/late only in stream harness pool; LLM Out tracked separately
    # (re-enters as In but is not stream tool growth).
    harness_delta = sum(
        int(c.get("context_delta") or 0)
        for c in harness_children
        if c.get("kind") != "llm_to_in"
    )
    harness_in_with_out = harness_delta + int(llm_to_ctx)
    children: list[dict[str, Any]] = []
    if llm_children:
        children.append(
            {
                "kind": "phase_llm",
                "label": "LLM",
                # Out phase; growth→next In mirrored under harness as llm_to_in
                "context_delta": emit_d,
                "children": llm_children,
            }
        )
    if harness_children:
        children.append(
            {
                "kind": "phase_harness",
                "label": "Harness",
                "context_delta": harness_in_with_out,
                "ambiguous": ambiguous,
                "attribution": attribution,
                "tool_count": len(cleaned),
                "result_chars_total": sum(
                    int(t.get("result_chars") or 0) for t in cleaned
                ),
                "result_tokens_est_total": content_sum,
                "tt_observed_total": tt_tools_sum,
                "late_absorbed": late_absorbed,
                "llm_out_in_tokens": int(llm_to_ctx),
                "children": harness_children,
            }
        )

    step["children"] = children
    # Window parts: Out composition + tool/hook stream growth (Out not double-counted)
    parts = llm_to_ctx + harness_delta
    step["context_parts_sum"] = parts
    if isinstance(s0, int) and isinstance(s1, int):
        step["context_unaccounted"] = (s1 - s0) - parts

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

    # Cold R1: never treat session-level _cache_baseline as a warm prior when
    # this is the first round (System card / no completed predecessor).
    is_session_first = (
        r.get("cache_baseline_at_start") is None
        and (
            (r in hb.rounds and hb.rounds.index(r) == 0)
            or (r not in hb.rounds and len(hb.rounds) == 0)
        )
    )
    if is_session_first or r.get("system_prompt") is not None:
        # system_prompt is only built for the first cold prompt
        if is_session_first:
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
        peel_round_usage,
    )

    if usage:
        child_ids = child_ids_to_peel(r)
        if child_ids:
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
            attach_subagents_after_steps(r, peel_meta)

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
        recon = reconstruct_model_step_usage(
            steps,
            official_usage=usage,
            prior_context_tokens=prior if isinstance(prior, int) else None,
            context_reread=bool(reread),
            reread_uncached_tokens=int(reread["reread_tokens"]) if reread else 0,
            user_uncached_tokens=int(user_unc),
            system_uncached_tokens=int(sys_unc),
            context_end_tokens=end_tok_i,
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

        # Inject System "Message" residual (stream under-count vs official input)
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

