"""Call context stays on the harness stream window (not reconstructed Input)."""

from __future__ import annotations

from token_telemetry.hierarchy.recap_compact import _fill_compact_cost
from token_telemetry.hierarchy.finalize import (
    _anchor_call_context_to_input,
    _apply_caused_context_display,
    _estimate_tooldef_message_bucket,
    _merge_bootstrap_into_breakdown,
)


def test_anchor_keeps_stream_window_not_api_input():
    r = {
        "context_end": 159101,
        "model_steps": [
            {
                "index": 1,
                "context_start": 20000,
                "context_end": 30000,
                "tokens_cached": 18000,
                "paid_at_start_tokens": 2000,
                "estimate": {"input_tokens": 22000, "cached_read_tokens": 18000},
            },
            {
                "index": 2,
                "context_start": 159101,
                "context_end": 159101,
                "tokens_cached": 0,
                "paid_at_start_tokens": 1800,
                "estimate": {"input_tokens": 1800, "cached_read_tokens": 0},
            },
        ],
    }
    _anchor_call_context_to_input(r)
    first, last = r["model_steps"]
    assert first["context_start"] == 20000
    assert first["api_input_tokens"] == 22000
    assert first["stream_context_start"] == 20000
    assert last["context_start"] == 159101
    assert last["stream_context_start"] == 159101
    assert last["api_input_tokens"] == 1800


def test_anchor_display_uses_stream_context_raw():
    r = {
        "model_steps": [
            {
                "context_start": 15350,
                "context_end": 18000,
                "stream_context_raw": 15350,
                "stream_context_start": 15350 + 8000,
                "stream_context_end": 18000,
                "estimate": {"input_tokens": 24000},
            }
        ],
        "context_start": 11980,
        "context_end": 22516,
    }
    _anchor_call_context_to_input(r)
    s = r["model_steps"][0]
    assert s["context_start"] == 15350
    assert s["context_end"] == 18000
    assert s["stream_context_start"] == 23350
    assert s["stream_context_raw"] == 15350
    assert s["api_input_tokens"] == 24000
    # Round-level start is left for merge (end − tree); do not touch it.
    assert r["context_start"] == 11980


def test_caused_context_skips_last_and_shifts_call1():
    r = {
        "model_steps": [
            {
                "index": 1,
                "context_start": 20000,
                "context_end": 30000,
                "stream_context_start": 20000,
                "stream_context_end": 30000,
            },
            {
                "index": 2,
                "context_start": 50000,
                "context_end": 52000,
                "stream_context_start": 50000,
                "stream_context_end": 52000,
            },
            {
                "index": 3,
                "context_start": 80000,
                "context_end": 80000,
                "stream_context_start": 80000,
                "stream_context_end": 80000,
                "tokens_cached": 80000,
            },
        ],
    }
    _anchor_call_context_to_input(r)
    _apply_caused_context_display(r)
    a, b, last = r["model_steps"]
    assert last["skip_context"] is True
    assert last["display_context_start"] is None
    assert last["context_start"] == 80000
    # skip_context is display-only (no harness after last call), not last Cached=0
    assert last["tokens_cached"] == 80000
    assert a["skip_context"] is False
    assert a["display_context_start"] == 50000
    assert a["context_growth_est"] == 30000
    assert a["context_start"] == 20000
    assert b["display_context_start"] == 80000
    assert b["context_growth_est"] == 30000


def test_cold_call1_display_starts_at_zero():
    r = {
        "system_prompt": {"kind": "system_prompt", "tokens_in": 8000},
        "model_steps": [
            {
                "index": 1,
                "context_start": 5416,
                "context_end": 17924,
                "stream_context_start": 5416,
                "stream_context_end": 17924,
            },
            {
                "index": 2,
                "context_start": 17924,
                "context_end": 18969,
                "stream_context_start": 17924,
                "stream_context_end": 18969,
            },
            {
                "index": 3,
                "context_start": 18969,
                "context_end": 18969,
                "stream_context_start": 18969,
                "stream_context_end": 18969,
            },
        ],
    }
    _apply_caused_context_display(r)
    a, _b, last = r["model_steps"]
    assert last["skip_context"] is True
    assert a["display_context_start"] == 0
    assert a["display_context_end"] == 17924


def test_single_call_keeps_own_context():
    r = {
        "model_steps": [
            {
                "context_start": 138014,
                "context_end": 138014,
                "stream_context_start": 138014,
            }
        ],
    }
    _anchor_call_context_to_input(r)
    _apply_caused_context_display(r)
    s = r["model_steps"][0]
    assert s["skip_context"] is False
    assert s["display_context_start"] == 138014


def test_tooldef_message_bucket_is_window_remainder():
    boot = {
        "user_tokens": 3741,
        "parts": [
            {"kind": "system", "tokens": 1219},
            {"kind": "user_info", "tokens": 428},
            {"kind": "reminders", "tokens": 1663},
            {"kind": "mcp", "tokens": 99},
            {"kind": "tool_definitions", "tokens": 8200},
        ],
    }
    steps = [{"harness_pool_tokens": 4522}, {"harness_pool_tokens": 1563}]
    r = {"context_end": 22516}
    hist = 1219 + 428 + 1663 + 99
    harness = 4522 + 1563
    assert _estimate_tooldef_message_bucket(r, boot, steps) == (
        22516 - 3741 - harness - hist
    )


class _HB:
    def __init__(self, rounds):
        self.rounds = rounds


def test_tree_in_includes_user_compact_out():
    """Between-rounds Compact Out is off Call In but must stay in tree In."""
    r = {
        "user_prompt": {
            "kind": "user_prompt",
            "tokens_in": 294,
            "cost_in_usd": 0.001,
            "compact_out": {"tokens_in": 6295, "cost_in_usd": 0.03},
        },
        "model_steps": [
            {"tokens_in": 9687, "cost_in_usd": 0.05},
        ],
        "breakdown": {
            "cache_miss_in_tokens": 1000,
            "cache_miss_in_usd": 0.01,
            "harness_in_tokens": 99999,  # overwritten from steps
        },
    }
    _merge_bootstrap_into_breakdown(_HB([r]), r)
    bd = r["breakdown"]
    assert bd["user_in_tokens"] == 294
    assert bd["user_compact_out_tokens"] == 6295
    assert bd["harness_in_tokens"] == 9687
    assert bd["tree_in_tokens"] == 294 + 6295 + 9687 + 1000
    assert bd["tree_in_tokens"] != 294 + 9687 + 1000


def test_compact_warm_is_cached_xor_plus_out():
    compact = {
        "kind": "compaction",
        "tokens_before": 314734,
        "tokens_after": 5942,
        "tokens_removed": 308792,
    }
    prev = {"index": 1, "model_steps": [{"tokens_cached": 0, "estimate": {}}]}
    nxt = {"index": 2, "compact_before": compact, "model_steps": []}
    hb = _HB([prev, nxt])
    _fill_compact_cost(hb, nxt)
    assert compact["pre_read_cached_tokens"] == 314734
    assert compact["pre_read_uncached_tokens"] == 0
    assert compact["pre_read_cache_miss"] is False
    assert int(compact.get("out_tokens") or 0) > 0
    assert compact["out_tokens"] <= 5942
    assert (compact.get("pre_read_cached_usd") or 0) > 0
    assert (compact.get("out_usd") or 0) > 0


def test_compact_after_miss_is_in_not_cached():
    compact = {
        "kind": "compaction",
        "tokens_before": 100000,
        "tokens_after": 4000,
    }
    prev = {
        "index": 1,
        "cache_miss": True,
        "context_reread": True,
        "model_steps": [],
    }
    nxt = {"index": 2, "compact_before": compact, "model_steps": []}
    _fill_compact_cost(_HB([prev, nxt]), nxt)
    assert compact["pre_read_uncached_tokens"] == 100000
    assert compact["pre_read_cached_tokens"] == 0
    assert compact["pre_read_cache_miss"] is True
    assert int(compact.get("out_tokens") or 0) > 0
