"""Wave F goldens: Out peel, System remainder, round In, tools not scaled to off_unc.

Numeric identities live here (22+7+1=30, tree_in=user+harness+miss). Live
session replay was a probe SID skip — identities are synthetic reconstruct.
"""

from __future__ import annotations

from token_telemetry.pricing.reconstruct import reconstruct_model_step_usage
from token_telemetry.tokenizer import wrap_user_query


def _step(start: int, **extra) -> dict:
    d = {
        "stream_context_start": int(start),
        "context_start": int(start),
        "children": [],
        "tools": [],
    }
    d.update(extra)
    return d


def _phase_children(step: dict, kind: str) -> list[dict]:
    out: list[dict] = []
    for ch in step.get("children") or []:
        if ch.get("kind") != kind:
            continue
        out.extend(c for c in (ch.get("children") or []) if isinstance(c, dict))
    return out


def test_wrap_user_query_keeps_tags():
    inner = "hello world"
    wrapped = wrap_user_query(inner)
    assert wrapped.startswith("<user_query>")
    assert wrapped.endswith("</user_query>")
    assert wrap_user_query("<user_query>hi</user_query>") == "<user_query>hi</user_query>"


def test_r1_out_peel_22_7_1_equals_30():
    recon = reconstruct_model_step_usage(
        [
            _step(
                3808,
                thought_summary_tokens=22,
                message_tokens=1,
                thought_encrypted_tokens=130,
            )
        ],
        official_usage={
            "inputTokens": 13916,
            "cachedReadTokens": 128,
            "outputTokens": 30,
            "reasoningTokens": 25,
            "modelCalls": 1,
        },
        user_uncached_tokens=63,
        context_end_tokens=13916,
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
    assert int(bd.get("output_tokens") or 0) == 30
    assert int(s.get("tokens_cached") or 0) == 0
    assert int(bd.get("last_cache_omitted_tokens") or 0) > 0
    assert int(bd.get("bootstrap_residual_tokens") or 0) == 10108


def test_r1_round_in_is_user_plus_harness_plus_miss():
    recon = reconstruct_model_step_usage(
        [
            _step(
                3808,
                thought_summary_tokens=22,
                message_tokens=1,
                thought_encrypted_tokens=130,
            )
        ],
        official_usage={
            "inputTokens": 13916,
            "cachedReadTokens": 128,
            "outputTokens": 30,
            "reasoningTokens": 25,
            "modelCalls": 1,
        },
        user_uncached_tokens=63,
        context_end_tokens=13916,
    )
    bd = recon["breakdown"]
    user = int(bd.get("user_in_tokens") or 0)
    harness = int(bd.get("harness_in_tokens") or 0)
    miss = int(bd.get("cache_miss_in_tokens") or 0)
    off_unc = 13916 - 128
    assert user == 63
    # Silent-tools gap is bootstrap residual (System remainder), not stuffed System=off_in−user.
    assert int(bd.get("bootstrap_residual_tokens") or 0) == 10108
    assert int(bd.get("bootstrap_residual_tokens") or 0) == 13916 - 3808
    assert miss == max(0, off_unc - user - harness)
    assert int(bd.get("tree_in_tokens") or 0) == user + harness + miss


def test_tools_tokz_not_scaled_to_off_unc():
    tool_z = 3
    recon = reconstruct_model_step_usage(
        [
            _step(
                14114,
                stream_context_end=14314,
                context_end=14314,
                thought_summary_tokens=47,
                message_tokens=2,
                children=[
                    {
                        "kind": "phase_harness",
                        "children": [
                            {
                                "kind": "tool",
                                "name": "run_terminal_command",
                                "tokens_in": tool_z,
                                "tokenizer_tokens": tool_z,
                                "context_delta": tool_z,
                            }
                        ],
                    }
                ],
                tools=[
                    {
                        "name": "run_terminal_command",
                        "result_tokens_est": tool_z,
                        "ch_result_tokens": tool_z,
                    }
                ],
            )
        ],
        official_usage={
            "inputTokens": 28385,
            "cachedReadTokens": 17024,
            "outputTokens": 86,
            "modelCalls": 2,
        },
        prior_context_tokens=14114,
        user_uncached_tokens=124,
        context_end_tokens=14314,
    )
    bd = recon["breakdown"]
    off_unc = 28385 - 17024
    harness = int(bd.get("harness_in_tokens") or 0)
    miss = int(bd.get("cache_miss_in_tokens") or 0)
    user = int(bd.get("user_in_tokens") or 0)
    tool_in = 0
    tool_tz = 0
    for sub in _phase_children(recon["steps"][0], "phase_harness"):
        if sub.get("kind") != "tool":
            continue
        tool_in += int(sub.get("tokens_in") or 0)
        tool_tz += int(sub.get("tokenizer_tokens") or 0)
    assert tool_tz == tool_z
    # Δctx fit (~76), not leftover uncached (~11k)
    assert tool_in < 200
    assert tool_in != off_unc - user
    assert miss == max(0, off_unc - user - harness)
    assert miss > tool_in
    assert int(bd.get("tree_in_tokens") or 0) == user + harness + miss
