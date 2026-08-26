"""Graph HTTP payload helpers (monkeypatched sessions, no live server)."""

from __future__ import annotations

import shutil
from pathlib import Path

from token_telemetry.graph.api import (
    ACTIVITY_CAP,
    REPLAY_CAP,
    activity_payload,
    graph_payload,
    projects_payload,
    replay_payload,
    rescan_payload,
    sessions_payload,
)
from token_telemetry.graph.projects import normalize_cwd
from token_telemetry.server.http import DASHBOARD_DIR, GRAPH_HTML

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "graph_updates.jsonl"


def _row(session_dir: Path, cwd, epoch: float = 1_000.0, **extra):
    return {
        "session_id": extra.pop("session_id", session_dir.name),
        "label": session_dir.name,
        "title": extra.pop("title", session_dir.name),
        "cwd": str(cwd),
        "session_kind": extra.pop("session_kind", None),
        "agent_name": extra.pop("agent_name", None),
        "last_active_epoch": epoch,
        "age_label": "1m",
        "path": str(session_dir),
        **extra,
    }


def _session_dir(tmp_path: Path, name: str, src: Path | None = None) -> Path:
    d = tmp_path / "sessions" / name
    d.mkdir(parents=True)
    if src is not None:
        shutil.copy(src, d / "updates.jsonl")
    else:
        (d / "updates.jsonl").write_text("", encoding="utf-8")
    return d


def test_graph_html_constant():
    assert GRAPH_HTML == DASHBOARD_DIR / "graph.html"


def test_missing_root_is_400():
    for fn in (graph_payload, sessions_payload, rescan_payload, replay_payload):
        status, body = fn("")
        assert status == 400
        assert body == {"error": "missing root"}
        status, body = fn("   ")
        assert status == 400
        assert body == {"error": "missing root"}
    status, body = activity_payload("", None)
    assert status == 400
    assert body == {"error": "missing root"}
    status, body = graph_payload(None)  # type: ignore[arg-type]
    assert status == 400


def test_unknown_root_is_403(monkeypatch, tmp_path):
    monkeypatch.setattr("token_telemetry.graph.api.list_sessions_for_ui", lambda: [])
    other = tmp_path / "not-a-session-cwd"
    other.mkdir()
    for fn in (graph_payload, sessions_payload, rescan_payload, replay_payload):
        status, body = fn(str(other))
        assert status == 403
        assert body == {"error": "root not allowlisted"}
    status, body = activity_payload(str(other), None)
    assert status == 403
    assert body == {"error": "root not allowlisted"}


def test_allowlisted_scan_and_slash_variant(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "mod.py").write_text("x = 1\n", encoding="utf-8")
    sess = _session_dir(tmp_path, "sess-1")
    monkeypatch.setattr(
        "token_telemetry.graph.api.list_sessions_for_ui",
        lambda: [_row(sess, repo)],
    )
    status, body = graph_payload(str(repo))
    assert status == 200
    assert {"root", "nodes", "edges", "scanned_at"} <= set(body)
    assert any(n.get("id") == "mod.py" and n.get("kind") == "file" for n in body["nodes"])

    slashy = str(repo).replace("\\", "/") + "/"
    status2, body2 = graph_payload(slashy)
    assert status2 == 200
    assert Path(body2["root"]).resolve() == Path(body["root"]).resolve()

    status3, again = rescan_payload(str(repo))
    assert status3 == 200
    assert {n["id"] for n in again["nodes"]} == {n["id"] for n in body["nodes"]}


def test_allowlisted_empty_scan(monkeypatch, tmp_path):
    empty = tmp_path / "empty-repo"
    empty.mkdir()
    sess = _session_dir(tmp_path, "sess-empty")
    monkeypatch.setattr(
        "token_telemetry.graph.api.list_sessions_for_ui",
        lambda: [_row(sess, empty)],
    )
    status, body = graph_payload(str(empty))
    assert status == 200
    assert body["nodes"] == []
    assert body["edges"] == []


def test_projects_and_sessions(monkeypatch, tmp_path):
    repo = tmp_path / "proj"
    repo.mkdir()
    a = _session_dir(tmp_path, "parent")
    b = _session_dir(tmp_path, "child")
    monkeypatch.setattr(
        "token_telemetry.graph.api.list_sessions_for_ui",
        lambda: [
            _row(a, repo, 2_000.0, session_kind="session"),
            _row(b, repo, 1_900.0, session_kind="subagent", agent_name="explore"),
        ],
    )
    payload = projects_payload()
    assert "projects" in payload
    assert len(payload["projects"]) == 1
    proj = payload["projects"][0]
    assert proj["root"] == normalize_cwd(str(repo))
    assert proj["session_count"] == 2

    status, sess = sessions_payload(str(repo))
    assert status == 200
    ids = [s["session_id"] for s in sess["sessions"]]
    assert ids == ["parent", "child"]


def test_activity_filters_and_caps(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    s1 = _session_dir(tmp_path, "sess-1", FIXTURE)
    s2 = _session_dir(tmp_path, "sess-2")
    monkeypatch.setattr(
        "token_telemetry.graph.api.list_sessions_for_ui",
        lambda: [
            _row(s1, repo, 2_000.0, agent_name="explore"),
            _row(s2, repo, 1_000.0, agent_name="other"),
        ],
    )
    status, all_ev = activity_payload(str(repo), None)
    assert status == 200
    assert all_ev["events"]
    assert {e["sid"] for e in all_ev["events"]} == {"sess-1"}
    kinds = {e["kind"] for e in all_ev["events"]}
    assert kinds >= {"read", "write", "search", "list", "exec"}

    status, one = activity_payload(str(repo), "sess-1")
    assert status == 200
    assert one["events"]
    assert all(e["sid"] == "sess-1" and e["name"] == "explore" for e in one["events"])

    status, none = activity_payload(str(repo), "missing-sid")
    assert status == 200
    assert none["events"] == []

    fat = [{"t": float(i), "tool": "read_file", "path": "a.py",
            "kind": "read", "sid": "sess-1", "name": ""} for i in range(ACTIVITY_CAP + 50)]
    monkeypatch.setattr(
        "token_telemetry.graph.api.parse_updates",
        lambda *a, **k: list(fat),
    )
    status, capped = activity_payload(str(repo), "sess-1")
    assert status == 200
    assert len(capped["events"]) == ACTIVITY_CAP
    assert capped["events"][0]["t"] == 50.0
    assert capped["events"][-1]["t"] == float(ACTIVITY_CAP + 49)


def test_replay_payload(monkeypatch, tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    s1 = _session_dir(tmp_path, "sess-1", FIXTURE)
    monkeypatch.setattr(
        "token_telemetry.graph.api.list_sessions_for_ui",
        lambda: [_row(s1, repo, agent_name="explore")],
    )
    status, body = replay_payload(str(repo))
    assert status == 200
    ts = [e["t"] for e in body["events"]]
    assert ts == sorted(ts)
    assert ts
    assert all(e["sid"] == "sess-1" for e in body["events"])

    fat = [{"t": float(i)} for i in range(REPLAY_CAP + 10)]
    monkeypatch.setattr(
        "token_telemetry.graph.api.collect_replay",
        lambda *a, **k: list(fat),
    )
    status, capped = replay_payload(str(repo))
    assert status == 200
    assert len(capped["events"]) == REPLAY_CAP
    assert capped["events"][0]["t"] == 10.0
    assert capped["events"][-1]["t"] == float(REPLAY_CAP + 9)
