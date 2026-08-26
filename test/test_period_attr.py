"""Period Parts/Tools cats — hierarchy fields, per-event time."""

from token_telemetry.session.period_attr import parts_from_round, tools_from_round
from token_telemetry.session.aggregate import _add_cat_list, _cats_out, _place


def _out_tok(segs):
    return sum(segs.get(k, {}).get("tok", 0) for k in ("thought", "reasoning", "toolreq", "message"))


def test_parts_uses_our_reasoning_not_api_lump():
    r = {
        "breakdown": {
            "user_in_usd": 0.01,
            "user_in_tokens": 50,
            "harness_in_usd": 0.02,
            "harness_in_tokens": 80,
            "llm_thought_summary_usd": 0.03,
            "llm_thought_summary_tokens": 40,
            "llm_reasoning_encrypted_usd": 0.2,
            "llm_reasoning_encrypted_tokens": 400,
            "llm_out_to_user_usd": 0.04,
            "llm_out_to_user_tokens": 30,
            "output_tokens": 470,
            "output_usd": 0.27,
            "cached_usd": 0.01,
            "cached_tokens": 200,
        },
        "cost_cached_usd": 0.01,
    }
    segs = {s["k"]: s for s in parts_from_round(r)}
    assert segs["reasoning"]["tok"] == 400
    assert segs["thought"]["tok"] == 40
    assert segs["thought"]["tok"] != segs["reasoning"]["tok"]
    assert segs["thought"]["tok"] != 400  # thought is not Enc blob
    assert _out_tok(segs) <= 470
    assert "in" not in segs  # parts does not dump API input
    assert segs["user"]["tok"] == 50
    assert "cache_miss" not in segs


def test_parts_cache_miss_own_cat_not_user():
    """Miss is its own In cat; thought is not Enc leftover."""
    r = {
        "breakdown": {
            "user_in_usd": 0.01,
            "user_in_tokens": 50,
            "cache_miss_in_usd": 0.05,
            "cache_miss_in_tokens": 5000,
            "harness_in_usd": 0.02,
            "harness_in_tokens": 80,
            "llm_thought_summary_usd": 0.03,
            "llm_thought_summary_tokens": 22,
            "llm_reasoning_tokens": 7,
            "llm_reasoning_usd": 0.002,
            "llm_reasoning_encrypted_usd": 0.2,
            "llm_reasoning_encrypted_tokens": 100,
            "llm_out_to_user_usd": 0.04,
            "llm_out_to_user_tokens": 1,
            "output_tokens": 30,
            "output_usd": 0.072,
        },
        "cache_miss_in_tokens": 5000,
        "cache_miss_in_usd": 0.05,
    }
    segs = {s["k"]: s for s in parts_from_round(r)}
    assert segs["cache_miss"]["tok"] == 5000
    assert segs["user"]["tok"] == 50
    assert segs["user"]["tok"] != 5050
    assert segs["thought"]["tok"] == 22
    assert segs["reasoning"]["tok"] == 7
    assert segs["thought"]["tok"] != segs["reasoning"]["tok"]
    assert _out_tok(segs) <= 30


def test_parts_uses_hierarchy_reasoning_not_enc_blob_fallback():
    """Prefer llm_reasoning_* over encrypted blob; no clamp/scale to official Out."""
    r = {
        "breakdown": {
            "llm_thought_summary_usd": 0.01,
            "llm_thought_summary_tokens": 22,
            "llm_reasoning_tokens": 0,
            "llm_reasoning_usd": 0,
            "llm_reasoning_encrypted_tokens": 100,
            "llm_reasoning_encrypted_usd": 0.05,
            "llm_out_to_harness_tokens": 5,
            "llm_out_to_harness_usd": 0.002,
            "llm_out_to_user_tokens": 3,
            "llm_out_to_user_usd": 0.001,
            "output_tokens": 30,
            "output_usd": 0.013,
        },
    }
    segs = {s["k"]: s for s in parts_from_round(r)}
    assert "reasoning" not in segs  # explicit 0 wins over encrypted blob
    assert segs["thought"]["tok"] == 22
    assert segs["toolreq"]["tok"] == 5
    assert segs["message"]["tok"] == 3
    assert _out_tok(segs) == 30


def test_tools_splits_named_tools():
    r = {
        "model_steps": [
            {
                "cost_cached_usd": 0.0,
                "cached_read_tokens": 0,
                "children": [
                    {
                        "kind": "phase_harness",
                        "children": [
                            {"kind": "llm_to_in", "cost_in_usd": 0.01, "tokens_in": 20},
                            {"kind": "tool", "name": "grep x2", "cost_in_usd": 0.02, "tokens_in": 100},
                        ],
                    },
                    {
                        "kind": "phase_llm",
                        "children": [
                            {"kind": "thought", "cost_out_usd": 0.01, "tokens_out": 10},
                            {"kind": "reasoning", "cost_out_usd": 0.05, "tokenizer_tokens": 80},
                            {"kind": "tool_request", "name": "grep", "cost_out_usd": 0.01, "tokens_out": 5},
                        ],
                    },
                ],
            }
        ]
    }
    segs = {s["key"]: s for s in tools_from_round(r)}
    assert segs["llm_out_in"]["tok"] == 20
    assert segs["tool:grep"]["tok"] == 100
    assert segs["toolreq:grep"]["tok"] == 5
    assert segs["reasoning"]["tok"] == 80


def test_tools_includes_cache_miss_like_parts():
    r = {
        "breakdown": {
            "cache_miss_in_usd": 0.05,
            "cache_miss_in_tokens": 5000,
        },
        "cache_miss_in_tokens": 5000,
        "cache_miss_in_usd": 0.05,
        "model_steps": [
            {
                "children": [
                    {
                        "kind": "phase_harness",
                        "children": [
                            {"kind": "tool", "name": "grep", "cost_in_usd": 0.01, "tokens_in": 40},
                        ],
                    }
                ],
            }
        ],
    }
    by_key = {s["key"]: s for s in tools_from_round(r)}
    assert by_key["cache_miss"]["tok"] == 5000
    assert by_key["tool:grep"]["tok"] == 40


def test_tools_cached_is_round_official_not_sum_of_calls():
    """Cache miss makes Σ call prefixes ≠ official cachedRead — use round header."""
    r = {
        "breakdown": {
            "cached_usd": 0.01,
            "cached_tokens": 100,
        },
        "cost_cached_usd": 0.01,
        "cached_read_tokens": 100,
        "model_steps": [
            {
                "cost_cached_usd": 0.05,
                "cached_read_tokens": 500,
                "children": [],
            },
            {
                "cost_cached_usd": 0.04,
                "cached_read_tokens": 400,
                "children": [],
            },
        ],
    }
    segs = {s["k"]: s for s in tools_from_round(r)}
    assert segs["cached"]["tok"] == 100
    assert segs["cached"]["usd"] == 0.01
    assert segs["cached"]["tok"] != 900


def test_mid_round_compact_not_counted_as_tool():
    r = {
        "model_steps": [
            {
                "children": [
                    {
                        "kind": "phase_harness",
                        "children": [
                            {
                                "kind": "compact_out_in",
                                "attribution": "harness",
                                "cost_in_usd": 0.02,
                                "tokens_in": 4475,
                            },
                            {"kind": "tool", "name": "read_file", "cost_in_usd": 0.01, "tokens_in": 80},
                        ],
                    }
                ],
            }
        ]
    }
    segs = {s["key"]: s for s in tools_from_round(r)}
    assert "tool:read_file" in segs
    assert all(not str(k).startswith("tool:Compact") for k in segs)
    assert "compact_out_in" not in segs
    assert segs["compact_out"]["tok"] == 4475


def test_parts_between_rounds_compact_does_not_double_peel_harness():
    """user.compact_out folds into User; harness_in already excludes it."""
    r = {
        "user_prompt": {
            "tokens_in": 20,
            "cost_in_usd": 0.005,
            "prompt_tokens_in": 18,
            "prompt_cost_in_usd": 0.004,
            "compact_out": {"tokens_in": 6300, "cost_in_usd": 0.03},
        },
        "breakdown": {
            "user_in_usd": 0.005,
            "user_in_tokens": 20,
            "harness_in_usd": 0.05,
            "harness_in_tokens": 9700,
            "llm_thought_summary_usd": 0.01,
            "llm_thought_summary_tokens": 10,
            "llm_reasoning_tokens": 5,
            "llm_reasoning_usd": 0.002,
            "llm_out_to_user_tokens": 3,
            "llm_out_to_user_usd": 0.001,
            "output_tokens": 18,
            "output_usd": 0.013,
        },
        "model_steps": [
            {
                "user_compact_out_tokens": 6300,
                "tokens_in": 9700,
                "children": [
                    {
                        "kind": "phase_harness",
                        "tokens_in": 9700,
                        "children": [
                            {
                                "kind": "compact_out_in",
                                "attribution": "user",
                                "tokens_in": 6300,
                                "cost_in_usd": 0.03,
                            },
                            {
                                "kind": "tool",
                                "name": "read_file",
                                "tokens_in": 9700,
                                "cost_in_usd": 0.05,
                            },
                        ],
                    }
                ],
            }
        ],
    }
    segs = {s["k"]: s for s in parts_from_round(r)}
    assert segs["harness"]["tok"] == 9700
    # Parts User matches Tools fold (prompt TokZ + Compact Out), not billed user_in.
    assert segs["user"]["tok"] == 18 + 6300
    tools = {s["k"]: s for s in tools_from_round(r)}
    assert tools["prompt"]["tok"] + tools["compact_out"]["tok"] == segs["user"]["tok"]


def test_tools_user_breakdown_prompt_answer_compact():
    r = {
        "user_prompt": {
            "prompt_tokens_in": 40,
            "prompt_cost_in_usd": 0.01,
            "tokens_in": 40,
            "cost_in_usd": 0.01,
            "prev_llm_answer": {"tokens_in": 100, "cost_in_usd": 0.05, "round_index": 2},
        },
        "model_steps": [{"children": []}],
    }
    by_k = {s["k"]: s for s in tools_from_round(r)}
    assert by_k["prompt"]["tok"] == 40
    assert by_k["llm_answer"]["tok"] == 100
    assert "user" not in by_k

    r2 = {
        "compact_before": {"kind": "compaction", "placement": "between_rounds"},
        "user_prompt": {
            "prompt_tokens_in": 20,
            "prompt_cost_in_usd": 0.005,
            "tokens_in": 20,
            "cost_in_usd": 0.005,
            "compact_out": {"tokens_in": 4475, "cost_in_usd": 0.02},
        },
        "model_steps": [
            {
                "children": [
                    {
                        "kind": "phase_harness",
                        "children": [
                            {
                                "kind": "compact_out_in",
                                "attribution": "user",
                                "tokens_in": 4475,
                                "cost_in_usd": 0.02,
                            }
                        ],
                    }
                ],
            }
        ],
    }
    by2 = {s["k"]: s for s in tools_from_round(r2)}
    assert by2["prompt"]["tok"] == 20
    assert by2["compact_out"]["tok"] == 4475
    assert "llm_answer" not in by2


def test_place_event_in_its_own_bucket_not_session_start():
    specs = [
        {"start_epoch": 1000, "end_epoch": 2000},
        {"start_epoch": 2000, "end_epoch": 3000},
    ]
    assert _place(1500, specs) == 0
    assert _place(2500, specs) == 1
    acc0, acc1 = {}, {}
    _add_cat_list(acc0, [{"key": "thought", "k": "thought", "label": "thought", "usd": 1, "tok": 10}])
    _add_cat_list(acc1, [{"key": "thought", "k": "thought", "label": "thought", "usd": 2, "tok": 20}])
    assert _cats_out(acc0)[0]["tok"] == 10
    assert _cats_out(acc1)[0]["tok"] == 20
