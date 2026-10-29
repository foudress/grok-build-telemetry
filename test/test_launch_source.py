"""Launch opens the running product. Both running → Grok."""

import json
from pathlib import Path

from launch_dashboard import choose_source, dashboard_url, launch_choice, launch_source


def test_both_active_prefers_grok() -> None:
    assert choose_source(grok_active=True, codex_active=True) == "grok"
    assert choose_source(grok_active=True, codex_active=False) == "grok"
    assert choose_source(grok_active=False, codex_active=True) == "codex"
    assert choose_source(grok_active=False, codex_active=False) == "grok"


def test_only_a_live_codex_rollout_opens_codex(tmp_path: Path) -> None:
    day = tmp_path / "codex" / "sessions" / "2026" / "10" / "29"
    day.mkdir(parents=True)
    rollout = day / "rollout-1.jsonl"
    rollout.write_text("{}\n", encoding="utf-8")
    now = rollout.stat().st_mtime + 5
    source = launch_source(
        sessions_root=tmp_path / "grok",
        codex_home=tmp_path / "codex",
        active_ids=[],
        now=now,
    )
    assert source == "codex"
    assert dashboard_url(8765, source) == "http://127.0.0.1:8765/?source=codex"


def test_listed_grok_session_wins_over_a_live_codex(tmp_path: Path) -> None:
    day = tmp_path / "codex" / "sessions" / "2026" / "10" / "29"
    day.mkdir(parents=True)
    rollout = day / "rollout-1.jsonl"
    rollout.write_text("{}\n", encoding="utf-8")
    grok = tmp_path / "grok" / "cwd" / "sid-1"
    grok.mkdir(parents=True)
    updates = grok / "updates.jsonl"
    updates.write_text("{}\n", encoding="utf-8")
    now = max(rollout.stat().st_mtime, updates.stat().st_mtime) + 5
    active = tmp_path / "active.json"
    active.write_text(json.dumps([{"session_id": "sid-1"}]), encoding="utf-8")
    source = launch_source(
        sessions_root=tmp_path / "grok",
        codex_home=tmp_path / "codex",
        active_file=active,
        now=now,
    )
    assert source == "grok"
    assert dashboard_url(8765, source, "sid-1") == (
        "http://127.0.0.1:8765/?source=grok&session=sid-1"
    )


def test_stale_grok_list_does_not_beat_a_live_codex(tmp_path: Path) -> None:
    import os

    day = tmp_path / "codex" / "sessions" / "2026" / "10" / "29"
    day.mkdir(parents=True)
    rollout = day / "rollout-1.jsonl"
    rollout.write_text("{}\n", encoding="utf-8")
    grok = tmp_path / "grok" / "cwd" / "sid-old"
    grok.mkdir(parents=True)
    updates = grok / "updates.jsonl"
    updates.write_text("{}\n", encoding="utf-8")
    now = rollout.stat().st_mtime + 5
    old = now - 3600
    os.utime(updates, (old, old))
    source, sid = launch_choice(
        sessions_root=tmp_path / "grok",
        codex_home=tmp_path / "codex",
        active_ids=["sid-old"],
        now=now,
    )
    assert source == "codex"
    assert sid == ""


def test_live_grok_file_wins_even_if_it_is_not_listed(tmp_path: Path) -> None:
    day = tmp_path / "codex" / "sessions" / "2026" / "10" / "29"
    day.mkdir(parents=True)
    rollout = day / "rollout-1.jsonl"
    rollout.write_text("{}\n", encoding="utf-8")
    grok = tmp_path / "grok" / "cwd" / "sid-live"
    grok.mkdir(parents=True)
    (grok / "updates.jsonl").write_text("{}\n", encoding="utf-8")
    now = rollout.stat().st_mtime + 5
    source, sid = launch_choice(
        sessions_root=tmp_path / "grok",
        codex_home=tmp_path / "codex",
        active_ids=[],
        now=now,
    )
    assert source == "grok"
    assert sid == "sid-live"
