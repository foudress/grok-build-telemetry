"""Codex rollout reader stays off the Grok ledgers and sums call deltas."""

from __future__ import annotations

import json
import time
from pathlib import Path

from token_telemetry.codex.store import (
    build_snapshot,
    clear_cache,
    snapshot_bytes,
    thread_detail,
)


def _write(path: Path, events: list[dict]) -> None:
    lines = []
    for i, event in enumerate(events, start=1):
        row = {
            "timestamp": event.get("timestamp") or f"2026-10-20T01:00:{i:02d}Z",
            "ordinal": i,
            "type": event["type"],
            "payload": event["payload"],
        }
        lines.append(json.dumps(row))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _usage(total: int, cached: int = 0, out: int = 0, reason: int = 0) -> dict:
    return {
        "input_tokens": total - out,
        "cached_input_tokens": cached,
        "cache_write_input_tokens": 0,
        "output_tokens": out,
        "reasoning_output_tokens": reason,
        "total_tokens": total,
    }


def test_sums_call_usage_not_cumulative_turn(tmp_path: Path):
    home = tmp_path / "codex"
    _write(
        home / "sessions" / "2026" / "10" / "20" / "rollout-2026-10-20T01-00-00-aaaaaaaa-aaaa-7aaa-8aaa-aaaaaaaaaaaa.jsonl",
        [
            {
                "type": "session_meta",
                "payload": {
                    "id": "thread-a",
                    "timestamp": "2026-10-20T01:00:00Z",
                    "cwd": str(tmp_path / "proj"),
                    "originator": "Codex Desktop",
                    "cli_version": "0.163.0",
                    "source": "vscode",
                },
            },
            {
                "type": "turn_context",
                "payload": {"turn_id": "turn-1", "model": "gpt-6-luna", "effort": "high"},
            },
            {
                "type": "token_usage_record",
                "payload": {
                    "turn_id": "turn-1",
                    "response_id": "r1",
                    "usage": _usage(10, cached=4, out=1, reason=1),
                    "turn_token_usage": _usage(10),
                    "thread_token_usage": _usage(10),
                },
            },
            {
                "type": "token_usage_record",
                "payload": {
                    "turn_id": "turn-1",
                    "response_id": "r2",
                    "usage": _usage(15, cached=8, out=2),
                    "turn_token_usage": _usage(999),
                    "thread_token_usage": _usage(9999),
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {
                        "model_context_window": 258400,
                        "last_token_usage": _usage(5000),
                        "total_token_usage": _usage(5000),
                    },
                },
            },
        ],
    )
    (home / "session_index.jsonl").write_text(
        json.dumps({"id": "thread-a", "thread_name": "Named thread", "updated_at": "2026-10-20T02:00:00Z"})
        + "\n",
        encoding="utf-8",
    )
    snap = build_snapshot(home)
    assert snap["totals"]["total_tokens"] == 25
    assert snap["totals"]["cached_input_tokens"] == 12
    assert snap["totals"]["uncached_input_tokens"] == (9 + 13) - 12
    assert snap["n_calls"] == 2
    thread = snap["threads"][0]
    assert thread["title"] == "Named thread"
    assert thread["model"] == "gpt-6-luna"
    assert thread["effort"] == "high"
    assert thread["project"] == "proj"
    assert thread["context_window"] == 258400
    assert thread["is_root"] is True
    assert snap["by_model"][0]["model"] == "gpt-6-luna"
    assert snap["by_day"][0]["day"] == "2026-10-20"


def test_token_count_fallback_and_child_rollup(tmp_path: Path):
    home = tmp_path / "codex"
    day = home / "sessions" / "2026" / "10" / "21"
    _write(
        day / "rollout-parent.jsonl",
        [
            {
                "type": "session_meta",
                "payload": {"id": "parent", "cwd": "C:/work/app", "timestamp": "2026-10-21T00:00:00Z"},
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "token_count",
                    "info": {"last_token_usage": _usage(40, cached=10, out=5), "model_context_window": 475000},
                },
            },
        ],
    )
    _write(
        day / "rollout-child.jsonl",
        [
            {
                "type": "session_meta",
                "payload": {
                    "id": "child",
                    "parent_thread_id": "parent",
                    "source": {"subagent": {"thread_spawn": {"agent_nickname": "Dirac"}}},
                    "timestamp": "2026-10-21T00:00:02Z",
                },
            },
            {
                "type": "turn_context",
                "payload": {"turn_id": "t", "model": "codex-auto-review", "effort": "low"},
            },
            {
                "type": "token_usage_record",
                "payload": {
                    "turn_id": "t",
                    "response_id": "c1",
                    "usage": _usage(7, out=1),
                },
            },
        ],
    )
    snap = build_snapshot(home)
    by_id = {t["id"]: t for t in snap["threads"]}
    assert by_id["parent"]["usage"]["total_tokens"] == 40
    assert by_id["parent"]["usage_with_children"]["total_tokens"] == 47
    assert by_id["parent"]["n_calls_with_children"] == 2
    assert by_id["parent"]["child_ids"] == ["child"]
    assert by_id["child"]["is_root"] is False
    assert by_id["child"]["source"] == "subagent:Dirac"
    assert snap["totals"]["total_tokens"] == 47


def test_duplicate_response_id_counted_once(tmp_path: Path):
    home = tmp_path / "codex"
    events = [
        {"type": "session_meta", "payload": {"id": "same"}},
        {
            "type": "token_usage_record",
            "payload": {"turn_id": "t", "response_id": "dup", "usage": _usage(11, out=1)},
        },
    ]
    _write(home / "sessions" / "a.jsonl", events)
    _write(home / "sessions" / "b.jsonl", events)
    snap = build_snapshot(home)
    assert snap["n_calls"] == 1
    assert snap["totals"]["total_tokens"] == 11
    assert len(snap["threads"]) == 1


def test_missing_home(tmp_path: Path):
    snap = build_snapshot(tmp_path / "nope")
    assert snap["ok"] is True
    assert snap["threads"] == []
    assert snap["totals"]["total_tokens"] == 0
    assert snap["note"] == "Codex home not found"


def test_snapshot_cache_etag_changes_when_file_changes(tmp_path: Path):
    home = tmp_path / "codex"
    path = home / "sessions" / "rollout.jsonl"
    _write(
        path,
        [
            {"type": "session_meta", "payload": {"id": "t"}},
            {
                "type": "token_usage_record",
                "payload": {"response_id": "a", "usage": _usage(3)},
            },
        ],
    )
    clear_cache()
    body1, etag1 = snapshot_bytes(home)
    body2, etag2 = snapshot_bytes(home)
    assert body1 == body2
    assert etag1 == etag2
    time.sleep(0.05)
    _write(
        path,
        [
            {"type": "session_meta", "payload": {"id": "t"}},
            {
                "type": "token_usage_record",
                "payload": {"response_id": "b", "usage": _usage(9)},
            },
        ],
    )
    body3, etag3 = snapshot_bytes(home)
    assert etag3 != etag1
    data = json.loads(body3)
    assert data["totals"]["total_tokens"] == 9
    assert "calls" not in data["threads"][0]
    detail = thread_detail("t", home)
    assert detail["ok"] is True
    assert detail["calls"][0]["total_tokens"] == 9
    assert thread_detail("missing", home)["ok"] is False


def test_tool_labels_attach_to_the_call_that_requested_them(tmp_path: Path):
    home = tmp_path / "codex"
    _write(
        home / "sessions" / "2026" / "10" / "22" / "rollout-2026-10-22T01-00-00-cccccccc-cccc-7ccc-8ccc-cccccccccccc.jsonl",
        [
            {"type": "session_meta", "payload": {"id": "thread-c", "timestamp": "2026-10-22T01:00:00Z"}},
            {"type": "turn_context", "payload": {"turn_id": "turn-9", "model": "gpt-6-luna", "effort": "low"}},
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "turn_id": "turn-9",
                    "item": {"type": "UserMessage", "content": [{"type": "text", "text": "fix the build"}]},
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "reasoning",
                    "summary": [],
                    "internal_chat_message_metadata_passthrough": {"turn_id": "turn-9"},
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call",
                    "name": "exec",
                    "call_id": "call-1",
                    "input": "pytest test",
                    "internal_chat_message_metadata_passthrough": {"turn_id": "turn-9"},
                },
            },
            {
                "type": "token_usage_record",
                "payload": {
                    "turn_id": "turn-9",
                    "response_id": "resp-1",
                    "usage": _usage(20, cached=5, out=4, reason=1),
                    "turn_token_usage": _usage(999),
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "turn_id": "turn-9",
                    "item": {
                        "type": "CommandExecution",
                        "command": ["pytest", "test"],
                        "cwd": "/work/repo",
                        "status": "completed",
                    },
                },
            },
            {
                "type": "response_item",
                "payload": {
                    "type": "custom_tool_call_output",
                    "call_id": "call-1",
                    "output": "should not duplicate the command row",
                },
            },
        ],
    )
    snap = build_snapshot(home)
    assert snap["totals"]["total_tokens"] == 20
    call = snap["threads"][0]["calls"][0]
    assert call["output_tokens"] == 4
    assert call["user_preview"] == "fix the build"
    kinds = [row["kind"] for row in call["detail"]]
    assert kinds == ["reasoning", "tool_request"]
    assert call["detail"][1]["name"] == "run_terminal_command"
    assert call["detail"][1]["command"] == "pytest test"
    assert len(call["harness"]) == 1
    assert call["harness"][0]["name"] == "run_terminal_command"
    assert call["harness"][0]["command"] == "pytest test"
    assert "duplicate" not in call["harness"][0]["preview"]


def test_exec_script_splits_into_grep_and_shell(tmp_path: Path):
    home = tmp_path / "codex"
    script = (
        'text(await tools.exec_command({cmd:"rg -n token src"})); '
        'text(await tools.exec_command({cmd:"pytest test"}));'
    )
    _write(
        home / "sessions" / "2026" / "10" / "23" / "rollout-2026-10-23T01-00-00-dddddddd-dddd-7ddd-8ddd-dddddddddddd.jsonl",
        [
            {"type": "session_meta", "payload": {"id": "thread-d"}},
            {
                "type": "response_item",
                "payload": {"type": "custom_tool_call", "name": "exec", "call_id": "c", "input": script},
            },
            {
                "type": "token_usage_record",
                "payload": {"turn_id": "t", "response_id": "r", "usage": _usage(8, out=2)},
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {"type": "ContextCompaction", "id": "cmp"},
                },
            },
        ],
    )
    call = build_snapshot(home)["threads"][0]["calls"][0]
    assert [row["name"] for row in call["detail"]] == ["grep", "run_terminal_command"]
    assert call["detail"][0]["command"] == "rg -n token src"
    assert call["compact_after"] is True


def test_read_file_row_keeps_the_file_only(tmp_path: Path):
    home = tmp_path / "codex"
    script = 'text(await tools.exec_command({cmd:"Get-Content src/app.py"}));'
    _write(
        home / "sessions" / "2026" / "10" / "23" / "rollout-2026-10-23T02-00-00-eeeeeeee-eeee-7eee-8eee-eeeeeeeeeeee.jsonl",
        [
            {"type": "session_meta", "payload": {"id": "thread-e"}},
            {
                "type": "response_item",
                "payload": {"type": "custom_tool_call", "name": "exec", "call_id": "c", "input": script},
            },
            {
                "type": "token_usage_record",
                "payload": {"turn_id": "t", "response_id": "r", "usage": _usage(8, out=2)},
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "turn_id": "t",
                    "item": {
                        "type": "CommandExecution",
                        "command": ["Get-Content", "src/app.py"],
                        "cwd": "/work/repo",
                    },
                },
            },
        ],
    )
    call = build_snapshot(home)["threads"][0]["calls"][0]
    assert call["detail"][0]["name"] == "read_file"
    assert call["detail"][0]["path"] == "src/app.py"
    assert call["detail"][0]["command"] == ""
    assert call["harness"][0]["name"] == "read_file"
    assert call["harness"][0]["path"] == "src/app.py"
    assert call["harness"][0]["command"] == ""


def test_snapshot_progress_counts_each_file(tmp_path: Path):
    home = tmp_path / "codex"
    day = home / "sessions" / "2026" / "10" / "23"
    _write(
        day / "rollout-a.jsonl",
        [{"type": "session_meta", "payload": {"id": "thread-a"}}],
    )
    _write(
        day / "rollout-b.jsonl",
        [{"type": "session_meta", "payload": {"id": "thread-b"}}],
    )
    seen = []
    build_snapshot(home, on_progress=lambda done, total, cold: seen.append((done, total, cold)))
    assert seen[0] == (0, 2, 0)
    assert seen[-1] == (2, 2, 1)
    again = []
    build_snapshot(home, on_progress=lambda done, total, cold: again.append((done, total, cold)))
    assert again[0] == (0, 2, 0)
    assert all(cold == 0 for _done, _total, cold in again)


def test_web_search_and_new_file_use_grok_tool_names(tmp_path: Path):
    home = tmp_path / "codex"
    _write(
        home / "sessions" / "2026" / "10" / "24" / "rollout-2026-10-24T01-00-00-eeeeeeee-eeee-7eee-8eee-eeeeeeeeeeee.jsonl",
        [
            {"type": "session_meta", "payload": {"id": "thread-e"}},
            {
                "type": "token_usage_record",
                "payload": {"turn_id": "t", "response_id": "r", "usage": _usage(6, out=1)},
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "Extension",
                        "kind": "web.search",
                        "query": "token telemetry",
                        "action": {"url": "https://example.test/docs"},
                    },
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "item_completed",
                    "item": {
                        "type": "FileChange",
                        "changes": {"src/new.py": {"type": "add", "content": "print(1)\n"}},
                    },
                },
            },
        ],
    )
    harness = build_snapshot(home)["threads"][0]["calls"][0]["harness"]
    assert [row["name"] for row in harness] == ["web_search", "write"]
    assert harness[0]["preview"] == "token telemetry"
    assert harness[1]["path"].endswith("new.py")
    assert int(harness[1]["chars"]) == len("print(1)\n")


def test_zst_rollout_is_read(tmp_path: Path):
    import zstandard

    home = tmp_path / "codex"
    events = [
        {"type": "session_meta", "payload": {"id": "z"}},
        {
            "type": "token_usage_record",
            "payload": {"response_id": "z1", "usage": _usage(6, cached=2, out=1)},
        },
    ]
    raw = []
    for i, event in enumerate(events, start=1):
        raw.append(
            json.dumps(
                {
                    "timestamp": f"2026-10-19T00:00:0{i}Z",
                    "ordinal": i,
                    "type": event["type"],
                    "payload": event["payload"],
                }
            )
        )
    blob = ("\n".join(raw) + "\n").encode("utf-8")
    dest = home / "sessions" / "2026" / "10" / "19" / "rollout.jsonl.zst"
    dest.parent.mkdir(parents=True)
    dest.write_bytes(zstandard.ZstdCompressor().compress(blob))
    snap = build_snapshot(home)
    assert snap["files"]["zst"] == 1
    assert snap["files"]["zst_skipped"] == 0
    assert snap["totals"]["total_tokens"] == 6
