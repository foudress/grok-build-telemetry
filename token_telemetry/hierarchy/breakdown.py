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
    _is_first_round,
    _official_input_tokens,
    _r1_tree_in_tokens,
)

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
        if sys_p.get("tool_definitions_tokens"):
            bd["system_tool_definitions_tokens"] = int(sys_p["tool_definitions_tokens"])
    user_tok = 0
    user_usd = 0.0
    # Between-rounds Compact Out lives under User In (not Call/Harness, not
    # Cache Miss). Fold it into user_in once so tree In does not add it again.
    co_tok = 0
    co_usd = 0.0
    user_includes_co = False
    if isinstance(up, dict):
        user_tok = int(up.get("tokens_in") or up.get("uncached_est") or 0)
        user_usd = float(up.get("cost_in_usd") or 0)
        bd["user_in_tokens"] = user_tok
        bd["user_in_usd"] = user_usd
        bd["user_cached_tokens"] = int(
            up.get("tokens_cached") or up.get("cached_est") or 0
        )
        bd["user_cached_usd"] = float(up.get("cost_cached_usd") or 0)
        co = up.get("compact_out") if isinstance(up.get("compact_out"), dict) else {}
        try:
            co_tok = int(co.get("tokens_in") or 0)
        except (TypeError, ValueError):
            co_tok = 0
        try:
            co_usd = float(co.get("cost_in_usd") or 0)
        except (TypeError, ValueError):
            co_usd = 0.0
        if co_tok > 0 or co_usd > 0:
            bd["user_compact_out_tokens"] = int(co_tok)
            bd["user_compact_out_usd"] = float(co_usd)
        user_includes_co = bool(up.get("compact_out_in_user_in")) and co_tok > 0
        if co_tok > 0 and not user_includes_co:
            user_tok = int(user_tok) + int(co_tok)
            user_usd = float(user_usd) + float(co_usd)
            up["tokens_in"] = int(user_tok)
            up["uncached_est"] = int(user_tok)
            up["cost_in_usd"] = float(user_usd)
            up["compact_out_in_user_in"] = True
            up["display_in_tokens"] = int(user_tok)
            up["display_in_usd"] = float(user_usd)
            bd["user_in_tokens"] = int(user_tok)
            bd["user_in_usd"] = float(user_usd)
            user_includes_co = True
    # Round In = User In (prompt + prev LLM answer + Compact Out) +
    # Σ LLM call In + cache_miss. Prefer sum of model_steps tokens_in
    # (authoritative call tree) over breakdown harness so User+Calls = Round In.
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
    try:
        miss_tok = int(bd.get("cache_miss_in_tokens") or 0)
    except (TypeError, ValueError):
        miss_tok = 0
    try:
        miss_usd = float(bd.get("cache_miss_in_usd") or 0)
    except (TypeError, ValueError):
        miss_usd = 0.0
    cold = bool(bd.get("cold_round")) or (
        isinstance(sys_p, dict) and sys_p.get("kind") == "system_prompt"
    )
    sys_tok = 0
    if cold and isinstance(sys_p, dict):
        try:
            sys_tok = int(sys_p.get("tokens_in") or 0)
        except (TypeError, ValueError):
            sys_tok = 0
    # Card In = official uncached. R1 peels System onto its own card, so
    # tree In should be paid − System. Miss = leftover billed uncached after
    # User In (prompt + prev answer + Compact Out) and harness — not ToolDef
    # (already on System). Compact Out is in user_in; do not add it again.
    try:
        paid_unc = int(bd.get("uncached_in_tokens") or bd.get("paid_uncached_tokens") or 0)
    except (TypeError, ValueError):
        paid_unc = 0
    if paid_unc <= 0:
        off_in = _official_input_tokens(r)
        usage = r.get("usage_raw") if isinstance(r.get("usage_raw"), dict) else {}
        try:
            off_cache = int(usage.get("cachedReadTokens") or usage.get("cached_read_tokens") or 0)
        except (TypeError, ValueError):
            off_cache = 0
        if isinstance(off_in, int) and off_in > 0:
            paid_unc = max(0, int(off_in) - min(int(off_cache), int(off_in)))
            bd["uncached_in_tokens"] = int(paid_unc)
            bd["paid_uncached_tokens"] = int(paid_unc)
    if paid_unc > 0:
        cap = max(0, int(paid_unc) - (int(sys_tok) if cold else 0))
        caused = int(user_tok) + int(harness_tok)
        if caused < cap:
            miss_tok = int(cap) - int(caused)
            try:
                from token_telemetry.pricing import _price_in

                peak = r.get("context_end") or r.get("context_peak") or cap or 1
                miss_usd = (
                    float(_price_in(int(miss_tok), max(1, int(peak))))
                    if miss_tok
                    else 0.0
                )
            except Exception:
                miss_usd = 0.0
            bd["cache_miss_in_tokens"] = int(miss_tok)
            bd["cache_miss_in_usd"] = float(miss_usd)
        else:
            overflow = int(caused) - int(cap)
            miss_tok = 0
            miss_usd = 0.0
            bd["cache_miss_in_tokens"] = 0
            bd["cache_miss_in_usd"] = 0.0
            if overflow > 0:
                take = min(overflow, int(harness_tok))
                if take:
                    if harness_tok > 0 and harness_usd:
                        harness_usd *= (harness_tok - take) / harness_tok
                    harness_tok -= take
                    overflow -= take
                # Never eat Compact Out from User In; shrink prompt/continuity only.
                prompt_room = max(
                    0,
                    int(user_tok) - (int(co_tok) if user_includes_co else 0),
                )
                take = min(overflow, int(prompt_room))
                if take:
                    prompt_usd_part = (
                        float(user_usd) - float(co_usd)
                        if user_includes_co
                        else float(user_usd)
                    )
                    if prompt_room > 0 and prompt_usd_part:
                        prompt_usd_part *= (prompt_room - take) / prompt_room
                    user_tok -= take
                    user_usd = float(prompt_usd_part) + (
                        float(co_usd) if user_includes_co else 0.0
                    )
                    if isinstance(up, dict):
                        up["tokens_in"] = int(user_tok)
                        up["uncached_est"] = int(user_tok)
                        up["cost_in_usd"] = float(user_usd)
                        up["display_in_tokens"] = int(user_tok)
                        up["display_in_usd"] = float(user_usd)
                    bd["user_in_tokens"] = int(user_tok)
                    bd["user_in_usd"] = float(user_usd)
                bd["harness_in_tokens"] = int(harness_tok)
                bd["harness_in_usd"] = float(harness_usd)
                bd["tree_in_clamped_to_paid"] = True
    bd["tree_in_tokens"] = int(user_tok) + harness_tok + int(miss_tok)
    bd["tree_in_usd"] = float(user_usd) + harness_usd + float(miss_usd)
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
            full = str(r.get("user_text") or up.get("user_text") or "")
            src = full or str(up.get("preview") or r.get("user_preview") or "")
            if src:
                try:
                    tz = int(count_user_prompt_tokens(src))
                except Exception:
                    tz = max(1, count_chars_as_tokens(len(src)) or 1)
        if tz > 0:
            up["tokenizer_tokens"] = int(tz)
            up["prompt_tokenizer_tokens"] = int(tz)
    r["user_prompt"] = up
