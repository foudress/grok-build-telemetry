"""Frozen ledger identities — fields, not helper names.

tree_in = user + harness + miss
R1 bill: System + tree_in = paid_unc
Compact Out ∈ User In
Last call Cached = 0
Agent line = child round I/O, never System

Uses public reconstruct + the breakdown writer + public Agent cards.
0 is a present value.
"""

from __future__ import annotations

from token_telemetry.hierarchy.breakdown import _merge_bootstrap_into_breakdown
from token_telemetry.pricing.reconstruct import reconstruct_model_step_usage
from token_telemetry.session.subagents import attach_subagents_after_steps


class _HB:
    def __init__(self, rounds=None):
        self.rounds = list(rounds or [])


def _step(start: int, **extra) -> dict:
    d = {
        "stream_context_start": int(start),
        "context_start": int(start),
        "children": [],
        "tools": [],
    }
    d.update(extra)
    return d


def _merge_round(**kwargs) -> dict:
    r = dict(kwargs)
    _merge_bootstrap_into_breakdown(_HB([r]), r)
    return r


def test_tree_in_equals_user_plus_harness_plus_miss():
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
    assert user == 63
    assert int(bd.get("tree_in_tokens") or 0) == user + harness + miss
    off_unc = 13916 - 128
    assert miss == max(0, off_unc - user - harness)


def test_r1_system_plus_tree_in_equals_paid_uncached():
    paid, sys_tok, user, harness = 28153, 10046, 324, 8641
    leftover = paid - sys_tok - user - harness
    r = _merge_round(
        index=1,
        context_end=19011,
        system_prompt={
            "kind": "system_prompt",
            "tokens_in": sys_tok,
            "cost_in_usd": 0.02,
            "message_residual_tokens": 6470,
        },
        user_prompt={"kind": "user_prompt", "tokens_in": user, "cost_in_usd": 0.001},
        model_steps=[{"tokens_in": harness, "cost_in_usd": 0.017}],
        breakdown={
            "uncached_in_tokens": paid,
            "paid_uncached_tokens": paid,
            "cold_round": True,
            "cache_miss_in_tokens": 0,
        },
    )
    bd = r["breakdown"]
    assert leftover == 9142
    assert bd["cache_miss_in_tokens"] == leftover
    assert bd["tree_in_tokens"] == user + harness + leftover
    assert sys_tok + bd["tree_in_tokens"] == paid


def test_compact_out_belongs_in_user_in_not_miss():
    user, co, harness, miss = 288, 6731, 1000, 50
    paid = user + co + harness + miss
    r = _merge_round(
        user_prompt={
            "kind": "user_prompt",
            "tokens_in": user,
            "cost_in_usd": 0.001,
            "compact_out": {"tokens_in": co, "cost_in_usd": 0.03},
        },
        model_steps=[{"tokens_in": harness, "cost_in_usd": 0.002}],
        breakdown={"uncached_in_tokens": paid, "cache_miss_in_tokens": 0},
    )
    bd = r["breakdown"]
    assert bd["user_compact_out_tokens"] == co
    assert bd["user_in_tokens"] == user + co
    assert r["user_prompt"]["compact_out_in_user_in"] is True
    assert bd["cache_miss_in_tokens"] == miss
    assert bd["tree_in_tokens"] == paid
    assert bd["tree_in_tokens"] == bd["user_in_tokens"] + harness + miss


def test_compact_out_not_double_counted_in_miss():
    user, co, harness = 294, 6295, 1000
    paid = user + co + harness
    r = _merge_round(
        user_prompt={
            "kind": "user_prompt",
            "tokens_in": user,
            "cost_in_usd": 0.001,
            "compact_out": {"tokens_in": co, "cost_in_usd": 0.03},
        },
        model_steps=[{"tokens_in": harness, "cost_in_usd": 0.05}],
        breakdown={
            "uncached_in_tokens": paid,
            "paid_uncached_tokens": paid,
            "cache_miss_in_tokens": co,
            "cache_miss_in_usd": 0.03,
        },
    )
    bd = r["breakdown"]
    assert bd["user_in_tokens"] == user + co
    assert bd["cache_miss_in_tokens"] == 0
    assert bd["tree_in_tokens"] == paid
    assert bd["tree_in_tokens"] != paid + co


def test_tree_in_includes_compact_out_off_call_in():
    r = _merge_round(
        user_prompt={
            "kind": "user_prompt",
            "tokens_in": 294,
            "cost_in_usd": 0.001,
            "compact_out": {"tokens_in": 6295, "cost_in_usd": 0.03},
        },
        model_steps=[{"tokens_in": 9687, "cost_in_usd": 0.05}],
        breakdown={
            "cache_miss_in_tokens": 1000,
            "cache_miss_in_usd": 0.01,
            "harness_in_tokens": 99999,
        },
    )
    bd = r["breakdown"]
    assert bd["user_in_tokens"] == 294 + 6295
    assert bd["user_compact_out_tokens"] == 6295
    assert bd["harness_in_tokens"] == 9687
    assert bd["tree_in_tokens"] == 294 + 6295 + 9687 + 1000
    assert bd["tree_in_tokens"] != 294 + 9687 + 1000


def test_last_call_cached_is_zero():
    recon = reconstruct_model_step_usage(
        [_step(178033), _step(178637), _step(178812)],
        official_usage={
            "inputTokens": 535446,
            "cachedReadTokens": 533760,
            "outputTokens": 511,
        },
        prior_context_tokens=177511,
    )
    steps = recon["steps"]
    assert int(steps[-1].get("tokens_cached") or 0) == 0
    assert int(steps[0].get("tokens_cached") or 0) == 177511
    assert int(recon["breakdown"].get("last_cache_omitted_tokens") or 0) > 0


def test_tree_in_zero_is_present_on_agent_line():
    """Present tree_in=0 must not fall through to user+call re-sum."""
    kid = "019ffb89-3d51-77d2-aef5-4394b3c13904"

    class _Snap:
        _session_dir = None
        _subagent_ordinal = {}
        _subagent_next_n = 0
        rounds = []
        _child_round_snaps = {
            kid: [
                {
                    "completed": True,
                    "index": 1,
                    "tree_in_tokens": 0,
                    "output_tokens": 20,
                    "user_prompt": {"kind": "user_prompt", "tokens_in": 400},
                    "model_steps": [
                        {"tokens_in": 100, "tokens_out": 20, "tokens_cached": 0}
                    ],
                    "usage_raw": {
                        "inputTokens": 10500,
                        "cachedReadTokens": 0,
                        "outputTokens": 20,
                    },
                    "breakdown": {"tree_in_tokens": 0, "cached_tokens": 0},
                }
            ]
        }
        _child_sys = {}
        _resume_alias = {}

    r = {
        "model_steps": [
            {
                "tools": [
                    {
                        "name": "get_command_or_subagent_output",
                        "subagent_id": kid,
                    }
                ]
            }
        ]
    }
    attach_subagents_after_steps(
        r,
        {
            "children": [
                {
                    "session_id": kid,
                    "usage": {
                        "inputTokens": 10500,
                        "cachedReadTokens": 0,
                        "outputTokens": 20,
                    },
                    "turns": [
                        {
                            "inputTokens": 10500,
                            "cachedReadTokens": 0,
                            "outputTokens": 20,
                        }
                    ],
                }
            ]
        },
        hb=_Snap(),
    )
    card = r["model_steps"][0]["subagents_after"][0]
    assert card.get("is_sys") is not True
    assert card["tokens_in"] == 0
    assert card["tokens_in"] != 500


def test_agent_line_is_round_io_not_system():
    kid = "019ffb89-3d51-77d2-aef5-4394b3c13903"
    sys_tok, tree_in, out = 10000, 500, 20

    class _Snap:
        _session_dir = None
        _subagent_ordinal = {}
        _subagent_next_n = 0
        rounds = []
        _child_round_snaps = {
            kid: [
                {
                    "completed": True,
                    "index": 1,
                    "tree_in_tokens": tree_in,
                    "output_tokens": out,
                    "system_prompt": {
                        "kind": "system_prompt",
                        "tokens_in": sys_tok,
                        "parts": [
                            {"kind": "system", "tokens": 2000},
                            {"kind": "tool_definitions", "tokens": 8000},
                        ],
                    },
                    "user_prompt": {"kind": "user_prompt", "tokens_in": 400},
                    "model_steps": [
                        {"tokens_in": 100, "tokens_out": out, "tokens_cached": 0}
                    ],
                    "usage_raw": {
                        "inputTokens": 10500,
                        "cachedReadTokens": 0,
                        "outputTokens": out,
                    },
                    "breakdown": {"tree_in_tokens": tree_in, "cached_tokens": 0},
                }
            ]
        }
        _child_sys = {}
        _resume_alias = {}

    r = {
        "model_steps": [
            {
                "tools": [
                    {
                        "name": "spawn_subagent",
                        "subagent_id": kid,
                        "result_preview": (
                            "Subagent started in background.\nsubagent_id: " + kid
                        ),
                    }
                ]
            },
            {
                "tools": [
                    {
                        "name": "get_command_or_subagent_output",
                        "subagent_id": kid,
                    }
                ]
            },
        ]
    }
    attach_subagents_after_steps(
        r,
        {
            "children": [
                {
                    "session_id": kid,
                    "usage": {
                        "inputTokens": 10500,
                        "cachedReadTokens": 0,
                        "outputTokens": out,
                    },
                    "turns": [
                        {
                            "inputTokens": 10500,
                            "cachedReadTokens": 0,
                            "outputTokens": out,
                        }
                    ],
                }
            ]
        },
        hb=_Snap(),
    )
    sys_card = r["model_steps"][0]["subagents_after"][0]
    agent = r["model_steps"][1]["subagents_after"][0]
    assert sys_card.get("is_sys") is True
    assert sys_card["tokens_in"] == sys_tok
    assert agent.get("is_sys") is not True
    assert agent["tokens_in"] == tree_in
    assert agent["tokens_out"] == out
    assert agent["tokens_in"] != 10500
    assert agent["tokens_in"] != sys_tok
    # Agent In + Sys In is not 2× System (System must not be billed twice).
    assert agent["tokens_in"] + sys_card["tokens_in"] != 2 * sys_tok
