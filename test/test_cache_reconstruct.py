"""Cache = Input − Uncached (last call included)."""

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


def _identities(recon: dict, off_in: int, off_cache: int) -> None:
    steps = recon["steps"]
    bd = recon["breakdown"]
    omitted = int(bd.get("last_cache_omitted_tokens") or 0)
    sum_cache = sum(int(s.get("tokens_cached") or 0) for s in steps)
    assert sum_cache + omitted == int(off_cache)
    sum_paid = sum(int(s.get("paid_at_start_tokens") or 0) for s in steps)
    assert sum_paid == int(off_in) - int(off_cache)
    assert int(recon["totals"]["cached_read"]) == int(off_cache)
    assert int(recon["totals"]["uncached_input"]) == int(off_in) - int(off_cache)
    if len(steps) >= 2:
        assert int(steps[-1].get("tokens_cached") or 0) == 0


def test_multi_call_warm_last_zero_c1_keeps_own_prefix():
    starts = [178033, 178637, 178812]
    off_in, off_cache, off_out = 535446, 533760, 511
    recon = reconstruct_model_step_usage(
        [_step(s) for s in starts],
        official_usage=_usage(off_in, off_cache, off_out),
        prior_context_tokens=177511,
    )
    c1 = recon["steps"][0]
    last = recon["steps"][-1]
    omitted = int(recon["breakdown"].get("last_cache_omitted_tokens") or 0)
    assert last["tokens_cached"] == 0
    assert omitted > 0
    # Call 1 is the prefix at *that* prompt — not Call 1 + last dumped.
    assert int(c1["tokens_cached"]) > 0
    assert int(c1["tokens_cached"]) < off_cache
    assert int(c1["tokens_cached"]) < omitted + 50_000
    _identities(recon, off_in, off_cache)


def test_single_warm_call_keeps_prefix():
    off_in, off_cache, off_out = 138008, 137600, 168
    recon = reconstruct_model_step_usage(
        [_step(138014)],
        official_usage=_usage(off_in, off_cache, off_out),
        prior_context_tokens=137887,
    )
    assert len(recon["steps"]) == 1
    # n==1: the only call *is* the prompt — show its prefix, last-omit=0.
    assert int(recon["breakdown"].get("last_cache_omitted_tokens") or 0) == 0
    assert recon["steps"][0]["tokens_cached"] > 0
    _identities(recon, off_in, off_cache)


def test_cold_multi_identities():
    off_in, off_cache, off_out = 30000, 20000, 100
    recon = reconstruct_model_step_usage(
        [_step(7150), _step(20473)],
        official_usage=_usage(off_in, off_cache, off_out),
    )
    assert recon["steps"][-1]["tokens_cached"] == 0
    assert int(recon["breakdown"]["user_cache_share_tokens"] or 0) == 0
    # Cold Call 1 keeps its prefix (System is not paid@start on the call).
    assert int(recon["steps"][0]["tokens_cached"] or 0) > 0
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
    assert last["tokens_cached"] == 0
    assert int(c1["tokens_cached"] or 0) > 0
    assert int(c1["tokens_cached"] or 0) < 29440
    _identities(recon, 52518, 29440)


def test_enc_residual_not_stacked_on_one_call():
    """Leftover Out lands on Encrypted pro-rata Enc TokZ."""
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
    reason = int(bd.get("llm_reasoning_encrypted_tokens") or 0)
    harness = int(bd.get("llm_out_to_harness_tokens") or 0)
    user = int(bd.get("llm_out_to_user_tokens") or 0)
    assert reason + harness + user == 900
    encs = [
        int((s.get("composition") or {}).get("reasoning_encrypted_out") or 0)
        for s in recon["steps"]
    ]
    assert all(e > 0 for e in encs), encs
    assert encs[0] > encs[1] > encs[2]


def test_cold_r1_nonlast_share_official_cache():
    recon = reconstruct_model_step_usage(
        [_step(5416), _step(17924), _step(18969)],
        official_usage=_usage(52518, 29440, 521),
        user_uncached_tokens=1800,
        system_uncached_tokens=11300,
        context_end_tokens=18969,
    )
    c1, c2, last = recon["steps"]
    assert last["tokens_cached"] == 0
    assert int(c1["tokens_cached"]) == 13100
    assert int(c1["tokens_cached"]) + int(c2["tokens_cached"]) == 29440


def test_prefers_stream_context_start():
    recon = reconstruct_model_step_usage(
        [_step(10000, context_start=999999)],
        official_usage=_usage(10000, 0, 0),
    )
    assert recon["breakdown"]["phys_raw_starts"] == [10000]
    assert recon["steps"][0]["estimate"]["input_tokens"] == 10000
    assert recon["steps"][0]["estimate"]["input_tokens"] != 999999
