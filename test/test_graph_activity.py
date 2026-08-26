"""Parse updates.jsonl tool events into ActivityEvent dicts."""

from __future__ import annotations

from pathlib import Path

from token_telemetry.graph.activity import parse_updates
from token_telemetry.graph.replay import collect_replay

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "graph_updates.jsonl"
CWD = r"C:\work\repo" if Path("C:\\").exists() else "/work/repo"


def test_maps_relative_target_file_with_cwd():
    events = parse_updates(FIXTURE, cwd=CWD, session_id="sess-1", agent_name="explore")
    reads = [e for e in events if e["kind"] == "read"]
    assert len(reads) == 1
    assert reads[0]["tool"] == "read_file"
    assert reads[0]["path"] == "src/foo.py"
    assert reads[0]["sid"] == "sess-1"
    assert reads[0]["name"] == "explore"


def test_kinds_read_write_search_list():
    events = parse_updates(FIXTURE, cwd=CWD, session_id="s")
    by_kind = {e["kind"]: e for e in events}
    assert set(by_kind) >= {"read", "write", "search", "list"}
    assert by_kind["write"]["tool"] == "search_replace"
    assert by_kind["write"]["path"] == "src/bar.py"
    assert by_kind["search"]["tool"] == "grep"
    assert by_kind["search"]["path"] == "src"
    assert by_kind["list"]["tool"] == "list_dir"
    assert by_kind["list"]["path"] == "src"


def test_ignores_web_search():
    events = parse_updates(FIXTURE, cwd=CWD, session_id="s")
    assert all(e["tool"] != "web_search" for e in events)
    assert all(e["kind"] != "web" for e in events)
    assert any(e["kind"] == "exec" and e["tool"] == "run_terminal_command" for e in events)


def test_replay_sorts_by_t():
    events = collect_replay(CWD, [FIXTURE])
    ts = [e["t"] for e in events]
    assert ts == sorted(ts)
    assert ts == [50.0, 75.0, 100.0, 150.0, 200.0]
    assert [e["kind"] for e in events] == ["search", "list", "read", "exec", "write"]
