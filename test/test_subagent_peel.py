"""Sub-agent link parsing + official-usage peel."""

from __future__ import annotations

import json
from pathlib import Path

from token_telemetry.session.subagents import (
    attach_subagents_after_steps,
    collect_child_ids_from_round,
    extract_task_ids,
    parse_subagent_meta,
    peel_round_usage,
    sub_usage,
)


SPAWN_TXT = """Subagent started in background.
subagent_id: 019ffb89-3d51-77d2-aef5-4394b3c13903
type: explore
description: Hunt dead Python code

When you need its result, use get_command_or_subagent_output with task_ids=["019ffb89-3d51-77d2-aef5-4394b3c13903"] and a positive timeout_ms.
"""

GET_TXT = """=== Multi-wait (wait_all) ===
--- Task 019ffb89-3d51-77d2-aef5-4394b3c13903 [completed] ---
Command: [subagent:explore] Hunt dead Python code
"""


def test_parse_spawn_result():
    m = parse_subagent_meta(SPAWN_TXT)
    assert m["subagent_id"] == "019ffb89-3d51-77d2-aef5-4394b3c13903"
    assert m["subagent_type"] == "explore"
    assert "Hunt dead" in m["subagent_description"]


def test_parse_get_task_line():
    m = parse_subagent_meta(GET_TXT)
    assert m["subagent_id"] == "019ffb89-3d51-77d2-aef5-4394b3c13903"


def test_extract_task_ids():
    ids = extract_task_ids(
        {
            "task_ids": [
                "019ffb89-3d51-77d2-aef5-4394b3c13903",
                "019ffb89-3d51-77d2-aef5-43aa5ea1554e",
            ]
        }
    )
    assert len(ids) == 2


def test_sub_usage_floors_at_zero():
    out = sub_usage(
        {"inputTokens": 100, "modelCalls": 5, "outputTokens": 3},
        {"inputTokens": 40, "modelCalls": 9, "outputTokens": 1},
    )
    assert out["inputTokens"] == 60
    assert out["modelCalls"] == 0
    assert out["outputTokens"] == 2


def test_collect_ids_from_round_tools():
    r = {
        "model_steps": [
            {
                "tools": [
                    {
                        "name": "get_command_or_subagent_output",
                        "subagent_ids": [
                            "019ffb89-3d51-77d2-aef5-4394b3c13903",
                            "019ffb89-3d51-77d2-aef5-43aa5ea1554e",
                        ],
                        "result_preview": GET_TXT,
                    }
                ]
            }
        ]
    }
    ids = collect_child_ids_from_round(r)
    assert "019ffb89-3d51-77d2-aef5-4394b3c13903" in ids
    assert "019ffb89-3d51-77d2-aef5-43aa5ea1554e" in ids


def test_peel_round_usage_reads_child_updates(tmp_path: Path):
    parent = tmp_path / "parent"
    parent.mkdir()
    kid_id = "019ffb89-3d51-77d2-aef5-4394b3c13903"
    kid = tmp_path / kid_id
    kid.mkdir()
    (kid / "summary.json").write_text(
        json.dumps({"session_kind": "subagent", "agent_name": "explore", "generated_title": "Hunt"}),
        encoding="utf-8",
    )
    rec = {
        "params": {
            "update": {
                "sessionUpdate": "turn_completed",
                "usage": {
                    "inputTokens": 1000,
                    "outputTokens": 20,
                    "cachedReadTokens": 800,
                    "totalTokens": 1020,
                    "modelCalls": 4,
                    "costUsdTicks": 1_000_000,
                },
            }
        }
    }
    (kid / "updates.jsonl").write_text(json.dumps(rec) + "\n", encoding="utf-8")
    parent_usage = {
        "inputTokens": 2500,
        "outputTokens": 50,
        "cachedReadTokens": 1800,
        "totalTokens": 2550,
        "modelCalls": 10,
        "costUsdTicks": 3_000_000,
    }
    peeled, meta = peel_round_usage(
        parent_usage, parent_dir=parent, child_ids=[kid_id]
    )
    assert meta["peeled"] is True
    assert peeled["inputTokens"] == 1500
    assert peeled["modelCalls"] == 6
    assert peeled["outputTokens"] == 30
    assert peeled["cachedReadTokens"] == 1000
    cards_round = {
        "model_steps": [
            {
                "tools": [
                    {
                        "name": "get_command_or_subagent_output",
                        "subagent_ids": [kid_id],
                    }
                ]
            }
        ]
    }
    attach_subagents_after_steps(cards_round, meta)
    after = cards_round["model_steps"][0]["subagents_after"]
    assert len(after) == 1
    assert after[0]["n"] == 1
    assert after[0]["session_id"] == kid_id
    assert after[0]["agent_name"] == "explore"
    assert after[0]["tokens_in"] == 200
    assert after[0]["tokens_cached"] == 800
    assert after[0]["tokens_out"] == 20
    assert after[0]["cost_in_usd"] > 0
    assert after[0]["cost_cached_usd"] > 0
    assert after[0]["cost_out_usd"] > 0


def test_last_call_context_stays_on_stream_window():
    from token_telemetry.hierarchy.finalize import _anchor_call_context_to_input

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
    last = r["model_steps"][1]
    assert last["context_start"] == 159101
    assert last["context_end"] == 159101
    assert last["stream_context_start"] == 159101
    first = r["model_steps"][0]
    assert first["context_start"] == 22000
