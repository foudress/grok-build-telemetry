"""Period Parts/Tools cats — hierarchy fields, per-event time."""

from token_telemetry.session.period_attr import parts_from_round, tools_from_round
from token_telemetry.session.aggregate import _add_cat_list, _cats_out, _place


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
            "cached_usd": 0.01,
            "cached_tokens": 200,
        },
        "cost_cached_usd": 0.01,
    }
    segs = {s["k"]: s for s in parts_from_round(r)}
    assert segs["reasoning"]["tok"] == 400
    assert segs["thought"]["tok"] == 40
    assert "in" not in segs  # parts does not dump API input
    assert segs["user"]["tok"] == 50


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
