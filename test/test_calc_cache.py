"""Disk calc cache: reuse until files change; reset wipes."""

from __future__ import annotations

import json
from pathlib import Path

from token_telemetry.session import calc_cache as cc
from token_telemetry.session import period_attr as pa
from token_telemetry.session import aggregate as agg_mod


def _sid_dir(root: Path, sid="aaaa1111-0000-0000-0000-000000000001") -> Path:
    d = root / sid
    d.mkdir(parents=True)
    (d / "summary.json").write_text(
        json.dumps({"session_summary": "Cached title"}), encoding="utf-8"
    )
    (d / "updates.jsonl").write_text(
        json.dumps(
            {
                "timestamp": 1_700_000_000,
                "params": {
                    "_meta": {"agentTimestampMs": 1_700_000_000_000},
                    "update": {
                        "sessionUpdate": "turn_completed",
                        "usage": {
                            "inputTokens": 10,
                            "cachedReadTokens": 0,
                            "outputTokens": 1,
                            "modelCalls": 1,
                        },
                    },
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return d


def test_load_save_invalidates_on_growth(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "CACHE_DIR", tmp_path / "cache")
    d = _sid_dir(tmp_path / "sess")
    cc.save_calc(d, events=[{"epoch": 1, "parts": [], "tools": []}])
    hit = cc.load_calc(d)
    assert hit and len(hit["events"]) == 1
    (d / "updates.jsonl").write_text(
        (d / "updates.jsonl").read_text(encoding="utf-8") + "{}\n",
        encoding="utf-8",
    )
    assert cc.load_calc(d) is None


def test_attr_skips_extract_on_disk_hit(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "CACHE_DIR", tmp_path / "cache")
    d = _sid_dir(tmp_path / "sess")
    calls = {"n": 0}

    def fake_extract(session_dir):
        calls["n"] += 1
        return [{"epoch": 9.0, "parts": [], "tools": []}]

    monkeypatch.setattr(pa, "extract_session_events", fake_extract)
    pa.clear_attr_mem()
    a = pa.cached_attr_events(d)
    b = pa.cached_attr_events(d)
    pa.clear_attr_mem()
    c = pa.cached_attr_events(d)
    assert a == b == c
    assert calls["n"] == 1


def test_reset_forces_recompute(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "CACHE_DIR", tmp_path / "cache")
    d = _sid_dir(tmp_path / "sess")
    calls = {"n": 0}

    def fake_extract(session_dir):
        calls["n"] += 1
        return [{"epoch": 1.0, "parts": [], "tools": []}]

    monkeypatch.setattr(pa, "extract_session_events", fake_extract)
    pa.clear_attr_mem()
    pa.cached_attr_events(d)
    assert cc.reset_all_calcs() >= 1
    pa.cached_attr_events(d)
    assert calls["n"] == 2
    assert agg_mod._file_cache == {}


def test_code_sig_change_misses_disk(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "CACHE_DIR", tmp_path / "cache")
    d = _sid_dir(tmp_path / "sess")
    monkeypatch.setattr(cc, "code_sig", lambda: "before")
    cc.save_calc(d, events=[{"epoch": 1}])
    assert cc.load_calc(d) is not None
    monkeypatch.setattr(cc, "code_sig", lambda: "after")
    assert cc.load_calc(d) is None


def test_cache_ver_rejects_previous_blob(tmp_path, monkeypatch):
    monkeypatch.setattr(cc, "CACHE_DIR", tmp_path / "cache")
    d = _sid_dir(tmp_path / "sess")
    assert cc.CACHE_VER >= 1
    cc.save_calc(d, events=[{"epoch": 1}])
    assert cc.load_calc(d) is not None
    path = cc._path_for(d)
    blob = json.loads(path.read_text(encoding="utf-8"))
    assert blob.get("v") == cc.CACHE_VER
    blob["v"] = int(cc.CACHE_VER) - 1
    path.write_text(json.dumps(blob), encoding="utf-8")
    assert cc.load_calc(d) is None


def test_rebuild_current_replays_stale_tree(tmp_path):
    from token_telemetry.session.monitor import SessionMonitor

    d = _sid_dir(tmp_path / "sess")
    extra = json.dumps(
        {
            "params": {
                "_meta": {"agentTimestampMs": 1_700_000_001_000, "promptId": "p2"},
                "update": {
                    "sessionUpdate": "turn_completed",
                    "usage": {
                        "inputTokens": 20,
                        "cachedReadTokens": 0,
                        "outputTokens": 2,
                        "modelCalls": 1,
                    },
                },
            }
        }
    )
    (d / "updates.jsonl").write_text(
        (d / "updates.jsonl").read_text(encoding="utf-8") + extra + "\n",
        encoding="utf-8",
    )
    mon = SessionMonitor()
    mon.attach(d, pin=True)
    out = mon.rebuild_current()
    assert out["rebuilt"] is True
    assert out["session_id"] == d.name
    n = len(mon.hierarchy.rounds)
    assert n >= 1
    mon.hierarchy.rounds.clear()
    assert mon.hierarchy.rounds == []
    mon.rebuild_current()
    assert len(mon.hierarchy.rounds) == n
