"""CALL-1 cache-miss detection: reread is a first-call event, not Σunc − growth."""

from __future__ import annotations

from token_telemetry.hierarchy.cache_miss import (
    _apply_session_restart_cache_miss,
    _detect_context_reread,
)


class _HB:
    def __init__(self) -> None:
        self.rounds: list = []


def _round(
    *,
    prior: int,
    c0_start: int,
    end: int,
    off_in: int,
    off_cache: int,
    off_unc: int | None = None,
    extra_up: dict | None = None,
) -> dict:
    if off_unc is None:
        off_unc = max(0, off_in - min(off_cache, off_in))
    up = {
        "kind": "user_prompt",
        "prior_context": prior,
        "tokens_in": prior,
        "uncached_est": 80,
        "tokens_cached": prior,
        "cached_est": prior,
    }
    if extra_up:
        up.update(extra_up)
    return {
        "completed": True,
        "usage_raw": {
            "inputTokens": off_in,
            "cachedReadTokens": off_cache,
        },
        "user_prompt": up,
        "context_end": end,
        "model_steps": [
            {
                "stream_context_start": c0_start,
                "context_start": c0_start,
            }
        ],
        "breakdown": {},
    }


def test_compact_collapse_is_not_a_miss():
    hb = _HB()
    r = _round(
        prior=192000,
        c0_start=6607,
        end=39805,
        off_in=244535,
        off_cache=203264,
    )
    assert _detect_context_reread(hb, r) is None


def test_warm_clean_is_not_a_miss():
    hb = _HB()
    r = _round(
        prior=126000,
        c0_start=126800,
        end=163000,
        off_in=38000 + 400000,
        off_cache=400000,
        off_unc=38000,
    )
    hit = _detect_context_reread(hb, r)
    assert hit is None


def test_first_call_miss_fires():
    hb = _HB()
    prior = 126446
    r = _round(
        prior=prior,
        c0_start=126824,
        end=163599,
        off_in=3802792,
        off_cache=3637888,
    )
    hit = _detect_context_reread(hb, r)
    assert hit is not None
    assert hit["kind"] == "first_call_reread"
    assert hit["off_unc"] == 164904
    assert hit["reread_tokens"] == prior


def test_classic_cache_miss():
    hb = _HB()
    r = _round(
        prior=100000,
        c0_start=100500,
        end=101000,
        off_in=91000,
        off_cache=1000,
    )
    hit = _detect_context_reread(hb, r)
    assert hit is not None
    assert hit["kind"] == "classic_cache_miss"
    assert hit["off_unc"] == 90000


def test_apply_zeros_user_cache_and_sets_reread_in():
    hb = _HB()
    r = _round(
        prior=126446,
        c0_start=126824,
        end=163599,
        off_in=3802792,
        off_cache=3637888,
    )
    _apply_session_restart_cache_miss(hb, r)
    up = r["user_prompt"]
    assert up["tokens_cached"] == 0
    assert up["cached_est"] == 0
    assert int(up["reread_in_tokens"]) > 0
    assert int(r["reread_in_tokens"]) > 0
    assert up["warning"] == "Context re-read (first-call cache miss)"
    assert up["context_reread_kind"] == "first_call_reread"
