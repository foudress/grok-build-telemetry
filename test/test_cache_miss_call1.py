"""CALL-1 cache-miss detection: leftover off_unc is round miss, not under User."""

from __future__ import annotations

from token_telemetry.hierarchy.cache_miss import (
    _apply_session_restart_cache_miss,
    _detect_context_reread,
)
from token_telemetry.pricing.reconstruct import reconstruct_model_step_usage


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


def test_idle_then_compact_is_not_idle_reread():
    hb = _HB()
    r = _round(
        prior=150000,
        c0_start=180000,
        end=12000,
        off_in=170000,
        off_cache=1000,
    )
    r["idle_gap_ms"] = 11 * 3600 * 1000
    r["mid_round_compacts"] = True
    r["model_steps"][0]["compacts_after"] = [
        {"kind": "compaction", "tokens_after": 4475}
    ]
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


def test_apply_keeps_user_cache_not_reread_under_user():
    """Miss stays on the round; User Cached is kept (not zeroed)."""
    hb = _HB()
    r = _round(
        prior=126446,
        c0_start=126824,
        end=163599,
        off_in=3802792,
        off_cache=3637888,
    )
    r["breakdown"] = {
        "cache_miss_in_tokens": 5000,
        "cache_miss_in_usd": 0.01,
        "harness_in_tokens": 100,
        "user_in_tokens": 80,
    }
    _apply_session_restart_cache_miss(hb, r)
    up = r["user_prompt"]
    assert int(up["tokens_cached"]) == 126446
    assert int(up["cached_est"]) == 126446
    assert int(r["breakdown"]["user_cached_tokens"]) == 126446
    assert not up.get("reread_in_tokens")
    assert not r.get("reread_in_tokens")
    assert int(up.get("uncached_est") or 0) == 80
    assert int(r["breakdown"]["user_in_tokens"]) == 80
    assert r["cache_miss"] is True
    assert r["session_restart"] is True
    assert r["breakdown"]["harness_in_tokens"] == 100
    assert r["breakdown"]["cache_miss_in_tokens"] == 5000
    # Miss is leftover uncached, not User In + miss.
    assert int(r["breakdown"]["user_in_tokens"]) != 80 + 5000


def test_apply_without_reconstruct_miss_is_noop_on_user_in():
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
    # Detector may fire, but miss tokens only come from reconstruct §0.5
    assert not up.get("reread_in_tokens")
    assert int(up.get("uncached_est") or 0) == 80


def test_miss_is_off_unc_minus_user_minus_harness():
    """Warm leftover uncached = round KV miss; compact collapse is not this."""
    user, tool_z = 80, 80
    recon = reconstruct_model_step_usage(
        [
            {
                "stream_context_start": 100500,
                "context_start": 100500,
                "stream_context_end": 100580,
                "context_end": 100580,
                "children": [
                    {
                        "kind": "phase_harness",
                        "children": [
                            {
                                "kind": "tool",
                                "name": "grep",
                                "tokens_in": tool_z,
                                "tokenizer_tokens": tool_z,
                                "context_delta": tool_z,
                            }
                        ],
                    }
                ],
                "tools": [
                    {
                        "name": "grep",
                        "result_tokens_est": tool_z,
                        "ch_result_tokens": tool_z,
                    }
                ],
            }
        ],
        official_usage={
            "inputTokens": 110000,
            "cachedReadTokens": 1000,
            "outputTokens": 10,
        },
        prior_context_tokens=100000,
        user_uncached_tokens=user,
        context_end_tokens=100580,
    )
    bd = recon["breakdown"]
    off_unc = 110000 - 1000
    harness = int(bd.get("harness_in_tokens") or 0)
    miss = int(bd.get("cache_miss_in_tokens") or 0)
    assert int(bd.get("user_in_tokens") or 0) == user
    assert miss == max(0, off_unc - user - harness)
    assert miss > 0
    assert int(bd.get("user_in_tokens") or 0) != user + miss
    assert int(bd.get("tree_in_tokens") or 0) == user + harness + miss
