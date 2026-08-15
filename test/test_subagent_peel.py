"""Sub-agent link parsing + official-usage peel."""

from __future__ import annotations

import json
import os
from pathlib import Path

from token_telemetry.session.subagents import (
    attach_subagents_after_steps,
    child_ids_to_peel,
    collect_child_ids_from_round,
    extract_task_ids,
    load_session_official_usage,
    parse_subagent_meta,
    peel_round_usage,
    price_child_usage,
    sibling_session_dir,
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


def test_sibling_session_dir_finds_other_cwd_folder(tmp_path: Path, monkeypatch):
    """Child may live under a different encoded-cwd folder than the parent."""
    root = tmp_path / "sessions"
    parent_root = root / "cwd-home"
    other_root = root / "cwd-project"
    parent = parent_root / "parent-session"
    kid_id = "01a005e9-be40-7473-a397-daedc7e861a4"
    kid = other_root / kid_id
    parent.mkdir(parents=True)
    kid.mkdir(parents=True)
    (kid / "updates.jsonl").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(
        "token_telemetry.session.discover.SESSIONS_ROOT",
        root,
    )
    found = sibling_session_dir(parent, kid_id)
    assert found is not None
    assert found.resolve() == kid.resolve()


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
    assert first["context_start"] == 20000
    assert first["context_end"] == 30000
    assert first["api_input_tokens"] == 22000
    assert first["stream_context_start"] == 20000
    assert last["stream_context_start"] == 159101


def _turn_completed(usage: dict) -> dict:
    return {
        "params": {
            "update": {
                "sessionUpdate": "turn_completed",
                "usage": usage,
            }
        }
    }


def _write_updates(session_dir: Path, usages: list[dict]) -> None:
    text = "".join(json.dumps(_turn_completed(u)) + "\n" for u in usages)
    path = session_dir / "updates.jsonl"
    path.write_text(text, encoding="utf-8")
    st = path.stat()
    os.utime(path, (st.st_atime, st.st_mtime + 2))


def test_incremental_peel_same_child_two_parent_rounds(tmp_path: Path):
    parent = tmp_path / "parent"
    parent.mkdir()
    kid_id = "019ffb89-3d51-77d2-aef5-4394b3c13903"
    kid = tmp_path / kid_id
    kid.mkdir()
    first = {
        "inputTokens": 1000,
        "outputTokens": 20,
        "cachedReadTokens": 800,
        "totalTokens": 1020,
        "modelCalls": 4,
        "costUsdTicks": 1_000_000,
    }
    _write_updates(kid, [first])
    parent_usage = {
        "inputTokens": 2500,
        "outputTokens": 50,
        "cachedReadTokens": 1800,
        "totalTokens": 2550,
        "modelCalls": 10,
        "costUsdTicks": 3_000_000,
    }
    cache: dict = {}
    peeled1, meta1 = peel_round_usage(
        dict(parent_usage),
        parent_dir=parent,
        child_ids=[kid_id],
        cache=cache,
    )
    assert meta1["peeled"] is True
    assert peeled1["inputTokens"] == 1500
    already_peeled = {kid_id: dict(meta1["children"][0]["usage"])}

    peeled2, meta2 = peel_round_usage(
        dict(parent_usage),
        parent_dir=parent,
        child_ids=[kid_id],
        cache=cache,
        already_peeled=already_peeled,
    )
    assert peeled2["inputTokens"] == parent_usage["inputTokens"]
    assert peeled2["modelCalls"] == parent_usage["modelCalls"]
    assert meta2["peeled"] is False

    extra = {
        "inputTokens": 500,
        "outputTokens": 10,
        "cachedReadTokens": 200,
        "totalTokens": 510,
        "modelCalls": 2,
        "costUsdTicks": 400_000,
    }
    _write_updates(kid, [first, extra])
    peeled3, meta3 = peel_round_usage(
        dict(parent_usage),
        parent_dir=parent,
        child_ids=[kid_id],
        cache=cache,
        already_peeled=already_peeled,
    )
    assert meta3["peeled"] is True
    assert peeled3["inputTokens"] == 2000
    assert peeled3["modelCalls"] == 8
    assert peeled3["outputTokens"] == 40


def test_load_session_official_usage_mtime_cache(tmp_path: Path):
    kid = tmp_path / "019ffb89-3d51-77d2-aef5-4394b3c13903"
    kid.mkdir()
    _write_updates(
        kid,
        [
            {
                "inputTokens": 1000,
                "outputTokens": 20,
                "cachedReadTokens": 800,
                "totalTokens": 1020,
                "modelCalls": 4,
            }
        ],
    )
    cache: dict = {}
    first = load_session_official_usage(kid, cache)
    second = load_session_official_usage(kid, cache)
    assert first["inputTokens"] == 1000
    assert second["inputTokens"] == 1000
    assert first is second
    _write_updates(
        kid,
        [
            {
                "inputTokens": 1000,
                "outputTokens": 20,
                "cachedReadTokens": 800,
                "totalTokens": 1020,
                "modelCalls": 4,
            },
            {
                "inputTokens": 500,
                "outputTokens": 10,
                "cachedReadTokens": 200,
                "totalTokens": 510,
                "modelCalls": 2,
            },
        ],
    )
    third = load_session_official_usage(kid, cache)
    assert third["inputTokens"] == 1500
    assert third["modelCalls"] == 6
    assert third is not first


def test_child_ids_to_peel_get_vs_spawn_only():
    kid = "019ffb89-3d51-77d2-aef5-4394b3c13903"
    get_round = {
        "model_steps": [
            {
                "tools": [
                    {
                        "name": "get_command_or_subagent_output",
                        "subagent_ids": [kid],
                        "result_preview": GET_TXT,
                    }
                ]
            }
        ]
    }
    assert kid in child_ids_to_peel(get_round)
    spawn_only = {
        "model_steps": [
            {
                "tools": [
                    {
                        "name": "spawn_subagent",
                        "subagent_id": kid,
                        "result_preview": SPAWN_TXT,
                    }
                ]
            }
        ]
    }
    assert child_ids_to_peel(spawn_only) == []
    assert kid in collect_child_ids_from_round(spawn_only)


def test_price_child_usage_uses_avg_prompt_not_lifetime_sum():
    from token_telemetry.pricing.rates import estimate_from_usage

    usage = {
        "inputTokens": 1_000_000,
        "totalTokens": 1_002_000,
        "modelCalls": 10,
    }
    priced = price_child_usage(usage)
    assert priced["context_tokens_for_tier"] == 100_000
    low = estimate_from_usage(usage, peak_context_tokens=100_000)
    high = estimate_from_usage(usage, peak_context_tokens=1_002_000)
    low_in = float((low.get("cost_usd") or {}).get("uncached_input") or 0)
    high_in = float((high.get("cost_usd") or {}).get("uncached_input") or 0)
    assert priced["cost_in_usd"] == low_in
    assert priced["cost_in_usd"] != high_in
    assert priced["official_usd"] == priced["estimate_usd"]


def test_sum_rounds_estimate_uses_api_total_not_ticks():
    from token_telemetry.session.monitor import _sum_rounds_estimate

    rounds = [
        {"breakdown": {"api_total_usd": 1.2}, "estimate_usd": 0.3},
        {"estimate_usd": 0.4, "recaps_after": [{"cost_usd": 0.05}]},
    ]
    assert abs(_sum_rounds_estimate(rounds) - 1.65) < 1e-9
