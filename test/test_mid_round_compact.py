"""Mid-round auto-compact: attach, cursor, reconstruct split, compact_out_in."""

from __future__ import annotations

from token_telemetry.hierarchy.builder import HierarchyBuilder
from token_telemetry.hierarchy.cache_miss import _detect_context_reread
from token_telemetry.hierarchy.recap_compact import _fill_compact_cost, _on_compact
from token_telemetry.pricing.reconstruct import (
    _inject_compact_out_in,
    reconstruct_model_step_usage,
)
from token_telemetry.session.period_attr import _push_side


class _HB:
    def __init__(self) -> None:
        self.rounds: list = []
        self._open = None
        self._last_ctx = 180000
        self._cache_baseline = 180000
        self._last_compact = None
        self._pending_compact = None
        self._pending_recaps: list = []

    def _session_key(self) -> str:
        return "t"

    def _bump(self) -> None:
        pass


def _step_dict(start: int, end: int, **extra) -> dict:
    d = {
        "stream_context_start": int(start),
        "context_start": int(start),
        "stream_context_end": int(end),
        "context_end": int(end),
        "children": [],
        "tools": [],
    }
    d.update(extra)
    return d


def _compact_event(*, before=199847, after=4475, auto=True) -> dict:
    return {
        "tokens_before": before,
        "tokens_after": after,
        "summary_preview": None,
        "auto": auto,
    }


def _raw(kind: str, *, tt=None, stream=1, agent=1000, pid="p1", **update) -> dict:
    u = {"sessionUpdate": kind, **update}
    meta = {
        "promptId": pid,
        "streamStartMs": stream,
        "agentTimestampMs": agent,
    }
    if tt is not None:
        meta["totalTokens"] = tt
    return {"params": {"update": u, "_meta": meta}}


def test_mid_round_attach_does_not_stomp_context_end():
    hb = _HB()
    s0 = {
        "index": 1,
        "context_start": 180000,
        "context_end": 185000,
    }
    hb._open = {
        "index": 1,
        "model_steps": [s0],
        "context_start": 180000,
        "context_end": 185000,
        "notes": [],
        "compactions": [],
    }
    _on_compact(hb, _compact_event(), 111)
    cas = s0.get("compacts_after") or []
    assert cas and cas[0]["tokens_after"] == 4475
    assert cas[0]["placement"] == "mid_round"
    assert cas[0]["auto"] is True
    assert cas[0]["after_step_index"] == 1
    assert hb._open["context_end"] == 185000
    assert hb._open["mid_round_compacts"] is True
    assert hb._pending_compact is None
    assert hb._cache_baseline == 4475


def test_turn_completed_baseline_is_last_step_not_pre_compact_max():
    hb = HierarchyBuilder(max_rounds=20)
    hb.feed_raw(
        _raw(
            "user_message_chunk",
            content={"type": "text", "text": "hi"},
            tt=1000,
            agent=1,
        )
    )
    hb.feed_raw(
        _raw(
            "agent_thought_chunk",
            content={"type": "text", "text": "t1"},
            tt=180000,
            stream=10,
            agent=2,
        )
    )
    hb.feed_raw(
        _raw(
            "agent_message_chunk",
            content={"type": "text", "text": "m1"},
            tt=181000,
            stream=10,
            agent=3,
        )
    )
    hb.feed_raw(
        _raw(
            "auto_compact_completed",
            tokens_before=199847,
            tokens_after=4475,
            agent=4,
        )
    )
    open_r = hb._open
    assert open_r is not None
    s0 = open_r["model_steps"][0]
    assert (s0.get("compacts_after") or [])[0]["tokens_after"] == 4475
    assert open_r["context_end"] != 4475
    hb.feed_raw(
        _raw(
            "agent_thought_chunk",
            content={"type": "text", "text": "t2"},
            tt=4500,
            stream=20,
            agent=5,
        )
    )
    hb.feed_raw(
        _raw(
            "agent_message_chunk",
            content={"type": "text", "text": "m2"},
            tt=8000,
            stream=20,
            agent=6,
        )
    )
    hb.feed_raw(
        _raw(
            "turn_completed",
            usage={
                "inputTokens": 200000,
                "cachedReadTokens": 180000,
                "outputTokens": 500,
                "modelCalls": 2,
            },
            agent=7,
        )
    )
    assert hb._open is None
    assert hb._cache_baseline == 8000
    last = hb.rounds[-1]
    assert last["context_end"] == 8000
    assert last["context_end"] < 100000


def test_reconstruct_split_post_tools_not_cliff_dump():
    compact = {
        "kind": "compaction",
        "auto": True,
        "tokens_before": 199847,
        "tokens_after": 4475,
        "out_tokens": 4475,
        "placement": "mid_round",
        "after_step_index": 1,
    }
    pre = _step_dict(180000, 185000, index=1, compacts_after=[compact])
    post = _step_dict(
        4480,
        12000,
        index=2,
        children=[
            {
                "kind": "phase_harness",
                "children": [
                    {
                        "kind": "tool",
                        "name": "read_file",
                        "tokens_in": 800,
                        "context_delta": 800,
                    }
                ],
            }
        ],
        tools=[
            {
                "name": "read_file",
                "result_tokens_est": 800,
                "ch_result_tokens": 800,
            }
        ],
    )
    recon = reconstruct_model_step_usage(
        [pre, post],
        official_usage={
            "inputTokens": 400000,
            "cachedReadTokens": 320000,
            "outputTokens": 2000,
            "modelCalls": 2,
        },
        prior_context_tokens=175000,
    )
    steps = recon["steps"]
    assert len(steps) == 2
    assert int(steps[1].get("stream_context_start") or steps[1].get("context_start") or 0) < 10000
    tool_in = 0
    for ch in steps[1].get("children") or []:
        if ch.get("kind") != "phase_harness":
            continue
        for sub in ch.get("children") or []:
            if sub.get("kind") == "tool":
                tool_in += int(sub.get("tokens_in") or 0)
    # Δctx fit: post-compact window leftover (~3k) onto tools, never off_unc (~80k).
    off_unc = 400000 - 320000
    miss = int(recon["breakdown"].get("cache_miss_in_tokens") or 0)
    assert 800 <= tool_in <= 4000
    assert tool_in < off_unc / 5
    assert miss > tool_in
    if tool_in > 0:
        assert 800 / tool_in > 0.2


def test_prune_does_not_reuse_round_index():
    """Live tree/chart join Compact · auto by round.index. Reusing
    len(rounds)+1 after prune labeled every card Round 25 and dropped C bars."""
    hb = HierarchyBuilder(max_rounds=8)
    for i in range(1, 21):
        pid = f"p{i}"
        hb.feed_raw(
            _raw(
                "user_message_chunk",
                content={"type": "text", "text": f"u{i}"},
                tt=1000 + i,
                agent=i * 10,
                pid=pid,
            )
        )
        hb.feed_raw(
            _raw(
                "agent_thought_chunk",
                content={"type": "text", "text": "t"},
                tt=2000 + i,
                stream=i,
                agent=i * 10 + 1,
                pid=pid,
            )
        )
        hb.feed_raw(
            _raw(
                "turn_completed",
                usage={
                    "inputTokens": 1000,
                    "cachedReadTokens": 0,
                    "outputTokens": 10,
                    "modelCalls": 1,
                },
                agent=i * 10 + 2,
                pid=pid,
            )
        )
    assert len(hb.rounds) == 8
    indexes = [r["index"] for r in hb.rounds]
    assert indexes == list(range(13, 21))
    assert len(set(indexes)) == 8
    assert hb.rounds[-1]["index"] == 20


def test_compact_out_in_on_first_post_step():
    compact = {
        "kind": "compaction",
        "auto": True,
        "tokens_before": 199847,
        "tokens_after": 4475,
        "out_tokens": 4475,
        "placement": "mid_round",
        "after_step_index": 1,
    }
    pre = _step_dict(180000, 185000, index=1, compacts_after=[compact])
    post = _step_dict(
        4480,
        12000,
        index=2,
        children=[
            {
                "kind": "phase_harness",
                "children": [
                    {
                        "kind": "tool",
                        "name": "read_file",
                        "tokens_in": 800,
                        "context_delta": 800,
                    }
                ],
            }
        ],
        tools=[{"name": "read_file", "result_tokens_est": 800, "ch_result_tokens": 800}],
    )
    recon = reconstruct_model_step_usage(
        [pre, post],
        official_usage={
            "inputTokens": 400000,
            "cachedReadTokens": 320000,
            "outputTokens": 2000,
            "modelCalls": 2,
        },
        prior_context_tokens=175000,
    )
    kids = []
    for ch in recon["steps"][1].get("children") or []:
        if ch.get("kind") == "phase_harness":
            kids = list(ch.get("children") or [])
            break
    assert kids and kids[0].get("kind") == "compact_out_in"
    assert int(kids[0].get("tokens_in") or 0) == 4475
    tz = int(kids[0].get("tokenizer_tokens") or kids[0].get("tokens_in") or 0)
    tf = int(kids[0].get("tokens_in") or 0)
    assert tz == 4475 and tf == 4475
    assert all(c.get("kind") != "compact_out_in" for c in kids[1:])


def test_inject_compact_out_in_is_idempotent():
    step = {
        "children": [{"kind": "phase_harness", "children": [], "tokens_in": 0}],
    }
    # Between-rounds default: attribution=user — node kept for cache math, not
    # added to Harness In display totals (User owns Compact Out).
    compact = {"kind": "compaction", "tokens_after": 4475, "out_tokens": 4475}
    assert _inject_compact_out_in(step, compact) is True
    assert _inject_compact_out_in(step, compact) is False
    kids = step["children"][0]["children"]
    assert sum(1 for c in kids if c.get("kind") == "compact_out_in") == 1
    assert kids[0].get("attribution") == "user"
    assert int(step["children"][0]["tokens_in"] or 0) == 0


def test_inject_mid_round_bumps_harness_in():
    step = {
        "children": [{"kind": "phase_harness", "children": [], "tokens_in": 100}],
    }
    compact = {
        "kind": "compaction",
        "tokens_after": 4475,
        "out_tokens": 4475,
        "placement": "mid_round",
    }
    assert _inject_compact_out_in(step, compact) is True
    kids = step["children"][0]["children"]
    assert kids[0].get("attribution") == "harness"
    assert int(step["children"][0]["tokens_in"] or 0) == 100 + 4475


def test_between_rounds_call1_harness_excludes_user_compact():
    """Call/Harness In = tools only; Compact Out stamped for User, not peeled twice."""
    compact = {
        "kind": "compaction",
        "auto": True,
        "tokens_before": 180000,
        "tokens_after": 6300,
        "out_tokens": 6300,
        "placement": "between_rounds",
        "_attribution": "user",
    }
    post = _step_dict(
        6500,
        20000,
        index=1,
        children=[
            {
                "kind": "phase_harness",
                "children": [
                    {
                        "kind": "tool",
                        "name": "read_file",
                        "tokens_in": 4000,
                        "context_delta": 4000,
                    },
                    {
                        "kind": "tool",
                        "name": "grep",
                        "tokens_in": 5700,
                        "context_delta": 5700,
                    },
                ],
            }
        ],
        tools=[
            {"name": "read_file", "result_tokens_est": 4000, "ch_result_tokens": 4000},
            {"name": "grep", "result_tokens_est": 5700, "ch_result_tokens": 5700},
        ],
    )
    recon = reconstruct_model_step_usage(
        [post],
        official_usage={
            "inputTokens": 50000,
            "cachedReadTokens": 30000,
            "outputTokens": 500,
            "modelCalls": 1,
        },
        prior_context_tokens=6300,
        compact_reentry=compact,
    )
    s0 = recon["steps"][0]
    assert int(s0.get("user_compact_out_tokens") or 0) == 6300
    # Display Call In must not include User-owned Compact Out.
    assert int(s0.get("tokens_in") or 0) == int(s0.get("harness_in_tokens") or 0)
    harness = None
    for ch in s0.get("children") or []:
        if ch.get("kind") == "phase_harness":
            harness = ch
            break
    assert harness is not None
    kids = list(harness.get("children") or [])
    assert kids and kids[0].get("kind") == "compact_out_in"
    assert kids[0].get("attribution") == "user"
    tools_sum = sum(
        int(c.get("tokens_in") or 0)
        for c in kids
        if c.get("kind") == "tool"
    )
    # Harness header must match visible tool subcategory total (no double-peel).
    assert tools_sum > 0
    assert int(harness.get("tokens_in") or 0) == tools_sum
    assert int(s0.get("tokens_in") or 0) == tools_sum
    assert int(s0.get("tokens_in") or 0) != tools_sum + 6300


def test_between_rounds_still_one_card_and_prior():
    hb = _HB()
    hb._open = None
    hb.rounds = [
        {
            "index": 1,
            "model_steps": [{"index": 2, "context_end": 180000}],
            "context_end": 180000,
            "notes": [],
            "compactions": [],
        }
    ]
    _on_compact(hb, _compact_event(), 222)
    last = hb.rounds[-1]
    assert last["compact_after"]["tokens_after"] == 4475
    assert last["compact_after"]["placement"] == "between_rounds"
    assert last.get("context_after_compact") == 4475
    assert last["context_end"] == 180000
    assert hb._pending_compact is last["compact_after"]
    assert not (last["model_steps"][0].get("compacts_after") or [])

    nxt = {
        "index": 2,
        "compact_before": hb._pending_compact,
        "model_steps": [_step_dict(4480, 9000, index=1)],
    }
    recon = reconstruct_model_step_usage(
        nxt["model_steps"],
        official_usage={
            "inputTokens": 12000,
            "cachedReadTokens": 4000,
            "outputTokens": 200,
            "modelCalls": 1,
        },
        prior_context_tokens=4475,
        compact_reentry=nxt["compact_before"],
    )
    kids = []
    for ch in recon["steps"][0].get("children") or []:
        if ch.get("kind") == "phase_harness":
            kids = list(ch.get("children") or [])
            break
    assert kids and kids[0].get("kind") == "compact_out_in"
    assert int(kids[0]["tokens_in"]) == 4475


def test_warm_tiny_after_compact_is_not_miss():
    hb = _HB()
    r = {
        "completed": True,
        "usage_raw": {"inputTokens": 9000, "cachedReadTokens": 4000},
        "user_prompt": {
            "kind": "user_prompt",
            "prior_context": 4436,
            "tokens_in": 80,
            "uncached_est": 80,
            "tokens_cached": 4436,
        },
        "context_end": 8000,
        "model_steps": [
            {"stream_context_start": 4482, "context_start": 4482}
        ],
        "breakdown": {},
    }
    assert _detect_context_reread(hb, r) is None


def test_stale_prior_compact_collapse_is_not_miss():
    hb = _HB()
    r = {
        "completed": True,
        "usage_raw": {"inputTokens": 244535, "cachedReadTokens": 203264},
        "user_prompt": {"prior_context": 150000, "tokens_in": 80, "uncached_est": 80},
        "context_end": 39805,
        "model_steps": [
            {"stream_context_start": 4482, "context_start": 4482}
        ],
        "breakdown": {},
    }
    assert _detect_context_reread(hb, r) is None


def test_idle_then_compact_is_not_idle_reread():
    hb = _HB()
    prior = 150000
    r = {
        "completed": True,
        "idle_gap_ms": 11 * 3600 * 1000,
        "mid_round_compacts": True,
        "usage_raw": {
            "inputTokens": prior + 20000,
            "cachedReadTokens": 1000,
        },
        "user_prompt": {
            "prior_context": prior,
            "tokens_in": 80,
            "uncached_est": 80,
        },
        "context_end": 12000,
        "model_steps": [
            {"stream_context_start": 180000, "context_start": 180000}
        ],
        "breakdown": {},
        "compactions": [{"kind": "compaction", "tokens_after": 4475}],
    }
    assert _detect_context_reread(hb, r) is None


def test_mid_round_xor_cached_plus_out():
    compact = {
        "kind": "compaction",
        "tokens_before": 199847,
        "tokens_after": 4475,
        "tokens_removed": 195372,
    }
    s0 = {"index": 1, "compacts_after": [compact], "estimate": {}}
    nxt = {"index": 1, "model_steps": [s0]}
    prev = {"index": 0, "model_steps": []}
    _fill_compact_cost(_HB_rounds := _HB(), nxt)
    _HB_rounds.rounds = [prev, nxt]
    _fill_compact_cost(_HB_rounds, nxt)
    assert compact["pre_read_cached_tokens"] == 199847
    assert compact["pre_read_uncached_tokens"] == 0
    assert compact["pre_read_cache_miss"] is False
    assert compact["out_tokens"] == 4475
    assert (compact.get("pre_read_cached_usd") or 0) > 0
    assert (compact.get("out_usd") or 0) > 0


def test_mid_round_period_has_own_epoch():
    events: list = []
    c = {
        "kind": "compaction",
        "agent_ms": 5_000_000,
        "tokens_before": 199847,
        "out_tokens": 4475,
        "cost_usd": 0.4,
        "pre_read_cached_tokens": 199847,
        "pre_read_cached_usd": 0.06,
    }
    _push_side(events, c, recap=False)
    assert len(events) == 1
    assert events[0]["epoch"] == 5_000_000


def test_open_zero_steps_is_compact_before_not_mid_round():
    hb = _HB()
    hb.rounds = [
        {
            "index": 1,
            "model_steps": [{"index": 1, "context_end": 90000}],
            "context_end": 90000,
            "notes": [],
            "compactions": [],
        }
    ]
    hb._open = {
        "index": 2,
        "model_steps": [],
        "notes": [],
        "compactions": [],
    }
    _on_compact(hb, _compact_event(), 333)
    assert hb._open.get("compact_before")["placement"] == "between_rounds"
    assert not hb._open.get("mid_round_compacts")
    assert hb.rounds[-1]["compact_after"] is hb._open["compact_before"]
