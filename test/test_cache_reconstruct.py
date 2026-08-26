"""Cache = rolling prefix (last call Cached=0). Round header = official."""

from __future__ import annotations

from token_telemetry.pricing.reconstruct import reconstruct_model_step_usage


def _step(start: int, *, context_start: int | None = None, **extra) -> dict:
    d = {
        "stream_context_start": int(start),
        "context_start": int(start if context_start is None else context_start),
        "children": [],
        "tools": [],
    }
    d.update(extra)
    return d


def _usage(off_in: int, off_cache: int, off_out: int) -> dict:
    return {
        "inputTokens": int(off_in),
        "cachedReadTokens": int(off_cache),
        "outputTokens": int(off_out),
    }


def _compact_in(step: dict) -> int:
    tot = 0
    for ch in step.get("children") or []:
        if ch.get("kind") != "phase_harness":
            continue
        for sub in ch.get("children") or []:
            if sub.get("kind") == "compact_out_in":
                tot += int(sub.get("tokens_in") or 0)
    return tot


def _identities(recon: dict, off_in: int, off_cache: int) -> None:
    steps = recon["steps"]
    bd = recon["breakdown"]
    assert int(recon["totals"]["cached_read"]) == int(off_cache)
    assert int(recon["totals"]["uncached_input"]) == int(off_in) - int(off_cache)
    assert int(bd.get("cached_tokens") or 0) == int(off_cache)
    assert int(bd.get("uncached_in_tokens") or 0) == int(off_in) - int(off_cache)
    omitted = int(bd.get("last_cache_omitted_tokens") or 0)
    # Last call Cached = 0 (no next LLM). Rolling holds for earlier calls.
    if steps:
        assert int(steps[-1].get("tokens_cached") or 0) == 0
    if len(steps) >= 2:
        assert omitted > 0
        for i in range(1, len(steps) - 1):
            prev_c = int(steps[i - 1].get("tokens_cached") or 0)
            prev_in = int(steps[i - 1].get("tokens_in") or 0)
            compact = _compact_in(steps[i - 1])
            assert int(steps[i].get("tokens_cached") or 0) == prev_c + max(
                0, prev_in - compact
            )
        prev_c = int(steps[-2].get("tokens_cached") or 0)
        prev_in = int(steps[-2].get("tokens_in") or 0)
        compact = _compact_in(steps[-2])
        assert omitted == prev_c + max(0, prev_in - compact)


def test_multi_call_warm_last_omits_cache():
    starts = [178033, 178637, 178812]
    off_in, off_cache, off_out = 535446, 533760, 511
    prior = 177511
    recon = reconstruct_model_step_usage(
        [_step(s) for s in starts],
        official_usage=_usage(off_in, off_cache, off_out),
        prior_context_tokens=prior,
    )
    c1 = recon["steps"][0]
    last = recon["steps"][-1]
    assert int(last["tokens_cached"] or 0) == 0
    assert int(c1["tokens_cached"]) == prior
    _identities(recon, off_in, off_cache)


def test_single_warm_call_omits_last_cache():
    off_in, off_cache, off_out = 138008, 137600, 168
    recon = reconstruct_model_step_usage(
        [_step(138014)],
        official_usage=_usage(off_in, off_cache, off_out),
        prior_context_tokens=137887,
    )
    assert len(recon["steps"]) == 1
    assert recon["steps"][0]["tokens_cached"] == 0
    assert int(recon["breakdown"].get("last_cache_omitted_tokens") or 0) == 137887
    _identities(recon, off_in, off_cache)


def test_cold_multi_identities():
    off_in, off_cache, off_out = 30000, 20000, 100
    recon = reconstruct_model_step_usage(
        [_step(7150), _step(20473)],
        official_usage=_usage(off_in, off_cache, off_out),
    )
    assert int(recon["breakdown"]["user_cache_share_tokens"] or 0) == 0
    # Cold, no System/User passed → Call 1 prefix is stream start.
    assert int(recon["steps"][0]["tokens_cached"] or 0) == 7150
    assert int(recon["steps"][-1]["tokens_cached"] or 0) == 0
    _identities(recon, off_in, off_cache)


def test_cold_r1_call1_not_empty_with_user():
    """R1 Call 1 Cached = prefix, not zeroed by treating full start as uncached."""
    recon = reconstruct_model_step_usage(
        [
            _step(5416, thought_summary_tokens=40),
            _step(17924, thought_summary_tokens=35),
            _step(18969, thought_summary_tokens=148),
        ],
        official_usage=_usage(52518, 29440, 521),
        user_uncached_tokens=1800,
    )
    c1, _c2, last = recon["steps"]
    assert int(c1["tokens_cached"] or 0) == 1800
    assert int(last["tokens_cached"] or 0) == 0
    _identities(recon, 52518, 29440)


def test_enc_residual_not_stacked_on_one_call():
    """Leftover Out after thought+message+toolreq lands on Enc pro-rata Enc TokZ."""
    recon = reconstruct_model_step_usage(
        [
            _step(
                10000,
                thought_summary_tokens=40,
                thought_encrypted_tokens=800,
                thought_encrypted_chars=1200,
            ),
            _step(
                11000,
                thought_summary_tokens=40,
                thought_encrypted_tokens=400,
                thought_encrypted_chars=600,
            ),
            _step(
                12000,
                thought_summary_tokens=40,
                thought_encrypted_tokens=200,
                thought_encrypted_chars=300,
            ),
        ],
        official_usage={
            "inputTokens": 33000,
            "cachedReadTokens": 20000,
            "outputTokens": 900,
            "reasoningTokens": 600,
        },
        prior_context_tokens=9000,
    )
    bd = recon["breakdown"]
    thought = int(bd.get("llm_thought_summary_tokens") or 0)
    reason = int(bd.get("llm_reasoning_tokens") or 0)
    harness = int(bd.get("llm_out_to_harness_tokens") or 0)
    user = int(bd.get("llm_out_to_user_tokens") or 0)
    assert thought + reason + harness + user == 900
    assert sum(int(s.get("tokens_out") or 0) for s in recon["steps"]) == 900
    encs = [
        int((s.get("composition") or {}).get("reasoning_encrypted_out") or 0)
        for s in recon["steps"]
    ]
    assert all(e > 0 for e in encs), encs
    assert encs[0] > encs[1] > encs[2]


def test_thought_peeled_from_enc_probe_r1():
    """thought_Z=22, message_Z=1, off_out=30 → thought_F=22, reasoning_F=7, Call Out=30."""
    recon = reconstruct_model_step_usage(
        [
            _step(
                10000,
                thought_summary_tokens=22,
                message_tokens=1,
                thought_encrypted_tokens=100,
            ),
        ],
        official_usage=_usage(10000, 0, 30),
    )
    s = recon["steps"][0]
    comp = s.get("composition") or {}
    assert int(comp.get("thought_out") or 0) == 22
    assert int(comp.get("reasoning_encrypted_out") or 0) == 7
    assert int(comp.get("message_out") or 0) == 1
    assert int(s.get("tokens_out") or 0) == 30
    bd = recon["breakdown"]
    assert int(bd.get("llm_thought_summary_tokens") or 0) == 22
    assert int(bd.get("llm_reasoning_tokens") or 0) == 7
    assert int(bd.get("llm_out_to_user_tokens") or 0) == 1


def test_cold_r1_nonlast_share_official_cache():
    recon = reconstruct_model_step_usage(
        [_step(5416), _step(17924), _step(18969)],
        official_usage=_usage(52518, 29440, 521),
        user_uncached_tokens=1800,
        system_uncached_tokens=11300,
        context_end_tokens=18969,
    )
    c1, c2, last = recon["steps"]
    assert int(c1["tokens_cached"]) == 13100
    assert int(last["tokens_cached"] or 0) == 0
    assert int(c2["tokens_cached"] or 0) > 0
    _identities(recon, 52518, 29440)


def test_prefers_stream_context_start():
    recon = reconstruct_model_step_usage(
        [_step(10000, context_start=999999)],
        official_usage=_usage(10000, 0, 0),
    )
    assert recon["breakdown"]["phys_raw_starts"] == [10000]
    assert recon["steps"][0]["estimate"]["input_tokens"] == 10000
    assert recon["steps"][0]["estimate"]["input_tokens"] != 999999


def test_warm_cache_miss_is_leftover_not_under_user():
    """Warm leftover uncached = round KV miss, not User In / User cache."""
    off_in, off_cache = 110000, 1000
    off_unc = off_in - off_cache
    user = 80
    recon = reconstruct_model_step_usage(
        [_step(100500)],
        official_usage=_usage(off_in, off_cache, 10),
        prior_context_tokens=100000,
        user_uncached_tokens=user,
    )
    bd = recon["breakdown"]
    harness = int(bd.get("harness_in_tokens") or 0)
    miss = int(bd.get("cache_miss_in_tokens") or 0)
    assert int(bd.get("user_in_tokens") or 0) == user
    assert miss == max(0, off_unc - user - harness)
    assert miss > 0
    assert int(bd.get("user_cache_share_tokens") or 0) == 0
    assert int(bd.get("tree_in_tokens") or 0) == user + harness + miss


def test_r1_system_remainder_is_not_round_miss():
    """Cold 1-call: off_unc ≈ System + user → round miss ≈ 0."""
    recon = reconstruct_model_step_usage(
        [_step(15100)],
        official_usage=_usage(10100, 0, 30),
        user_uncached_tokens=100,
        system_uncached_tokens=10000,
        context_end_tokens=15100,
    )
    bd = recon["breakdown"]
    harness = int(bd.get("harness_in_tokens") or 0)
    assert int(bd.get("cache_miss_in_tokens") or 0) == max(
        0, 10100 - 10000 - 100 - harness
    )
    assert int(bd.get("cache_miss_in_tokens") or 0) == 0
    assert int(bd.get("user_in_tokens") or 0) == 100
    assert int(recon["steps"][0].get("tokens_cached") or 0) == 0
    assert int(bd.get("last_cache_omitted_tokens") or 0) == 10100
    # Header Cached $ is official off_c (0); last call has no display prefix.
    assert float(bd.get("cached_usd") or 0) == 0.0
    assert float(recon["totals"].get("cost_cached_usd") or 0) == 0.0
