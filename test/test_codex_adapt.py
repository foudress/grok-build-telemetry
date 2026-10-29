"""Codex snapshot → the same dashboard wire Grok already renders."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from token_telemetry.codex.adapt import build_period, sessions_for_ui, state_for
from token_telemetry.codex.prices import price_usage
from token_telemetry.codex.api import CodexFeed
from token_telemetry.codex.store import focus_snapshot
from token_telemetry.session.windows import period_window


def _call(t: str, turn: str, unc: int, cached: int, out: int, reason: int) -> dict:
    return {
        "t": t,
        "turn_id": turn,
        "model": "gpt-5-codex",
        "effort": "medium",
        "input_tokens": unc + cached,
        "uncached_input_tokens": unc,
        "cached_input_tokens": cached,
        "output_tokens": out,
        "reasoning_output_tokens": reason,
    }


def _snap(parent_times: tuple[str, str, str], child_time: str) -> dict:
    t1, t2, t3 = parent_times
    return {
        "threads": [
            {
                "id": "p",
                "title": "parent",
                "cwd": "/work",
                "source": "cli",
                "parent_id": "",
                "is_root": True,
                "started": t1,
                "updated": t3,
                "context_window": 258400,
                "model": "gpt-5-codex",
                "calls": [
                    _call(t1, "t1", 10, 40, 5, 2),
                    _call(t2, "t1", 8, 50, 3, 1),
                    _call(t3, "t2", 4, 60, 2, 0),
                ],
            },
            {
                "id": "c",
                "title": "child",
                "cwd": "/work",
                "source": "cli",
                "parent_id": "p",
                "is_root": False,
                "started": child_time,
                "updated": child_time,
                "context_window": 258400,
                "model": "gpt-5-codex",
                "calls": [_call(child_time, "tc", 7, 0, 1, 0)],
            },
        ]
    }


def _inside_today() -> tuple[str, str, str, str]:
    now = datetime(2026, 10, 21, 20, tzinfo=timezone.utc)
    start, end, _label = period_window("daily", 0, now=now)
    mid = start + (end - start) / 2
    stamps = [
        (mid + timedelta(seconds=i)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        for i in (0, 30, 600, 900)
    ]
    return stamps[0], stamps[1], stamps[2], stamps[3]


def test_state_matches_dashboard_rounds() -> None:
    t1, t2, t3, tc = _inside_today()
    snap = _snap((t1, t2, t3), tc)
    state = state_for(snap, "p", pinned="p")
    assert state["watching"] is True
    assert state["ledger_source"] == "codex"
    assert state["session_id"] == "p"
    rounds = state["rounds"]
    assert len(rounds) == 2
    assert rounds[0]["breakdown"]["tree_in_tokens"] == 18
    assert rounds[0]["breakdown"]["cached_tokens"] == 90
    assert rounds[0]["breakdown"]["llm_reasoning_tokens"] == 3
    assert rounds[0]["breakdown"]["llm_out_to_user_tokens"] == 5
    assert rounds[0]["breakdown"]["tree_in_usd"] == 0
    assert len(rounds[0]["model_steps"]) == 2
    assert rounds[0]["model_steps"][0]["context_end"] == 50
    assert rounds[0]["context_end"] == 58
    assert rounds[0]["user_prompt"]["tokens_cached"] == 40
    assert rounds[0]["model_steps"][0]["tokens_cached"] == 50
    assert rounds[0]["model_steps"][1]["tokens_cached"] == 0
    assert state["turns"][0]["uncached_input_tokens"] == 18
    assert state["sub_sessions"][0]["turns"][0]["uncached_input_tokens"] == 7
    assert state["sub_sessions"][0]["session_kind"] == "subagent"
    assert state["live"]["context_tokens"] == 64
    assert state["signals"]["contextWindowTokens"] == 258400
    assert state["context_series"][-1]["v"] == 64
    assert [row["session_id"] for row in sessions_for_ui(snap)] == ["p"]
    focused = sessions_for_ui(snap, focus="c")
    assert focused[0]["session_id"] == "c"
    assert any(row["session_id"] == "p" for row in focused)


def test_round_tree_shows_tools_without_changing_totals() -> None:
    call = _call("2026-10-21T12:00:00Z", "t1", 10, 40, 5, 2)
    call["user_preview"] = "fix the build"
    call["detail"] = [
        {"kind": "reasoning", "preview": ""},
        {"kind": "tool_request", "name": "exec", "command": "pytest test", "preview": "pytest test", "chars": "0"},
        {"kind": "message", "preview": "done", "chars": "20"},
    ]
    call["harness"] = [
        {"kind": "tool", "name": "exec", "command": "pytest test", "path": "/work/repo", "preview": "pytest test"},
    ]
    snap = {
        "threads": [{
            "id": "p",
            "title": "parent",
            "is_root": True,
            "updated": call["t"],
            "context_window": 1000,
            "calls": [call],
        }]
    }
    state = state_for(snap, "p", pinned="p")
    step = state["rounds"][0]["model_steps"][0]
    phases = {node["kind"]: node for node in step["children"]}
    assert phases["phase_llm"]["tokens_out"] == 5
    names = [node.get("name") or node["kind"] for node in phases["phase_llm"]["children"]]
    assert names == ["reasoning", "exec", "message"]
    assert phases["phase_llm"]["children"][0]["tokens_out"] == 2
    assert phases["phase_llm"]["children"][1]["tokens_out"] == 0
    assert phases["phase_llm"]["children"][2]["tokens_out"] == 3
    assert phases["phase_harness"]["tool_count"] == 1
    assert phases["phase_harness"]["children"][0]["command"] == "pytest test"
    assert state["rounds"][0]["user_prompt"]["preview"] == "fix the build"
    assert state["rounds"][0]["breakdown"]["tree_in_tokens"] == 10


def test_tool_request_output_is_not_copied_onto_a_message() -> None:
    call = _call("2026-10-21T12:00:00Z", "t1", 10, 40, 5, 2)
    call["detail"] = [
        {"kind": "reasoning", "chars": "0"},
        {"kind": "tool_request", "name": "grep", "command": "rg token", "chars": "40"},
    ]
    snap = {"threads": [{
        "id": "p", "title": "p", "is_root": True, "updated": call["t"], "calls": [call],
    }]}
    step = state_for(snap, "p", pinned="p")["rounds"][0]["model_steps"][0]
    llm = [n for n in step["children"] if n["kind"] == "phase_llm"][0]
    kinds = [n["kind"] for n in llm["children"]]
    assert kinds == ["reasoning", "tool_request"]
    assert llm["children"][1]["tokens_out"] == 3
    assert llm["children"][1]["name"] == "grep"


def test_period_sums_each_call_once() -> None:
    t1, t2, t3, tc = _inside_today()
    snap = _snap((t1, t2, t3), tc)
    now = datetime(2026, 10, 21, 20, tzinfo=timezone.utc)
    agg = build_period(snap, period="daily", offset=0, grain="hour", now=now)
    assert agg["ledger_source"] == "codex"
    assert sum(b["tokens_in"] for b in agg["buckets"]) == 29
    assert agg["totals"]["tokens_in"] == 29
    assert agg["totals"]["tokens_cached"] == 150
    assert agg["totals"]["official_usd"] == 0
    assert agg["totals"]["parts"]
    by_id = {s["session_id"]: s for s in agg["sessions"]}
    assert by_id["p"]["tokens_in"] == 22
    assert by_id["c"]["tokens_in"] == 7
    assert by_id["c"]["depth"] == 1
    sess = build_period(snap, period="daily", offset=0, grain="session", now=now)
    labels = [b["label"] for b in sess["buckets"]]
    assert labels == ["1", "1.1"]
    assert sum(b["tokens_in"] for b in sess["buckets"]) == 29


def test_cache_miss_line_price_and_mid_turn_compact() -> None:
    first = _call("2026-10-21T12:00:00Z", "t", 100, 4900, 4, 1)
    first["model"] = "gpt-6-luna"
    first["compact_after"] = True
    first["harness"] = [
        {"kind": "tool", "name": "grep", "command": "rg foo", "chars": "30"},
        {"kind": "tool", "name": "read_file", "command": "Get-Content a.py", "chars": "10"},
    ]
    second = _call("2026-10-21T12:00:30Z", "t", 4500, 1000, 2, 0)
    second["model"] = "gpt-6-luna"
    snap = {"threads": [{
        "id": "p",
        "title": "p",
        "is_root": True,
        "updated": second["t"],
        "model": "gpt-6-luna",
        "calls": [first, second],
    }]}
    state = state_for(snap, "p", pinned="p")
    rnd = state["rounds"][0]
    bd = rnd["breakdown"]
    assert bd["tree_in_tokens"] == 4600
    assert bd["cache_miss_in_tokens"] == 4000
    assert bd["user_in_tokens"] == 100
    assert bd["harness_in_tokens"] == 500
    step = rnd["model_steps"][0]
    assert step["compacts_after"][0]["placement"] == "mid_round"
    assert step["compacts_after"][0]["tokens_before"] == 5000
    assert "compact_before" not in rnd
    harness = [n for n in step["children"] if n["kind"] == "phase_harness"][0]
    assert [c.get("kind") if c.get("kind") == "llm_to_in" else c["name"] for c in harness["children"]] == [
        "llm_to_in", "grep", "read_file",
    ]
    # Out 4 is the billed Out. The other 496 of the prompt growth stay on the tools.
    assert [c["tokens_in"] for c in harness["children"]] == [4, 372, 124]
    assert harness["children"][1]["cost_in_usd"] > 0
    assert bd["llm_out_to_harness_in_tokens"] == 4
    assert harness["tokens_in"] == 500
    # Those 500 are the next prompt's new uncached, shown on the call that ran the tools.
    assert step["tokens_in"] == 500
    paid = rnd["model_steps"][1]
    assert paid["tokens_in"] == 0
    assert paid["cached_read_tokens"] == 0
    assert step["cached_read_tokens"] == 1000
    assert rnd["user_prompt"]["tokens_cached"] == 4900
    assert paid["cache_miss_in_tokens"] == 4000
    assert step["context_plot_tokens"] == 5000
    assert paid["context_plot_tokens"] == 5500
    assert rnd["context_start"] + rnd["user_prompt"]["tokens_in"] == 5000
    luna = price_usage("gpt-6-luna", uncached=100_000, cached=100_000, output=100_000, prompt=200_000)
    assert abs(luna["in"] - 0.01) < 1e-12
    assert abs(luna["cached"] - 0.001) < 1e-12
    assert abs(luna["out"] - 0.05) < 1e-12
    long = price_usage("gpt-6-luna", uncached=300_000, cached=0, output=0, prompt=300_000)
    assert abs(long["in"] - 0.06) < 1e-9
    assert rnd["estimate_usd"] > 0
    assert state["totals"]["official_usd"] == 0


def test_llm_out_is_the_billed_output_and_tools_keep_the_growth() -> None:
    """Paid uncached can be smaller than the Out. Tools still get the prompt growth."""
    first = _call("2026-10-21T12:00:00Z", "t", 200, 800, 400, 50)
    first["model"] = "gpt-6-luna"
    first["harness"] = [
        {"kind": "tool", "name": "grep", "command": "rg foo", "chars": "30"},
        {"kind": "tool", "name": "read_file", "command": "Get-Content a.py", "chars": "10"},
    ]
    # Next prompt grew by 600. Only 200 of that was uncached.
    second = _call("2026-10-21T12:00:30Z", "t", 200, 1400, 10, 0)
    second["model"] = "gpt-6-luna"
    snap = {"threads": [{
        "id": "p", "title": "p", "is_root": True, "updated": second["t"], "calls": [first, second],
    }]}
    step = state_for(snap, "p", pinned="p")["rounds"][0]["model_steps"][0]
    harness = [n for n in step["children"] if n["kind"] == "phase_harness"][0]
    assert [c["tokens_in"] for c in harness["children"]] == [400, 150, 50]
    assert harness["tokens_in"] == 600
    assert step["tokens_in"] == 600


def test_parent_total_includes_subagents() -> None:
    parent = _call("2026-10-21T12:00:00Z", "t", 1_000_000, 0, 0, 0)
    parent["model"] = "gpt-6-luna"
    child = _call("2026-10-21T12:00:10Z", "c", 1_000_000, 0, 0, 0)
    child["model"] = "gpt-5.6-luna"
    snap = {"threads": [
        {"id": "p", "title": "p", "is_root": True, "updated": parent["t"], "calls": [parent]},
        {
            "id": "c", "title": "child", "parent_id": "p", "is_root": False,
            "updated": child["t"], "calls": [child],
        },
    ]}
    state = state_for(snap, "p", pinned="p")
    parent_usd = state["totals"]["parent_estimate_usd"]
    child_usd = state["totals"]["children_estimate_usd"]
    assert parent_usd > 0 and child_usd > 0
    assert state["totals"]["estimate_usd"] == parent_usd + child_usd
    assert state["sub_sessions"][0]["estimate_usd"] == child_usd


def test_auto_review_is_priced_as_luna_56() -> None:
    bill = price_usage(
        "codex-auto-review", uncached=100_000, cached=0, output=0, prompt=100_000
    )
    assert bill["priced"] is True
    assert bill["model"] == "gpt-5.6-luna"
    assert abs(bill["in"] - 0.02) < 1e-12


def test_empty_and_unknown_thread() -> None:
    empty = state_for({"threads": []}, None, pinned=None)
    assert empty["watching"] is False
    assert empty["rounds"] == []
    missing = state_for({"threads": [{"id": "p", "calls": []}]}, "nope", pinned="nope")
    assert missing["watching"] is False


def test_focus_snapshot_reads_the_newest_day_only(tmp_path: Path) -> None:
    old = tmp_path / "sessions" / "2020" / "01" / "01"
    new = tmp_path / "sessions" / "2026" / "10" / "29"
    old.mkdir(parents=True)
    new.mkdir(parents=True)
    meta = '{"type":"session_meta","payload":{"id":"%s","cwd":"/work","timestamp":"2026-10-29T00:00:00Z"}}\n'
    old_path = old / "rollout-old.jsonl"
    new_path = new / "rollout-new.jsonl"
    old_path.write_text(meta % "old-thread", encoding="utf-8")
    new_path.write_text(meta % "new-thread", encoding="utf-8")
    os_utime = __import__("os").utime
    os_utime(old_path, (1_700_000_000, 1_700_000_000))
    os_utime(new_path, (1_800_000_000, 1_800_000_000))
    snap = focus_snapshot(tmp_path)
    ids = {t["id"] for t in snap["threads"]}
    assert "new-thread" in ids
    assert "old-thread" not in ids
    feed = CodexFeed()
    body, _etag = feed.state_bytes(tmp_path)
    data = json.loads(body)
    assert data["session_id"] == "new-thread"
    assert data.get("pending") is not True


def test_state_bytes_does_not_scan_history(tmp_path: Path) -> None:
    feed = CodexFeed()
    body, _etag = feed.state_bytes(tmp_path)
    data = json.loads(body)
    assert data["rounds"] == []
    assert data["source"] == "codex"


def test_child_rollout_does_not_hide_its_parent(tmp_path: Path) -> None:
    parent_day = tmp_path / "sessions" / "2026" / "10" / "28"
    child_day = tmp_path / "sessions" / "2026" / "10" / "29"
    parent_day.mkdir(parents=True)
    child_day.mkdir(parents=True)
    parent = parent_day / "rollout-parent.jsonl"
    child = child_day / "rollout-child.jsonl"
    parent.write_text(
        '{"type":"session_meta","payload":{"id":"parent-thread","cwd":"/work",'
        '"timestamp":"2026-10-28T00:00:00Z"}}\n',
        encoding="utf-8",
    )
    child.write_text(
        '{"type":"session_meta","payload":{"id":"child-thread","cwd":"/work",'
        '"parent_thread_id":"parent-thread","timestamp":"2026-10-29T12:00:00Z"}}\n',
        encoding="utf-8",
    )
    os_utime = __import__("os").utime
    os_utime(parent, (1_700_000_000, 1_700_000_000))
    os_utime(child, (1_800_000_000, 1_800_000_000))
    feed = CodexFeed()
    data = json.loads(feed.state_bytes(tmp_path)[0])
    assert data["session_id"] == "parent-thread"
    assert [row["session_id"] for row in data["sessions"]] == ["parent-thread"]
    assert [row["session_id"] for row in data["sub_sessions"]] == ["child-thread"]


def test_feed_pin_does_not_touch_real_home(tmp_path: Path) -> None:
    home = tmp_path / "missing-codex"
    feed = CodexFeed()
    unknown = feed.select("nope", home=home)
    assert unknown["ok"] is False
    follow = feed.select(None, home=home)
    assert follow["ok"] is True
    assert follow["session_id"] is None
    assert follow["pinned"] is False
    health = feed.health(home=home)
    assert health["source"] == "codex"
    assert health["ok"] is True
