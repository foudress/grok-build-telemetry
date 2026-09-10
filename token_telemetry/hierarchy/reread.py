"""Moved from cache_miss.py — same function bodies."""
from __future__ import annotations

from typing import Any, Optional

from token_telemetry.tokenizer import (
    count_chars_as_tokens,
    count_tokens,
    count_user_prompt_tokens,
)

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
