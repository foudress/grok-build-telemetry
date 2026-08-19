"""chat_history prefix mutation vs append (live watcher)."""

from __future__ import annotations

import json
import time
from pathlib import Path

from token_telemetry.session import history_watch as hw
from token_telemetry.session.history_watch import (
    HistoryWatcher,
    classify_diff,
    diff_fingerprints,
    fingerprint,
    rec_hash,
)


def _rec(typ, content, **kw):
    o = {"type": typ, "content": content}
    o.update(kw)
    return o


def _snaps(recs):
    return [fingerprint(r, i) for i, r in enumerate(recs)]


def test_append_only_not_a_mutation():
    old = _snaps([_rec("system", "S"), _rec("user", "hi", prompt_index=0)])
    new = _snaps(
        [
            _rec("system", "S"),
            _rec("user", "hi", prompt_index=0),
            _rec("assistant", "ok"),
        ]
    )
    d = diff_fingerprints(old, new)
    assert d["kind"] == "append"
    assert d["cache_break_index"] is None
    assert d["prefix"] == 2
    assert d["added"] == 1


def test_tail_edit_after_last_user():
    rid = "r1"
    old = _snaps(
        [
            _rec("system", "S"),
            _rec("user", "q", prompt_index=0),
            _rec("reasoning", "", id=rid, status="in_progress", encrypted_content="aa"),
        ]
    )
    new = _snaps(
        [
            _rec("system", "S"),
            _rec("user", "q", prompt_index=0),
            _rec("reasoning", "", id=rid, status="completed", encrypted_content="aabb"),
            _rec("assistant", "ans"),
        ]
    )
    d = diff_fingerprints(old, new)
    assert d["kind"] == "tail"
    assert d["cache_break_index"] == 2
    assert any(c["op"] == "edit" for c in d["changes"])


def test_middle_user_edit_is_mutate():
    old = _snaps(
        [
            _rec("system", "S"),
            _rec("user", "long prompt v1", prompt_index=0),
            _rec("assistant", "a1"),
            _rec("user", "follow", prompt_index=1),
        ]
    )
    new = _snaps(
        [
            _rec("system", "S"),
            _rec("user", "compacted prompt", prompt_index=0),
            _rec("assistant", "a1"),
            _rec("user", "follow", prompt_index=1),
        ]
    )
    d = diff_fingerprints(old, new)
    assert d["kind"] == "mutate"
    assert d["cache_break_index"] == 1
    edits = [c for c in d["changes"] if c["op"] == "edit"]
    assert len(edits) == 1
    assert edits[0]["index"] == 1
    assert "long prompt" in (edits[0]["old_preview"] or "")
    assert "compacted" in (edits[0]["new_preview"] or "")


def test_system_rewrite_is_mutate():
    old = _snaps([_rec("system", "rules v1"), _rec("user", "q", prompt_index=0)])
    new = _snaps([_rec("system", "rules v2 shorter"), _rec("user", "q", prompt_index=0)])
    d = diff_fingerprints(old, new)
    assert d["kind"] == "mutate"
    assert d["cache_break_index"] == 0
    assert d["from_compact"] is False


def test_compaction_replace_is_mutate():
    old = _snaps(
        [
            _rec("system", "S"),
            _rec("user", "q1", prompt_index=0),
            _rec("assistant", "a1"),
            _rec("user", "q2", prompt_index=1),
            _rec("assistant", "a2"),
        ]
    )
    new = _snaps(
        [
            _rec("system", "S"),
            _rec("user", "this session is being continued… summary", synthetic_reason="compact"),
            _rec("user", "q2", prompt_index=1),
        ]
    )
    d = diff_fingerprints(old, new)
    assert d["kind"] == "mutate"
    assert d["cache_break_index"] == 1
    assert d["removed"] >= 1
    assert d["from_compact"] is True
    assert d["compact_why"]


def test_truncate_kind():
    # Shrink that removes a prior turn (not just the open tail)
    old = _snaps(
        [
            _rec("system", "S"),
            _rec("user", "q1", prompt_index=0),
            _rec("assistant", "a1"),
            _rec("user", "q2", prompt_index=1),
            _rec("assistant", "a2"),
        ]
    )
    new = _snaps(
        [
            _rec("system", "S"),
            _rec("user", "q1", prompt_index=0),
            _rec("assistant", "a1"),
        ]
    )
    assert classify_diff(old, new) == "truncate"
    d = diff_fingerprints(old, new)
    assert d["kind"] == "truncate"
    assert d["removed"] >= 1
    # Dropping only the last assistant after last user is tail, not truncate
    old_t = _snaps([_rec("system", "S"), _rec("user", "q"), _rec("assistant", "a")])
    new_t = _snaps([_rec("system", "S"), _rec("user", "q")])
    assert classify_diff(old_t, new_t) == "tail"


def test_same_content_same_hash():
    a = _rec("user", [{"type": "text", "text": "hello"}], prompt_index=0)
    b = _rec("user", [{"type": "text", "text": "hello"}], prompt_index=0)
    assert rec_hash(a) == rec_hash(b)


def test_watcher_baseline_then_mutate(tmp_path: Path):
    cwd = tmp_path / "proj"
    sid = "sess-live-1"
    d = cwd / sid
    d.mkdir(parents=True)
    hist = d / "chat_history.jsonl"
    # discover requires updates.jsonl
    (d / "updates.jsonl").write_text("{}\n", encoding="utf-8")
    recs = [_rec("system", "S"), _rec("user", "hello", prompt_index=0)]
    hist.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")

    w = HistoryWatcher(root=tmp_path)
    w.tick()
    snap = w.snapshot()
    assert len(snap["sessions"]) == 1
    assert snap["sessions"][0]["mutations"] == 0
    kinds = [e["kind"] for e in snap["events"]]
    assert kinds[0] == "baseline"

    recs[1] = _rec("user", "hello COMPRESSED", prompt_index=0)
    recs.append(_rec("assistant", "ok"))
    time.sleep(0.02)
    hist.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")
    w.tick()
    snap = w.snapshot()
    assert snap["sessions"][0]["mutations"] == 1
    muts = [e for e in snap["events"] if e["kind"] == "mutate"]
    assert len(muts) == 1
    assert muts[0]["cache_break_index"] == 1


def test_watcher_new_session_after_start(tmp_path: Path):
    w = HistoryWatcher(root=tmp_path)
    w.tick()
    assert w.snapshot()["sessions"] == []

    d = tmp_path / "proj" / "sess-new"
    d.mkdir(parents=True)
    (d / "updates.jsonl").write_text("{}\n", encoding="utf-8")
    recs = [_rec("system", "S")]
    (d / "chat_history.jsonl").write_text(json.dumps(recs[0]) + "\n", encoding="utf-8")
    w.tick()
    assert len(w.snapshot()["sessions"]) == 1

    recs.append(_rec("user", "q", prompt_index=0))
    time.sleep(0.02)
    (d / "chat_history.jsonl").write_text(
        "\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8"
    )
    w.tick()
    ev = [e for e in w.snapshot()["events"] if e["kind"] == "append"]
    assert ev and ev[0]["added"] == 1


def test_watcher_evicts_stale_for_new_session(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(hw, "MAX_SESSIONS", 2)
    w = HistoryWatcher(root=tmp_path)
    for i in range(2):
        d = tmp_path / "p" / f"old-{i}"
        d.mkdir(parents=True)
        (d / "updates.jsonl").write_text("{}\n", encoding="utf-8")
        (d / "chat_history.jsonl").write_text(
            json.dumps(_rec("system", f"S{i}")) + "\n", encoding="utf-8"
        )
        time.sleep(0.02)
    w.tick()
    assert {s["session_id"] for s in w.snapshot()["sessions"]} == {"old-0", "old-1"}
    d = tmp_path / "p" / "new-hot"
    d.mkdir(parents=True)
    (d / "updates.jsonl").write_text("{}\n", encoding="utf-8")
    time.sleep(0.02)
    (d / "chat_history.jsonl").write_text(
        json.dumps(_rec("system", "NEW")) + "\n", encoding="utf-8"
    )
    w.tick()
    ids = {s["session_id"] for s in w.snapshot()["sessions"]}
    assert "new-hot" in ids
    assert len(ids) == 2


def test_incomplete_last_line_skipped(tmp_path: Path):
    d = tmp_path / "proj" / "sess-partial"
    d.mkdir(parents=True)
    (d / "updates.jsonl").write_text("{}\n", encoding="utf-8")
    hist = d / "chat_history.jsonl"
    hist.write_text(json.dumps(_rec("system", "S")) + "\n", encoding="utf-8")
    w = HistoryWatcher(root=tmp_path)
    w.tick()
    n0 = w.snapshot()["sessions"][0]["records"]
    time.sleep(0.02)
    hist.write_text(
        json.dumps(_rec("system", "S")) + "\n" + '{"type":"user","content":',
        encoding="utf-8",
    )
    w.tick()
    assert w.snapshot()["sessions"][0]["records"] == n0
