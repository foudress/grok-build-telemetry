"""Project grouping by normalized session cwd (pure fake rows)."""

from __future__ import annotations

import os

from token_telemetry.graph.projects import group_projects, normalize_cwd
from token_telemetry.session.discover import format_age


def _row(session_id: str, cwd, epoch: float, **extra):
    out = {
        "session_id": session_id,
        "label": session_id,
        "title": session_id,
        "cwd": cwd,
        "session_kind": extra.pop("session_kind", None),
        "agent_name": extra.pop("agent_name", None),
        "last_active_epoch": epoch,
        "age_label": "1m",
        "path": f"/fake/{session_id}",
    }
    out.update(extra)
    return out


def _existing_cwd() -> str:
    return os.path.abspath(os.getcwd())


def test_normalize_cwd_empty_and_whitespace():
    assert normalize_cwd(None) is None
    assert normalize_cwd("") is None
    assert normalize_cwd("   ") is None
    assert normalize_cwd("\t\n") is None


def test_normalize_cwd_trailing_slash_and_case():
    root = _existing_cwd()
    a = normalize_cwd(root)
    b = normalize_cwd(root + os.sep)
    c = normalize_cwd(root.replace("\\", "/") + "/")
    assert a is not None
    assert a == b == c
    if os.name == "nt":
        assert a == normalize_cwd(root.upper())
        assert a == normalize_cwd(root.lower())
        mixed = root[0].swapcase() + root[1:].replace("\\", "/") + "/"
        assert a == normalize_cwd(mixed)


def test_normalize_cwd_short_8_3_same_bucket():
    if os.name != "nt":
        return
    long = _existing_cwd()
    try:
        import ctypes

        GetShortPathNameW = ctypes.windll.kernel32.GetShortPathNameW
        buf = ctypes.create_unicode_buffer(32768)
        n = GetShortPathNameW(long, buf, 32768)
    except Exception:
        return
    if not n:
        return
    short = buf.value
    if os.path.normcase(short) == os.path.normcase(long):
        return
    assert normalize_cwd(short) == normalize_cwd(long)


def test_same_cwd_case_slash_variants_merge():
    root = _existing_cwd()
    slashy = root.replace("\\", "/") + "/"
    other = root.upper() if os.name == "nt" else root + os.sep
    rows = [
        _row("sess-a", root, 1_000.0),
        _row("sess-b", slashy, 900.0),
        _row("sess-c", other, 800.0),
    ]
    projects = group_projects(rows)
    assert len(projects) == 1
    proj = projects[0]
    assert proj["session_count"] == 3
    assert proj["root"] == normalize_cwd(root)
    assert proj["label"] == os.path.basename(normalize_cwd(root).rstrip("\\/"))
    assert [s["session_id"] for s in proj["sessions"]] == [
        "sess-a",
        "sess-b",
        "sess-c",
    ]


def test_subagent_other_cwd_is_another_project():
    parent_cwd = _existing_cwd()
    child_cwd = os.path.abspath(os.path.join(parent_cwd, ".."))
    assert normalize_cwd(parent_cwd) != normalize_cwd(child_cwd)
    rows = [
        _row("parent", parent_cwd, 2_000.0, session_kind="session"),
        _row(
            "child",
            child_cwd,
            1_900.0,
            session_kind="subagent",
            agent_name="explore",
        ),
    ]
    projects = group_projects(rows)
    assert len(projects) == 2
    by_sid = {
        p["sessions"][0]["session_id"]: p for p in projects
    }
    assert set(by_sid) == {"parent", "child"}
    assert by_sid["parent"]["session_count"] == 1
    assert by_sid["child"]["session_count"] == 1
    assert by_sid["parent"]["sessions"][0]["session_kind"] != "subagent"
    assert by_sid["child"]["sessions"][0]["session_kind"] == "subagent"
    # Newer parent project first
    assert projects[0]["sessions"][0]["session_id"] == "parent"
    assert projects[1]["sessions"][0]["session_id"] == "child"


def test_missing_cwd_dropped():
    root = _existing_cwd()
    missing_key = _row("no-key", root, 50.0)
    del missing_key["cwd"]
    rows = [
        _row("keep", root, 100.0),
        _row("none", None, 400.0),
        _row("empty", "", 300.0),
        _row("ws", "   ", 200.0),
        missing_key,
    ]
    projects = group_projects(rows)
    assert len(projects) == 1
    assert projects[0]["session_count"] == 1
    assert projects[0]["sessions"][0]["session_id"] == "keep"


def test_session_count_and_newest_first(monkeypatch):
    now = 1_700_000_000.0
    monkeypatch.setattr("token_telemetry.graph.projects.time.time", lambda: now)
    root = _existing_cwd()
    other = os.path.abspath(os.path.join(root, ".."))
    rows = [
        _row("old", root, now - 3_600.0),
        _row("new", root, now - 10.0),
        _row("mid", root, now - 120.0),
        _row("other-new", other, now - 5.0),
        _row("other-old", other, now - 9_000.0),
    ]
    projects = group_projects(rows)
    assert [p["session_count"] for p in projects] == [2, 3]
    other_proj, root_proj = projects
    assert [s["session_id"] for s in other_proj["sessions"]] == [
        "other-new",
        "other-old",
    ]
    assert [s["session_id"] for s in root_proj["sessions"]] == [
        "new",
        "mid",
        "old",
    ]
    assert other_proj["last_active_epoch"] == now - 5.0
    assert root_proj["last_active_epoch"] == now - 10.0
    assert other_proj["age_label"] == format_age(5.0)
    assert root_proj["age_label"] == format_age(10.0)
    # Original row objects, not copies
    assert root_proj["sessions"][0] is rows[1]
