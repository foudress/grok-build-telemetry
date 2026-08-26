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


def test_bg_shell_get_command_is_not_a_subagent_card(tmp_path: Path):
    """Background run_terminal_command waits share get_command_or_subagent_output.

    Graph session R2 Sub Agent 9/11/12 were empty cards for dashboard-restart
    task ids (no session dir) — not auto-compact, not spawn_subagent.
    """
    parent = tmp_path / "parent-session"
    parent.mkdir()
    (parent / "updates.jsonl").write_text("", encoding="utf-8")
    bg_id = "01a0198a-495b-7be1-a3d3-eb6b63e22dbb"
    _peeled, meta = peel_round_usage(
        {"inputTokens": 100, "modelCalls": 1, "outputTokens": 1},
        parent_dir=parent,
        child_ids=[bg_id],
    )
    assert meta.get("children") == []
    r = {
        "model_steps": [
            {
                "tools": [
                    {
                        "name": "get_command_or_subagent_output",
                        "subagent_id": bg_id,
                        "subagent_ids": [bg_id],
                        "title": "python scripts/live_dashboard.py",
                    }
                ]
            }
        ]
    }
    attach_subagents_after_steps(r, meta)
    assert not r["model_steps"][0].get("subagents_after")


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
    assert priced["official_usd"] is None
    assert priced["estimate_usd"] == float((low.get("cost_usd") or {}).get("total") or 0)


def test_sum_rounds_estimate_uses_api_total_not_ticks():
    from token_telemetry.session.monitor import _sum_rounds_estimate

    rounds = [
        {"breakdown": {"api_total_usd": 1.2}, "estimate_usd": 0.3},
        {"estimate_usd": 0.4, "recaps_after": [{"cost_usd": 0.05}]},
    ]
    assert abs(_sum_rounds_estimate(rounds) - 1.65) < 1e-9


def test_session_estimate_adds_child_round_bills():
    from token_telemetry.session.monitor import _sum_rounds_estimate

    parent = [{"breakdown": {"api_total_usd": 1.0}}]
    child_a = [{"breakdown": {"api_total_usd": 0.25}}]
    child_b = [{"estimate_usd": 0.10}]
    total = _sum_rounds_estimate(parent) + _sum_rounds_estimate(child_a) + _sum_rounds_estimate(child_b)
    assert abs(total - 1.35) < 1e-9
    assert abs(_sum_rounds_estimate(parent) - 1.0) < 1e-9


def test_extract_resume_from_and_root_walk(tmp_path: Path):
    from token_telemetry.session.subagents import (
        extract_resume_from,
        root_subagent_id,
    )

    old = "019ffb89-3d51-77d2-aef5-4394b3c13903"
    new = "01a01fae-9094-71e1-a2ef-6aef88b2810e"
    assert extract_resume_from({"resume_from": old}) == old
    orig = tmp_path / old
    nxt = tmp_path / new
    orig.mkdir()
    nxt.mkdir()
    (orig / "updates.jsonl").write_text("{}\n", encoding="utf-8")
    (nxt / "updates.jsonl").write_text("{}\n", encoding="utf-8")
    (orig / "summary.json").write_text(
        json.dumps({"session_kind": "subagent"}), encoding="utf-8"
    )
    (nxt / "summary.json").write_text(
        json.dumps(
            {
                "session_kind": "subagent_resume",
                "parent_session_id": old,
            }
        ),
        encoding="utf-8",
    )
    assert root_subagent_id(new, parent_dir=orig) == old
    assert root_subagent_id(old, parent_dir=orig) == old


def test_attach_resume_reuses_n_as_r2():
    old = "019ffb89-3d51-77d2-aef5-4394b3c13903"
    new = "01a01fae-9094-71e1-a2ef-6aef88b2810e"

    class _HB:
        _session_dir = None
        _subagent_ordinal = {}
        _subagent_result_n = {}
        _subagent_next_n = 0

    hb = _HB()
    r1 = {
        "model_steps": [
            {
                "tools": [
                    {
                        "name": "get_command_or_subagent_output",
                        "subagent_id": old,
                    }
                ]
            }
        ]
    }
    hb.rounds = []
    attach_subagents_after_steps(
        r1, {"children": [{"session_id": old, "title": "Wave B"}]}, hb=hb
    )
    hb.rounds = [r1]
    c1 = r1["model_steps"][0]["subagents_after"][0]
    assert c1["n"] == 1
    assert c1["resume_index"] == 1
    assert c1["label"] == "Sub Agent 1"

    r2 = {
        "model_steps": [
            {
                "tools": [
                    {
                        "name": "get_command_or_subagent_output",
                        "subagent_id": new,
                        "resume_from": old,
                    }
                ]
            }
        ]
    }
    hb.rounds = [r1, r2]
    attach_subagents_after_steps(
        r2, {"children": [{"session_id": new, "title": "Wave B"}]}, hb=hb
    )
    c1 = r1["model_steps"][0]["subagents_after"][0]
    c2 = r2["model_steps"][0]["subagents_after"][0]
    assert c2["n"] == 1
    assert c2["resume_index"] == 2
    assert c1["label"] == "Sub Agent 1 R1"
    assert c2["label"] == "Sub Agent 1 R2"
    assert c2["root_session_id"] == old


def test_two_child_turns_in_one_wait_are_r1_r2(tmp_path: Path):
    kid_id = "019ffb89-3d51-77d2-aef5-4394b3c13903"
    parent = tmp_path / "parent"
    parent.mkdir()
    kid = tmp_path / kid_id
    kid.mkdir()
    (kid / "summary.json").write_text(
        json.dumps({"session_kind": "subagent"}), encoding="utf-8"
    )

    def rec(inn, out, cache, calls, ticks):
        return json.dumps(
            {
                "params": {
                    "update": {
                        "sessionUpdate": "turn_completed",
                        "usage": {
                            "inputTokens": inn,
                            "outputTokens": out,
                            "cachedReadTokens": cache,
                            "modelCalls": calls,
                            "costUsdTicks": ticks,
                        },
                    }
                }
            }
        )

    (kid / "updates.jsonl").write_text(
        rec(100, 10, 0, 1, 1000) + "\n" + rec(80, 5, 40, 1, 500) + "\n",
        encoding="utf-8",
    )

    class _HB:
        _session_dir = parent
        _subagent_ordinal = {}
        _subagent_next_n = 0
        rounds = []

    hb = _HB()
    r = {
        "model_steps": [
            {
                "tools": [
                    {
                        "name": "get_command_or_subagent_output",
                        "subagent_id": kid_id,
                    }
                ]
            }
        ]
    }
    _, meta = peel_round_usage(
        {"inputTokens": 500, "modelCalls": 4, "outputTokens": 20},
        parent_dir=parent,
        child_ids=[kid_id],
    )
    attach_subagents_after_steps(r, meta, hb=hb)
    cards = r["model_steps"][0]["subagents_after"]
    assert len(cards) == 1
    assert cards[0]["label"] == "Sub Agent 1"
    assert cards[0]["tokens_out"] == 10
    assert cards[0]["tokens_in"] + cards[0]["tokens_cached"] == 100

    r2 = {
        "model_steps": [
            {
                "tools": [
                    {
                        "name": "get_command_or_subagent_output",
                        "subagent_id": kid_id,
                    }
                ]
            }
        ]
    }
    hb.rounds = [r]
    attach_subagents_after_steps(r2, meta, hb=hb)
    hb.rounds = [r, r2]
    from token_telemetry.session.subagents import relabel_subagent_cards
    relabel_subagent_cards(hb)
    c1 = r["model_steps"][0]["subagents_after"][0]
    c2 = r2["model_steps"][0]["subagents_after"][0]
    assert c1["label"] == "Sub Agent 1 R1"
    assert c2["label"] == "Sub Agent 1 R2"
    assert c2["tokens_out"] == 5
    assert c2["tokens_cached"] == 40


def test_spawn_places_sys_card_after_spawn_step():
    kid = "019ffb89-3d51-77d2-aef5-4394b3c13903"

    class _HB:
        _session_dir = None
        _subagent_ordinal = {}
        _subagent_next_n = 0
        rounds = []
        _child_round_snaps = {
            kid: [
                {
                    "system_prompt": {"tokens_in": 10108, "cost_in_usd": 0.02},
                    "usage_raw": {"inputTokens": 12000, "outputTokens": 1, "modelCalls": 1},
                }
            ]
        }

    hb = _HB()
    r = {
        "model_steps": [
            {
                "tools": [
                    {
                        "name": "spawn_subagent",
                        "subagent_id": kid,
                        "result_preview": SPAWN_TXT,
                    }
                ]
            },
            {
                "tools": [
                    {"name": "read_file"},
                ]
            },
        ]
    }
    attach_subagents_after_steps(r, {"children": []}, hb=hb)
    sys_cards = r["model_steps"][0]["subagents_after"]
    assert len(sys_cards) == 1
    assert sys_cards[0]["is_sys"] is True
    assert sys_cards[0]["label"] == "Sub Agent 1 Sys"
    assert sys_cards[0]["tokens_in"] == 10108
    assert r["model_steps"][1].get("subagents_after") == []


def test_spawn_tool_call_id_is_not_an_agent():
    """ACP call ids are not session ids — using them invented Sub Agent 3 for agent 1."""

    class _HB:
        _session_dir = None
        _subagent_ordinal = {}
        _subagent_next_n = 0
        rounds = []
        _child_round_snaps = {}

    hb = _HB()
    r = {
        "model_steps": [
            {
                "tools": [
                    {
                        "name": "spawn_subagent",
                        "tool_call_id": "call-8eda273c-0608-49fd-afe2-49e162d0cd97-69",
                    },
                    {
                        "name": "spawn_subagent",
                        "tool_call_id": "call-010dd06b-b59b-44ed-8b2b-a3f6f1543cc0-73",
                        "subagent_id": "019ffb89-3d51-77d2-aef5-4394b3c13903",
                        "result_preview": SPAWN_TXT,
                    },
                ]
            }
        ]
    }
    attach_subagents_after_steps(r, {"children": []}, hb=hb)
    after = r["model_steps"][0]["subagents_after"]
    assert len(after) == 1
    assert after[0]["is_sys"] is True
    assert after[0]["n"] == 1
    assert after[0]["session_id"] == "019ffb89-3d51-77d2-aef5-4394b3c13903"
    assert after[0]["label"] == "Sub Agent 1 Sys"
    assert hb._subagent_next_n == 1


def test_get_command_preview_is_not_a_spawn_sys():
    """Wait results often contain subagent_id lines — must not paint Sys."""
    kid = "019ffb89-3d51-77d2-aef5-4394b3c13903"

    class _HB:
        _session_dir = None
        _subagent_ordinal = {kid: 1}
        _subagent_next_n = 1
        rounds = []
        _child_round_snaps = {
            kid: [{"system_prompt": {"tokens_in": 5000, "cost_in_usd": 0.01}}],
        }

    hb = _HB()
    r = {
        "model_steps": [
            {
                "tools": [
                    {
                        "name": "get_command_or_subagent_output",
                        "subagent_id": kid,
                        "result_preview": SPAWN_TXT,
                    }
                ]
            }
        ]
    }
    attach_subagents_after_steps(
        r, {"children": [{"session_id": kid, "title": "Wave"}]}, hb=hb
    )
    after = r["model_steps"][0]["subagents_after"]
    assert after
    assert after[0].get("is_sys") is not True
    assert after[0]["n"] == 1
    assert hb._subagent_next_n == 1


def test_resume_spawn_does_not_emit_sys_or_new_n():
    old = "019ffb89-3d51-77d2-aef5-4394b3c13903"
    new = "01a01fae-9094-71e1-a2ef-6aef88b2810e"

    class _HB:
        _session_dir = None
        _subagent_ordinal = {old: 1}
        _subagent_next_n = 1
        rounds = []
        _child_round_snaps = {
            old: [{"system_prompt": {"tokens_in": 100, "cost_in_usd": 0.01}}],
            new: [{"system_prompt": {"tokens_in": 90, "cost_in_usd": 0.01}}],
        }

    hb = _HB()
    r1 = {
        "model_steps": [
            {
                "tools": [
                    {
                        "name": "spawn_subagent",
                        "subagent_id": old,
                        "result_preview": SPAWN_TXT,
                    }
                ]
            }
        ]
    }
    attach_subagents_after_steps(r1, {"children": []}, hb=hb)
    assert r1["model_steps"][0]["subagents_after"][0]["is_sys"] is True
    assert r1["model_steps"][0]["subagents_after"][0]["tokens_in"] == 100
    hb.rounds = [r1]
    r2 = {
        "model_steps": [
            {
                "tools": [
                    {
                        "name": "spawn_subagent",
                        "subagent_id": new,
                        "resume_from": old,
                        "result_preview": "Subagent started.\nsubagent_id: " + new,
                    }
                ]
            }
        ]
    }
    attach_subagents_after_steps(r2, {"children": []}, hb=hb)
    sys2 = r2["model_steps"][0]["subagents_after"]
    assert sys2 == []
    assert hb._subagent_ordinal[old] == 1
    assert hb._subagent_next_n == 1
    assert hb._subagent_ordinal.get(new) is None


def test_two_spawns_same_step_are_sys_1_and_2():
    a = "01a01f99-b56e-7363-80e2-766701f6c487"
    b = "01a01f99-b56e-7363-80e2-767028a0dc91"

    class _HB:
        _session_dir = None
        _subagent_ordinal = {}
        _subagent_next_n = 0
        rounds = []
        _child_round_snaps = {
            a: [{"system_prompt": {"tokens_in": 12500, "cost_in_usd": 0.02}}],
            b: [{"system_prompt": {"tokens_in": 10400, "cost_in_usd": 0.02}}],
        }

    hb = _HB()
    r = {
        "model_steps": [
            {
                "tools": [
                    {
                        "name": "spawn_subagent",
                        "subagent_id": a,
                        "result_preview": "Subagent started in background.\nsubagent_id: 01a01f99-b56e-…",
                    },
                    {
                        "name": "spawn_subagent",
                        "subagent_id": b,
                        "result_preview": "Subagent started in background.\nsubagent_id: 01a01f99-b56e-…",
                    },
                ]
            }
        ]
    }
    attach_subagents_after_steps(r, {"children": []}, hb=hb)
    after = r["model_steps"][0]["subagents_after"]
    assert [sa["label"] for sa in after] == ["Sub Agent 1 Sys", "Sub Agent 2 Sys"]
    assert after[0]["tokens_in"] == 12500
    assert after[1]["tokens_in"] == 10400
    assert after[0]["session_id"] == a
    assert after[1]["session_id"] == b
    assert after[0]["is_sys"] is True


def test_history_stamp_on_clipped_preview_places_sys():
    """Live spawn preview truncates the UUID; chat_history stamp restores it."""
    from token_telemetry.session.subagents import apply_history_subagent_ids

    kid = "01a01f99-b56e-7363-80e2-766701f6c487"

    class _HB:
        _session_dir = None
        _subagent_ordinal = {}
        _subagent_next_n = 0
        rounds = []
        _child_round_snaps = {
            kid: [{"system_prompt": {"tokens_in": 12500, "cost_in_usd": 0.02}}],
        }

    hb = _HB()
    tool = {
        "name": "spawn_subagent",
        "result_preview": "Subagent started in background.\nsubagent_id: 01a01f99-b56e-…",
    }
    apply_history_subagent_ids(
        tool,
        {"subagent_id": kid, "subagent_ids": [kid], "preview": tool["result_preview"]},
    )
    r = {"model_steps": [{"tools": [tool]}]}
    attach_subagents_after_steps(r, {"children": []}, hb=hb)
    after = r["model_steps"][0]["subagents_after"]
    assert len(after) == 1
    assert after[0]["label"] == "Sub Agent 1 Sys"
    assert after[0]["tokens_in"] == 12500
    assert after[0]["session_id"] == kid


def test_sys_in_copies_system_box_total_not_tokens_in_field():
    """Sys In = System header tot (sum of parts), not a separate tokens_in."""
    from token_telemetry.session.subagents import system_box_total

    kid = "01a01f99-b56e-7363-80e2-766701f6c487"
    sp = {
        "kind": "system_prompt",
        "tokens_in": 99999,
        "logical_tokens": 99999,
        "message_residual_tokens": 7520,
        "cost_in_usd": 0.017,
        "estimate_usd": 0.017,
        "parts": [
            {"kind": "system", "tokens": 965},
            {"kind": "mcp", "tokens": 94},
            {"kind": "tool_defs_message", "tokens": 7520},
        ],
    }
    assert system_box_total(sp) == 8579

    class _HB:
        _session_dir = None
        _subagent_ordinal = {}
        _subagent_next_n = 0
        rounds = []
        _child_sys = {kid: {"tokens_in": 1000, "cost_in_usd": 0.002}}
        _child_round_snaps = {kid: [{"system_prompt": sp}]}

    hb = _HB()
    r = {
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
    attach_subagents_after_steps(r, {"children": []}, hb=hb)
    after = r["model_steps"][0]["subagents_after"]
    assert after[0]["is_sys"] is True
    assert after[0]["tokens_in"] == 8579
    assert after[0]["tokens_in"] != 99999
    assert after[0]["tokens_in"] != 1000


def test_capture_child_sys_refreshes_stale_partial():
    from token_telemetry.session.subagents import capture_child_sys

    root = "01a01f99-b56e-7363-80e2-766701f6c487"

    class _HB:
        _child_sys = {root: {"tokens_in": 1059, "cost_in_usd": 0.002}}

    hb = _HB()
    capture_child_sys(
        hb,
        root,
        [
            {
                "system_prompt": {
                    "kind": "system_prompt",
                    "tokens_in": 8579,
                    "parts": [
                        {"kind": "system", "tokens": 1059},
                        {"kind": "tool_defs_message", "tokens": 7520},
                    ],
                    "message_residual_tokens": 7520,
                    "cost_in_usd": 0.017,
                }
            }
        ],
    )
    assert hb._child_sys[root]["tokens_in"] == 8579


def test_clipped_spawn_preview_is_not_an_id():
    """80-char preview truncates the UUID — must not invent a session from it."""
    from token_telemetry.session.subagents import spawn_session_ids

    preview = "Subagent started in background.\nsubagent_id: 01a01f99-b56e-…"
    assert spawn_session_ids({"name": "spawn_subagent", "result_preview": preview}) == []
    assert spawn_session_ids(
        {
            "name": "spawn_subagent",
            "result_preview": preview,
            "subagent_id": "01a01f99-b56e-7363-80e2-766701f6c487",
        }
    ) == ["01a01f99-b56e-7363-80e2-766701f6c487"]


def test_parent_plus_each_tab_estimate():
    from token_telemetry.session.monitor import _sum_child_tab_estimates

    parent = 23.182
    tabs = [
        {"estimate_usd": 1.237},
        {"estimate_usd": 3.158},
        {"estimate_usd": 1.404},
        {"estimate_usd": 2.419},
        {"estimate_usd": 4.758},
        {"estimate_usd": 2.836},
        {"estimate_usd": 2.240},
        {"estimate_usd": 2.708},
        {"estimate_usd": 1.212},
        {"estimate_usd": 1.758},
        {"estimate_usd": 2.654},
        {"estimate_usd": 1.278},
    ]
    kids = _sum_child_tab_estimates(tabs)
    assert abs(kids - 27.662) < 1e-6
    assert abs(parent + kids - 50.844) < 1e-6
