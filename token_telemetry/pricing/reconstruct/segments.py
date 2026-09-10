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
        from token_telemetry.pricing.reconstruct.orchestrate import (
            reconstruct_model_step_usage as _rms,
        )
        rec = _rms(
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
