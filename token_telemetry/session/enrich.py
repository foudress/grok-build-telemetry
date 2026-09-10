"""Moved from session/monitor.py — same bodies."""
from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional

from token_telemetry.pricing import (
    estimate_cost_usd,
    estimate_from_usage,
    pick_tier,
    pricing_model_scope,
    pricing_payload,
    ticks_to_usd,
)
from token_telemetry.hierarchy import HierarchyBuilder, MAX_ROUNDS_RETAINED
from token_telemetry.session.discover import (
    SESSIONS_ROOT,
    list_sessions_for_ui,
    read_active_session_ids,
    resolve_session_dir,
)
from token_telemetry.session.subagents import (
    attach_subagents_after_steps,
    capture_child_sys,
    child_ids_from_session_update,
    collect_child_ids_from_round,
    collect_resume_alias_from_round,
    ensure_lineage_subagent_cards,
    is_subagent_session,
    latest_in_resume_chain,
    read_session_summary,
    root_subagent_id,
    sibling_session_dir,
)
from token_telemetry.hierarchy.gen_rate import mean_gen_rate

def _mean_round_tps(rounds: list) -> Optional[float]:
    return mean_gen_rate(rounds)

def _enrich_user_prompt(up: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Price the user-prompt continuity row (prior cache + new uncached)."""
    if not up or not isinstance(up, dict):
        return up
    out = dict(up)
    # Already priced by hierarchy bootstrap split (first round)
    if out.get("cost_in_usd") is not None and out.get("tokens_in") is not None:
        if out.get("estimate_usd") is None:
            out["estimate_usd"] = float(out.get("cost_in_usd") or 0) + float(
                out.get("cost_cached_usd") or 0
            )
        return out
    cached = out.get("cached_est")
    uncached = out.get("uncached_est")
    try:
        c = int(cached) if cached is not None else 0
        u = int(uncached) if uncached is not None else 0
    except (TypeError, ValueError):
        return out
    total = c + u
    if total <= 0 and c <= 0 and u <= 0:
        out["estimate_usd"] = 0.0
        out["cost_in_usd"] = 0.0
        out["cost_cached_usd"] = 0.0
        out["tokens_in"] = 0
        out["tokens_cached"] = 0
        return out
    est = estimate_cost_usd(
        input_tokens=total,
        output_tokens=0,
        cached_read_tokens=c,
        peak_context_tokens=total or c or u,
        model_calls=1,
    )
    out["input_est"] = total
    out["tokens_in"] = u
    out["tokens_cached"] = c
    out["cost_in_usd"] = float(est["cost_usd"]["uncached_input"])
    out["cost_cached_usd"] = float(est["cost_usd"]["cached_input"])
    out["estimate_usd"] = float(est["cost_usd"]["total"])
    out["estimate_breakdown"] = est["cost_usd"]
    out["tier"] = est["tier"]
    return out

def _enrich_system_prompt(sp: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Pass-through / light price for first-round system bootstrap card."""
    if not sp or not isinstance(sp, dict):
        return sp
    out = dict(sp)
    # Keep parts list (breakdown)
    if isinstance(out.get("parts"), list):
        out["parts"] = [dict(p) for p in out["parts"] if isinstance(p, dict)]
    if out.get("cost_in_usd") is not None:
        return out
    u = int(out.get("tokens_in") or out.get("uncached_est") or out.get("logical_tokens") or 0)
    if u <= 0:
        out["estimate_usd"] = 0.0
        out["cost_in_usd"] = 0.0
        out["tokens_in"] = 0
        return out
    est = estimate_cost_usd(
        input_tokens=u,
        output_tokens=0,
        cached_read_tokens=0,
        peak_context_tokens=u,
        model_calls=1,
    )
    out["tokens_in"] = u
    out["tokens_cached"] = 0
    out["cost_in_usd"] = float(est["cost_usd"]["uncached_input"])
    out["cost_cached_usd"] = 0.0
    out["estimate_usd"] = float(est["cost_usd"]["total"])
    return out

def _round_api_bill(rr: dict) -> float:
    """Full list-rate bill for one turn (R1 includes System)."""
    if not isinstance(rr, dict):
        return 0.0
    bd = rr.get("breakdown") if isinstance(rr.get("breakdown"), dict) else {}
    api_tot = bd.get("api_total_usd")
    if api_tot is None:
        su = rr.get("step_usage") if isinstance(rr.get("step_usage"), dict) else {}
        tot = su.get("totals") if isinstance(su.get("totals"), dict) else {}
        api_tot = tot.get("api_cost_usd")
    if api_tot is not None:
        try:
            return float(api_tot)
        except (TypeError, ValueError):
            return 0.0
    try:
        return float(rr.get("estimate_usd") or 0)
    except (TypeError, ValueError):
        return 0.0

def _sum_rounds_estimate(rounds: list) -> float:
    """Σ per-turn list-rate bills + fork recaps (same as parent session KPI)."""
    est = 0.0
    for rr in rounds or []:
        if not isinstance(rr, dict):
            continue
        est += _round_api_bill(rr)
        for rec in rr.get("recaps_after") or []:
            if isinstance(rec, dict) and rec.get("cost_usd") is not None:
                try:
                    est += float(rec["cost_usd"])
                except (TypeError, ValueError):
                    pass
    return est

def _sum_child_tab_estimates(sub_sessions: list) -> float:
    """One child session $ per sub-agent tab (the number shown on that tab)."""
    tot = 0.0
    for ss in sub_sessions or []:
        if not isinstance(ss, dict):
            continue
        try:
            tot += float(ss.get("estimate_usd") or 0)
        except (TypeError, ValueError):
            pass
    return tot

def enrich_round_usage(self, round_: dict[str, Any]) -> dict[str, Any]:
        """Attach official usage + list-rate estimate + per-step reconstruction."""
        from token_telemetry.pricing import reconstruct_model_step_usage  # local; always available

        usage = round_.get("usage_raw") or {}
        peak = round_.get("context_peak")
        end = round_.get("context_end")
        if isinstance(peak, int) and isinstance(end, int) and end > 0 and peak > 2 * end:
            peak = None
        steps = list(round_.get("model_steps") or [])
        # Ensure per-step estimates exist (open rounds / older builders)
        step_usage = round_.get("step_usage")
        if steps and (
            not step_usage
            or not (steps[0].get("estimate") if steps else None)
        ):
            prior = round_.get("cache_baseline_at_start")
            if not isinstance(prior, int):
                prior = (step_usage or {}).get("prior_context_tokens")
            up0 = round_.get("user_prompt") if isinstance(round_.get("user_prompt"), dict) else {}
            user_unc = 0
            try:
                user_unc = int(up0.get("tokens_in") or up0.get("uncached_est") or 0)
            except (TypeError, ValueError):
                user_unc = 0
            sys0 = round_.get("system_prompt") if isinstance(round_.get("system_prompt"), dict) else {}
            try:
                sys_unc = int(sys0.get("tokens_in") or sys0.get("logical_tokens") or 0)
            except (TypeError, ValueError):
                sys_unc = 0
            end_tok = round_.get("context_end")
            try:
                end_tok_i = int(end_tok) if end_tok is not None else None
            except (TypeError, ValueError):
                end_tok_i = None
            compact_reentry = None
            cb = round_.get("compact_before")
            if isinstance(cb, dict) and cb.get("kind") == "compaction":
                compact_reentry = dict(cb)
                if compact_reentry.get("placement") != "mid_round":
                    compact_reentry["_attribution"] = "user"
                    if not compact_reentry.get("placement"):
                        compact_reentry["placement"] = "between_rounds"
            fam = (
                round_.get("model_family")
                or getattr(self.hierarchy, "_pricing_model", None)
            )
            with pricing_model_scope(fam):
                recon = reconstruct_model_step_usage(
                    steps,
                    official_usage=usage if usage else None,
                    prior_context_tokens=prior if isinstance(prior, int) else None,
                    user_uncached_tokens=int(user_unc),
                    system_uncached_tokens=int(sys_unc),
                    context_end_tokens=end_tok_i,
                    compact_reentry=compact_reentry,
                )
            steps = recon["steps"]
            step_usage = {
                "method": recon["method"],
                "calibrated": recon["calibrated"],
                "totals": recon["totals"],
                "breakdown": recon.get("breakdown") or {},
                "note": recon.get("note"),
                "prior_context_tokens": recon.get("prior_context_tokens"),
            }

        fam = (
            round_.get("model_family")
            or getattr(self.hierarchy, "_pricing_model", None)
        )
        with pricing_model_scope(fam):
            user_prompt = _enrich_user_prompt(round_.get("user_prompt"))
            system_prompt = _enrich_system_prompt(round_.get("system_prompt"))
        # Prefer hierarchy merge (includes session-restart user In / tree_in)
        breakdown = dict(
            round_.get("breakdown")
            or (step_usage or {}).get("breakdown")
            or {}
        )
        # Keep hierarchy miss / cache-share fields if step_usage overwrote
        for k in (
            "cache_miss_in_tokens",
            "cache_miss_in_usd",
            "reread_in_tokens",
            "reread_in_usd",
            "reread_tokens",
            "context_reread",
            "user_cache_share_tokens",
        ):
            if round_.get("breakdown") and round_["breakdown"].get(k) is not None:
                breakdown[k] = round_["breakdown"][k]
        if system_prompt:
            breakdown["system_in_tokens"] = int(system_prompt.get("tokens_in") or 0)
            breakdown["system_in_usd"] = float(system_prompt.get("cost_in_usd") or 0)
        if user_prompt:
            breakdown["user_in_tokens"] = int(user_prompt.get("tokens_in") or 0)
            breakdown["user_in_usd"] = float(user_prompt.get("cost_in_usd") or 0)
            breakdown["user_cached_tokens"] = int(user_prompt.get("tokens_cached") or 0)
            breakdown["user_cached_usd"] = float(user_prompt.get("cost_cached_usd") or 0)
            # Round In = User In (Compact Out folded in) + Σ call In + cache_miss.
            # Prefer hierarchy merge (already clamped to official uncached).
            user_tok = int(user_prompt.get("tokens_in") or 0)
            user_usd = float(user_prompt.get("cost_in_usd") or 0)
            co = (
                user_prompt.get("compact_out")
                if isinstance(user_prompt.get("compact_out"), dict)
                else {}
            )
            try:
                co_tok = int(co.get("tokens_in") or 0)
            except (TypeError, ValueError):
                co_tok = 0
            try:
                co_usd = float(co.get("cost_in_usd") or 0)
            except (TypeError, ValueError):
                co_usd = 0.0
            extra_co = 0 if user_prompt.get("compact_out_in_user_in") else int(co_tok)
            extra_co_usd = 0.0 if user_prompt.get("compact_out_in_user_in") else float(co_usd)
            hier_bd = (
                round_.get("breakdown")
                if isinstance(round_.get("breakdown"), dict)
                else {}
            )
            hier_tree = hier_bd.get("tree_in_tokens")
            try:
                miss_tok = int(breakdown.get("cache_miss_in_tokens") or 0)
            except (TypeError, ValueError):
                miss_tok = 0
            try:
                miss_usd = float(breakdown.get("cache_miss_in_usd") or 0)
            except (TypeError, ValueError):
                miss_usd = 0.0
            breakdown["cache_miss_in_tokens"] = int(miss_tok)
            breakdown["cache_miss_in_usd"] = float(miss_usd)
            if hier_tree is not None:
                # Ledger already merged — do not re-sum user+harness+miss.
                breakdown["tree_in_tokens"] = int(hier_tree)
                if hier_bd.get("tree_in_usd") is not None:
                    breakdown["tree_in_usd"] = float(hier_bd.get("tree_in_usd") or 0)
                if hier_bd.get("harness_in_tokens") is not None:
                    breakdown["harness_in_tokens"] = int(hier_bd.get("harness_in_tokens") or 0)
                    breakdown["harness_in_usd"] = float(hier_bd.get("harness_in_usd") or 0)
            else:
                if hier_bd.get("harness_in_tokens") is not None:
                    harness_tok = int(hier_bd.get("harness_in_tokens") or 0)
                    harness_usd = float(hier_bd.get("harness_in_usd") or 0)
                else:
                    sum_call = 0
                    sum_call_usd = 0.0
                    for s in steps or []:
                        if not isinstance(s, dict):
                            continue
                        sum_call += int(s.get("tokens_in") or s.get("harness_in_tokens") or 0)
                        sum_call_usd += float(
                            s.get("cost_in_usd") or s.get("harness_in_usd") or 0
                        )
                    harness_tok = (
                        int(sum_call)
                        if sum_call > 0
                        else int(breakdown.get("harness_in_tokens") or 0)
                    )
                    harness_usd = (
                        float(sum_call_usd)
                        if sum_call > 0
                        else float(breakdown.get("harness_in_usd") or 0)
                    )
                breakdown["harness_in_tokens"] = harness_tok
                breakdown["harness_in_usd"] = harness_usd
                breakdown["tree_in_tokens"] = (
                    int(user_tok) + int(extra_co) + int(harness_tok) + int(miss_tok)
                )
                breakdown["tree_in_usd"] = (
                    float(user_usd)
                    + float(extra_co_usd)
                    + float(harness_usd)
                    + float(miss_usd)
                )
            # Wave D owns tree.js; alias so the live miss chip is not blank.
            breakdown["reread_in_tokens"] = int(miss_tok)
            breakdown["reread_in_usd"] = float(miss_usd)

        su_totals = (step_usage or {}).get("totals") or {}
        base = {
            "index": round_.get("index"),
            "prompt_id": round_.get("prompt_id"),
            "user_preview": round_.get("user_preview"),
            "user_chars": round_.get("user_chars"),
            "context_start": round_.get("context_start"),
            "context_end": round_.get("context_end"),
            "context_peak": peak,
            "context_delta": round_.get("context_delta"),
            "compact_delta": round_.get("compact_delta"),
            "context_after_compact": round_.get("context_after_compact"),
            "cache_baseline_at_start": round_.get("cache_baseline_at_start"),
            "idle_gap_ms": round_.get("idle_gap_ms"),
            "started_ms": round_.get("started_ms"),
            "completed_ms": round_.get("completed_ms"),
            "turn_start_ms": round_.get("turn_start_ms"),
            "gen_ms": round_.get("gen_ms"),
            "gen_out_tokens": round_.get("gen_out_tokens"),
            "gen_tokens_per_sec": round_.get("gen_tokens_per_sec"),
            "gen_rate_n": round_.get("gen_rate_n"),
            "session_restart": bool(
                round_.get("session_restart")
                or (user_prompt or {}).get("session_restart")
            ),
            "cache_miss": bool(
                round_.get("cache_miss") or (user_prompt or {}).get("cache_miss")
            ),
            "context_reread": round_.get("context_reread"),
            "cache_miss_in_tokens": breakdown.get("cache_miss_in_tokens"),
            "cache_miss_in_usd": breakdown.get("cache_miss_in_usd"),
            "reread_in_tokens": (
                breakdown.get("cache_miss_in_tokens")
                or breakdown.get("reread_in_tokens")
                or round_.get("reread_in_tokens")
                or (user_prompt or {}).get("reread_tokens")
            ),
            "reread_in_usd": (
                breakdown.get("cache_miss_in_usd")
                or breakdown.get("reread_in_usd")
                or round_.get("reread_in_usd")
                or (user_prompt or {}).get("reread_in_usd")
            ),
            "completed": round_.get("completed"),
            "model_step_count": round_.get("model_step_count") or len(steps),
            "model_steps": steps,
            "step_usage": step_usage,
            "breakdown": breakdown,
            "cost_in_usd": su_totals.get("cost_in_usd"),
            "cost_cached_usd": su_totals.get("cost_cached_usd"),
            "cost_out_usd": su_totals.get("cost_out_usd"),
            "notes": round_.get("notes") or [],
            "compactions": round_.get("compactions") or [
                n for n in (round_.get("notes") or []) if isinstance(n, dict) and n.get("kind") == "compaction"
            ],
            "compact_before": round_.get("compact_before"),
            "compact_after": round_.get("compact_after"),
            "recap_after": round_.get("recap_after"),
            "recaps_after": round_.get("recaps_after") or [
                n for n in (round_.get("notes") or [])
                if isinstance(n, dict) and n.get("kind") == "session_recap"
            ],
            "recaps_before": round_.get("recaps_before") or [],
            "user_prompt": user_prompt,
            "system_prompt": system_prompt,
            "stop_reason": round_.get("stop_reason"),
            "model_id": round_.get("model_id"),
            "model_family": round_.get("model_family")
            or getattr(self.hierarchy, "_pricing_model", None),
        }

        if not usage:
            # Live open round: estimate from reconstructed steps only
            su = (step_usage or {}).get("totals") or {}
            return {
                **base,
                "usage": None,
                "estimate": None,
                "estimate_usd": su.get("cost_usd"),
                "input_tokens": su.get("input"),
                "output_tokens": su.get("output"),
                "cached_read_tokens": su.get("cached_read"),
                "uncached_input_tokens": su.get("uncached_input"),
                "official_usd": None,
                "tier": None,
            }

        est = estimate_from_usage(
            usage,
            peak_context_tokens=peak,
            model=round_.get("model_family")
            or getattr(self.hierarchy, "_pricing_model", None),
        )
        ticks = usage.get("costUsdTicks")
        official = ticks_to_usd(ticks) if ticks is not None else None
        api_ms = usage.get("apiDurationMs") or 0
        out_t = int(usage.get("outputTokens") or 0)
        reason_t = int(usage.get("reasoningTokens") or 0)
        in_t = int(usage.get("inputTokens") or 0)
        cache_t = int(usage.get("cachedReadTokens") or 0)
        calls = usage.get("modelCalls")
        sec = api_ms / 1000.0 if api_ms else 0
        uncached = max(0, in_t - min(cache_t, in_t))
        step_sum = (step_usage or {}).get("totals") or {}
        # Prefer sum of per-step list prices when steps align with modelCalls
        estimate_usd = est["cost_usd"]["total"]
        cost_in_usd = est["cost_usd"].get("uncached_input")
        cost_cached_usd = est["cost_usd"].get("cached_input")
        cost_out_usd = est["cost_usd"].get("output")
        # Prefer calibrated per-step API bill (paid In + cache + out) when present
        if step_sum.get("cost_usd") is not None and steps:
            estimate_usd = float(step_sum["cost_usd"])
            if step_sum.get("cost_in_usd") is not None:
                cost_in_usd = float(step_sum["cost_in_usd"])
            if step_sum.get("cost_cached_usd") is not None:
                cost_cached_usd = float(step_sum["cost_cached_usd"])
            if step_sum.get("cost_out_usd") is not None:
                cost_out_usd = float(step_sum["cost_out_usd"])
        # R1: hierarchy peels System from white total (tree In + Cached + Out)
        if (
            system_prompt
            and isinstance(round_.get("estimate_usd"), (int, float))
            and (breakdown.get("round_total_peeled_system") or round_.get("system_prompt"))
        ):
            estimate_usd = float(round_["estimate_usd"])
            if breakdown.get("tree_in_usd") is not None:
                # tree In is the R1 In bar; keep API unc under cost_in if needed
                pass
            if breakdown.get("total_usd") is not None:
                breakdown = dict(breakdown)
                breakdown["total_usd"] = float(estimate_usd)
        return {
            **base,
            "input_tokens": in_t,
            "output_tokens": out_t,
            "reasoning_tokens": reason_t,
            "cached_read_tokens": cache_t,
            "uncached_input_tokens": uncached,
            "total_tokens": int(usage.get("totalTokens") or 0),
            "api_duration_ms": api_ms,
            "model_calls": calls,
            "official_ticks": ticks,
            "official_usd": official if official is not None else 0.0,
            "estimate_usd": estimate_usd,
            "estimate_usd_from_usage_aggregate": est["cost_usd"]["total"],
            "estimate_breakdown": est["cost_usd"],
            "cost_in_usd": cost_in_usd,
            "cost_cached_usd": cost_cached_usd,
            "cost_out_usd": cost_out_usd,
            "tier": est["tier"],
            "context_tokens_for_tier": est["context_tokens_for_tier"],
            "tier_method": (est.get("tier_resolution") or {}).get("method"),
            "output_tokens_per_sec": round_.get("gen_tokens_per_sec")
            if round_.get("gen_tokens_per_sec") is not None
            else (round(out_t / sec, 3) if sec else None),
            "reasoning_tokens_per_sec": round(reason_t / sec, 3) if sec else None,
            "gen_tokens_per_sec": round_.get("gen_tokens_per_sec")
            if round_.get("gen_tokens_per_sec") is not None
            else (round(out_t / sec, 3) if sec else None),
            "gen_ms": round_.get("gen_ms"),
        }
