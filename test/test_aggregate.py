"""Period window math + bucket/session aggregation (no hierarchy)."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from token_telemetry.session.aggregate import (
    build_aggregate,
    period_window,
    week_monday,
    _file_cache,
    _priced_from_io_segs,
)
from token_telemetry.session import aggregate as agg_mod


def _aware(y, m, d, hh=12, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


def test_week_monday():
    assert week_monday(_aware(2026, 8, 14).date()).isoformat() == "2026-08-10"


def test_period_window_daily_weekly_monthly():
    now = _aware(2026, 8, 14, 15, 30)
    s, e, lab = period_window("daily", 0, now=now)
    assert s.day == 14 and e.day == 15
    assert "14" in lab

    s, e, _ = period_window("daily", -1, now=now)
    assert s.day == 13 and e.day == 14

    s, e, _ = period_window("weekly", 0, now=now)
    assert s.date().isoformat() == "2026-08-10"
    assert e.date().isoformat() == "2026-08-17"

    s, e, lab = period_window("monthly", 0, now=now)
    assert s.date().isoformat() == "2026-08-01"
    assert e.date().isoformat() == "2026-09-01"
    assert "August" in lab

    s, e, _ = period_window("monthly", -1, now=now)
    assert s.date().isoformat() == "2026-07-01"
    assert e.date().isoformat() == "2026-08-01"


def _write_session(root: Path, sid: str, turns, *, kind=None, title="Hello", parent=None):
    d = root / sid
    d.mkdir(parents=True)
    summary = {"generated_title": title}
    if kind:
        summary["session_kind"] = kind
        summary["agent_name"] = "explore"
    if parent:
        summary["parent_session_id"] = parent
    (d / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
    lines = []
    for ep, inn, cache, out in turns:
        obj = {
            "timestamp": ep,
            "params": {
                "_meta": {"agentTimestampMs": int(ep * 1000)},
                "update": {
                    "sessionUpdate": "turn_completed",
                    "usage": {
                        "inputTokens": inn,
                        "cachedReadTokens": cache,
                        "outputTokens": out,
                        "modelCalls": 1,
                    },
                },
            },
        }
        lines.append(json.dumps(obj))
    (d / "updates.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_build_aggregate_daily_hourly_and_subagent(tmp_path, monkeypatch):
    now = _aware(2026, 8, 14, 18, 0)
    day0 = datetime(2026, 8, 14, tzinfo=timezone.utc)
    t10 = (day0 + timedelta(hours=10, minutes=5)).timestamp()
    t14 = (day0 + timedelta(hours=14, minutes=20)).timestamp()
    t_yest = (day0 - timedelta(hours=2)).timestamp()

    _write_session(
        tmp_path,
        "aaaa1111-0000-0000-0000-000000000001",
        [
            (t10, 1000, 200, 50),
            (t14, 4000, 1000, 100),
            (t_yest, 99999, 0, 1),
        ],
        title="Main work",
    )
    _write_session(
        tmp_path,
        "bbbb2222-0000-0000-0000-000000000002",
        [(t10, 8000, 7000, 20)],
        kind="subagent",
        title="Hunt code",
    )

    monkeypatch.setattr(agg_mod, "list_session_dirs", lambda: list(tmp_path.iterdir()))
    _file_cache.clear()

    out = build_aggregate("daily", 0, now=now)
    assert out["period"] == "daily"
    assert out["grain"] == "hour"
    assert len(out["buckets"]) == 24

    tot = out["totals"]
    # uncached In = (1000-200) + (4000-1000) = 3800; subagent excluded
    assert tot["tokens_in"] == 3800
    assert tot["tokens_cached"] == 1200
    assert tot["tokens_out"] == 150
    assert tot["tokens_all"] == 3800 + 1200 + 150

    b10 = out["buckets"][10]
    assert b10["tokens_in"] == 800
    assert b10["tokens_cached"] == 200
    b14 = out["buckets"][14]
    assert b14["tokens_in"] == 3000
    assert out["buckets"][0]["tokens_in"] == 0

    ids = {s["session_id"] for s in out["sessions"]}
    assert len(out["sessions"]) == 2
    assert any(s["session_kind"] == "subagent" and s.get("depth") == 1 for s in out["sessions"])
    assert "aaaa1111-0000-0000-0000-000000000001" in ids

    # yesterday's turn is outside
    y = build_aggregate("daily", -1, now=now)
    assert y["totals"]["tokens_in"] == 99999

    q = build_aggregate("daily", 0, grain="15m", now=now)
    assert q["grain"] == "15m"
    assert len(q["buckets"]) == 96
    # 10:05 → 10:00–10:15
    assert q["buckets"][10 * 4]["tokens_in"] == 800
    assert q["buckets"][10 * 4 + 1]["tokens_in"] == 0


def test_monthly_grain_week(tmp_path, monkeypatch):
    now = _aware(2026, 8, 14, 12, 0)
    # Monday Aug 10 2026
    mon = datetime(2026, 8, 10, 9, 0, tzinfo=timezone.utc)
    _write_session(
        tmp_path,
        "cccc3333-0000-0000-0000-000000000003",
        [(mon.timestamp(), 500, 100, 10)],
        title="Week A",
    )
    monkeypatch.setattr(agg_mod, "list_session_dirs", lambda: list(tmp_path.iterdir()))
    _file_cache.clear()

    day = build_aggregate("monthly", 0, grain="day", now=now)
    assert len(day["buckets"]) == 31
    assert day["totals"]["tokens_in"] == 400

    week = build_aggregate("monthly", 0, grain="week", now=now)
    assert week["grain"] == "week"
    assert 4 <= len(week["buckets"]) <= 6
    assert week["totals"]["tokens_in"] == 400
    assert sum(b["tokens_in"] for b in week["buckets"]) == 400
    assert week["buckets"][0]["label"].startswith("01 Aug")

    wh = build_aggregate("weekly", 0, grain="hour", now=now)
    assert wh["grain"] == "hour"
    assert len(wh["buckets"]) == 7 * 24
    wd = build_aggregate("weekly", 0, grain="day", now=now)
    assert wd["grain"] == "day"
    assert len(wd["buckets"]) == 7


def test_session_grain_numeric_bars(tmp_path, monkeypatch):
    now = _aware(2026, 8, 14, 18, 0)
    day0 = datetime(2026, 8, 14, tzinfo=timezone.utc)
    t10 = (day0 + timedelta(hours=10, minutes=5)).timestamp()
    main = "aaaa1111-0000-0000-0000-000000000001"
    sub = "bbbb2222-0000-0000-0000-000000000002"
    # Parent turn_completed includes the child bill (real harness behavior).
    _write_session(
        tmp_path,
        main,
        [(t10, 9000, 7200, 70)],
        title="Main work",
    )
    _write_session(
        tmp_path,
        sub,
        [(t10, 8000, 7000, 20)],
        kind="subagent",
        title="Hunt code",
        parent=main,
    )
    monkeypatch.setattr(agg_mod, "list_session_dirs", lambda: list(tmp_path.iterdir()))
    _file_cache.clear()

    out = build_aggregate("daily", 0, grain="session", now=now)
    assert out["grain"] == "session"
    assert len(out["sessions"]) == 2
    assert len(out["buckets"]) == 2
    # Main = N, Sub Agent = N.M (order still parent then child)
    assert [b["label"] for b in out["buckets"]] == ["1", "1.1"]
    # Parent bar peeled (own work only); sub bar has sub turns; totals parent-only
    by_sid = {b["session_id"]: b for b in out["buckets"]}
    assert by_sid[main]["tokens_in"] == 800  # 1800 raw − 1000 child
    assert by_sid[main]["tokens_cached"] == 200
    assert by_sid[main]["tokens_out"] == 50
    assert by_sid[sub]["tokens_in"] == 1000  # 8000-7000
    assert out["totals"]["tokens_in"] == 800
    assert out["totals"]["tokens_cached"] == 200
    assert out["totals"]["tokens_out"] == 50


def _evt(ep, kind):
    return json.dumps({
        "timestamp": ep,
        "params": {
            "_meta": {"agentTimestampMs": int(ep * 1000)},
            "update": {"sessionUpdate": kind},
        },
    })


def test_session_hierarchy_and_gap_segments(tmp_path, monkeypatch):
    now = _aware(2026, 8, 14, 18, 0)
    day0 = datetime(2026, 8, 14, tzinfo=timezone.utc)
    t10 = (day0 + timedelta(hours=10)).timestamp()
    t1040 = (day0 + timedelta(hours=10, minutes=40)).timestamp()
    t11 = (day0 + timedelta(hours=11)).timestamp()
    t12 = (day0 + timedelta(hours=12)).timestamp()
    t15 = (day0 + timedelta(hours=15)).timestamp()
    t16 = (day0 + timedelta(hours=16)).timestamp()
    t17 = (day0 + timedelta(hours=17)).timestamp()
    later = "cccc3333-0000-0000-0000-000000000003"
    parent = "aaaa1111-0000-0000-0000-000000000001"
    child = "bbbb2222-0000-0000-0000-000000000002"

    _write_session(tmp_path, parent, [(t1040, 100, 0, 10), (t16, 100, 0, 10)], title="Main")
    p = tmp_path / parent / "updates.jsonl"
    extra = "\n".join([
        _evt(t10, "user_message_chunk"),
        _evt(t11, "agent_thought_chunk"),
        json.dumps({
            "timestamp": t11,
            "params": {
                "update": {
                    "sessionUpdate": "tool_call",
                    "toolCall": {"toolName": "spawn_subagent", "rawInput": {"task_ids": [child]}},
                }
            },
        }),
        _evt(t15, "user_message_chunk"),
    ])
    p.write_text(p.read_text(encoding="utf-8") + extra + "\n", encoding="utf-8")

    _write_session(tmp_path, child, [(t12, 50, 0, 5)], kind="subagent", title="Hunt")
    cpath = tmp_path / child / "updates.jsonl"
    cpath.write_text(
        _evt(t11, "user_message_chunk") + "\n"
        + _evt(t11 + 30, "agent_thought_chunk") + "\n"
        + cpath.read_text(encoding="utf-8"),
        encoding="utf-8",
    )

    _write_session(tmp_path, later, [(t17, 20, 0, 2)], title="Later")

    monkeypatch.setattr(agg_mod, "list_session_dirs", lambda: list(tmp_path.iterdir()))
    _file_cache.clear()

    out = build_aggregate("daily", 0, grain="hour", now=now)
    sess = out["sessions"]
    parents = [s for s in sess if s["depth"] == 0]
    assert parents[0]["session_id"] == parent
    assert parents[0]["label"] == "Session 1"
    assert parents[1]["session_id"] == later
    assert parents[1]["label"] == "Session 2"
    assert sess[1]["session_id"] == child
    assert sess[1]["parent_id"] == parent
    assert sess[1]["label"] == "Sub Agent 1"
    assert sess[1]["depth"] == 1
    # child lives ~1h (first event t11 → turn t12), not a point bar
    assert sess[1]["last_epoch"] - sess[1]["first_epoch"] >= 3500
    kinds = {sp["kind"] for sp in sess[0]["spans"]}
    assert "work" in kinds
    assert "wait" in kinds


def test_resume_nests_under_parent_as_r2(tmp_path, monkeypatch):
    now = _aware(2026, 8, 14, 18, 0)
    day0 = datetime(2026, 8, 14, tzinfo=timezone.utc)
    t11 = (day0 + timedelta(hours=11)).timestamp()
    t12 = (day0 + timedelta(hours=12)).timestamp()
    t13 = (day0 + timedelta(hours=13)).timestamp()
    parent = "aaaa1111-0000-0000-0000-000000000001"
    child = "bbbb2222-0000-0000-0000-000000000002"
    resume = "cccc3333-0000-0000-0000-000000000003"

    # Parent wait-turn includes the latest child bill (180/18 = own 100/10 + resume 80/8).
    _write_session(tmp_path, parent, [(t11, 180, 0, 18)], title="Main")
    p = tmp_path / parent / "updates.jsonl"
    p.write_text(
        p.read_text(encoding="utf-8")
        + json.dumps(
            {
                "timestamp": t11,
                "params": {
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCall": {
                            "toolName": "spawn_subagent",
                            "rawInput": {"task_ids": [child]},
                        },
                    }
                },
            }
        )
        + "\n"
        + json.dumps(
            {
                "timestamp": t13,
                "params": {
                    "update": {
                        "sessionUpdate": "tool_call",
                        "toolCall": {
                            "toolName": "spawn_subagent",
                            "rawInput": {"resume_from": child},
                        },
                    }
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    _write_session(tmp_path, child, [(t12, 50, 0, 5)], kind="subagent", title="Hunt")
    _write_session(
        tmp_path,
        resume,
        [(t13, 80, 0, 8)],
        kind="subagent_resume",
        title="Hunt",
        parent=child,
    )

    monkeypatch.setattr(agg_mod, "list_session_dirs", lambda: list(tmp_path.iterdir()))
    _file_cache.clear()
    out = build_aggregate("daily", 0, grain="hour", now=now)
    sess = out["sessions"]
    parents = [s for s in sess if s["depth"] == 0]
    assert len(parents) == 1
    assert parents[0]["session_id"] == parent
    kids = [s for s in sess if s["depth"] == 1]
    assert len(kids) == 1
    # Latest resume only (no spawn+resume sum / no Round-1 duplicate row)
    assert kids[0]["session_id"] == resume
    assert kids[0]["parent_id"] == parent
    assert kids[0]["root_session_id"] == child
    assert kids[0]["label"] == "Sub Agent 1"
    assert kids[0]["tokens_out"] == 8
    # Parent I/O peeled to own work; resume listed separately (not double-counted)
    assert out["totals"]["tokens_out"] == 10
    assert parents[0]["tokens_out"] == 10
    assert parents[0]["tokens_in"] == 100


def test_later_session_text_dump_does_not_steal_subagent(tmp_path, monkeypatch):
    """Skill/chat dumps that mention spawn_subagent + UUIDs must not reparent kids."""
    now = _aware(2026, 8, 14, 18, 0)
    day0 = datetime(2026, 8, 14, tzinfo=timezone.utc)
    t11 = (day0 + timedelta(hours=11)).timestamp()
    t12 = (day0 + timedelta(hours=12)).timestamp()
    t16 = (day0 + timedelta(hours=16)).timestamp()
    parent = "aaaa1111-0000-0000-0000-000000000001"
    later = "cccc3333-0000-0000-0000-000000000003"
    child = "bbbb2222-0000-0000-0000-000000000002"

    _write_session(tmp_path, parent, [(t11, 100, 0, 10)], title="Owner")
    p = tmp_path / parent / "updates.jsonl"
    p.write_text(
        p.read_text(encoding="utf-8")
        + json.dumps(
            {
                "timestamp": t11,
                "method": "session/update",
                "params": {
                    "sessionId": parent,
                    "update": {
                        "sessionUpdate": "subagent_spawned",
                        "subagent_id": child,
                        "parent_session_id": parent,
                        "child_session_id": child,
                    },
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    _write_session(tmp_path, child, [(t12, 50, 0, 5)], kind="subagent", title="Hunt")
    # Later main: huge tool_call_update body mentions spawn_subagent + child UUID
    # (same failure mode as reading a skill / prior plan). Must not steal.
    _write_session(tmp_path, later, [(t16, 20, 0, 2)], title="Later")
    lp = tmp_path / later / "updates.jsonl"
    dump = (
        "How to use spawn_subagent and get_command_or_subagent_output.\n"
        f"Example subagent_id: {child}\n"
        f'task_ids=["{child}"]\n'
    )
    lp.write_text(
        lp.read_text(encoding="utf-8")
        + json.dumps(
            {
                "timestamp": t16,
                "params": {
                    "update": {
                        "sessionUpdate": "tool_call_update",
                        "title": "read_file",
                        "toolName": "read_file",
                        "status": "completed",
                        "content": [{"type": "content", "content": {"type": "text", "text": dump}}],
                    }
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(agg_mod, "list_session_dirs", lambda: list(tmp_path.iterdir()))
    _file_cache.clear()
    out = build_aggregate("daily", 0, grain="hour", now=now)
    kids = [s for s in out["sessions"] if s.get("depth") == 1]
    assert len(kids) == 1
    assert kids[0]["session_id"] == child
    assert kids[0]["parent_id"] == parent
    assert kids[0]["tokens_out"] == 5
    assert kids[0]["tokens_in"] == 50
    # White line price is list-rate estimate, not official ticks
    assert "estimate_usd" in kids[0]
    assert kids[0]["estimate_usd"] > 0


def test_title_prefers_session_summary_even_with_bom(tmp_path, monkeypatch):
    now = _aware(2026, 8, 14, 12, 0)
    t12 = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc).timestamp()
    sid = "dddd4444-0000-0000-0000-000000000004"
    d = tmp_path / sid
    d.mkdir()
    body = json.dumps(
        {
            "session_summary": "Real summary title",
            "generated_title": "Old generated",
        }
    )
    (d / "summary.json").write_bytes(b"\xef\xbb\xbf" + body.encode("utf-8"))
    (d / "updates.jsonl").write_text(
        json.dumps(
            {
                "timestamp": t12,
                "params": {
                    "_meta": {"agentTimestampMs": int(t12 * 1000)},
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
    monkeypatch.setattr(agg_mod, "list_session_dirs", lambda: [d])
    _file_cache.clear()
    out = build_aggregate("daily", 0, now=now)
    assert out["sessions"][0]["title"] == "Real summary title"

def test_priced_from_io_segs_maps_in_cached_out():
    p = _priced_from_io_segs([
        {"k": "in", "usd": 0.02, "tok": 50},
        {"k": "cached", "usd": 0.01, "tok": 200},
        {"k": "out", "usd": 0.03, "tok": 10},
    ])
    assert p["tokens_in"] == 50
    assert p["tokens_cached"] == 200
    assert p["tokens_out"] == 10
    assert abs(p["cost_in_usd"] - 0.02) < 1e-9
    assert abs(p["cost_cached_usd"] - 0.01) < 1e-9
    assert abs(p["cost_out_usd"] - 0.03) < 1e-9
    assert abs(p["estimate_usd"] - 0.06) < 1e-9
    assert p["official_usd"] == 0.0


def test_period_io_includes_recap_side_not_in_turns(tmp_path, monkeypatch):
    """I/O mode must add recap In/Cached/Out (absent from turn_completed)."""
    now = _aware(2026, 8, 14, 18, 0)
    day0 = datetime(2026, 8, 14, tzinfo=timezone.utc)
    t10 = (day0 + timedelta(hours=10, minutes=5)).timestamp()
    t105 = (day0 + timedelta(hours=10, minutes=30)).timestamp()
    sid = "eeee5555-0000-0000-0000-000000000005"
    _write_session(tmp_path, sid, [(t10, 1000, 200, 50)], title="With recap")

    def fake_attr(d):
        return [
            {
                "epoch": t105,
                "io": [
                    {"k": "in", "usd": 0.01, "tok": 40},
                    {"k": "cached", "usd": 0.02, "tok": 300},
                    {"k": "out", "usd": 0.005, "tok": 5},
                ],
                "parts": [{"key": "recap", "k": "recap", "label": "recap", "usd": 0.035, "tok": 345}],
                "tools": [{"key": "recap", "k": "recap", "label": "recap", "usd": 0.035, "tok": 345}],
            }
        ]

    monkeypatch.setattr(agg_mod, "list_session_dirs", lambda: list(tmp_path.iterdir()))
    monkeypatch.setattr(agg_mod, "cached_attr_events", fake_attr)
    _file_cache.clear()

    out = build_aggregate("daily", 0, stack="io", now=now)
    tot = out["totals"]
    # turn: uncached 800 + cache 200 + out 50; recap: +40 / +300 / +5
    assert tot["tokens_in"] == 800 + 40
    assert tot["tokens_cached"] == 200 + 300
    assert tot["tokens_out"] == 50 + 5
    b10 = out["buckets"][10]
    assert b10["tokens_in"] == 800 + 40
    assert b10["tokens_cached"] == 200 + 300
    assert b10["tokens_out"] == 50 + 5
    sess = out["sessions"][0]
    assert sess["tokens_in"] == 800 + 40
    assert sess["tokens_cached"] == 200 + 300
    assert sess["turns"] == 1  # recap must not inflate turn count
    # I/O fetch still fills Parts/Tools so UI can switch stack without refetch.
    assert out.get("cats_ready") is True
    assert out.get("rate_full") is False
    assert any(c.get("key") == "recap" for c in (tot.get("parts") or []))


def test_attr_skipped_for_sessions_outside_window(tmp_path, monkeypatch):
    """Cold attr must not run for sessions with zero in-window turns."""
    now = _aware(2026, 8, 14, 18, 0)
    day0 = datetime(2026, 8, 14, tzinfo=timezone.utc)
    t_in = (day0 + timedelta(hours=10)).timestamp()
    t_out = (day0 - timedelta(days=3)).timestamp()
    in_sid = "ffff6666-0000-0000-0000-000000000006"
    out_sid = "ffff6666-0000-0000-0000-000000000007"
    _write_session(tmp_path, in_sid, [(t_in, 100, 0, 10)], title="In window")
    _write_session(tmp_path, out_sid, [(t_out, 100, 0, 10)], title="Outside")

    seen: list[str] = []

    def fake_attr(d):
        seen.append(d.name)
        return []

    monkeypatch.setattr(agg_mod, "list_session_dirs", lambda: list(tmp_path.iterdir()))
    monkeypatch.setattr(agg_mod, "cached_attr_events", fake_attr)
    _file_cache.clear()

    out = build_aggregate("daily", 0, stack="io", now=now)
    assert out_sid not in seen
    assert in_sid in seen
    assert out["session_count"] == 1


def test_rate_full_includes_flag(tmp_path, monkeypatch):
    now = _aware(2026, 8, 14, 18, 0)
    day0 = datetime(2026, 8, 14, tzinfo=timezone.utc)
    t10 = (day0 + timedelta(hours=10)).timestamp()
    sid = "ffff6666-0000-0000-0000-000000000008"
    _write_session(tmp_path, sid, [(t10, 100, 0, 10)], title="Rate")

    def fake_attr(d):
        return [{
            "epoch": t10,
            "tps": 12.5,
            "round": 1,
            "gen_ms": 800,
            "gen_out_tokens": 10,
            "io": [],
            "parts": [],
            "tools": [],
        }]

    monkeypatch.setattr(agg_mod, "list_session_dirs", lambda: list(tmp_path.iterdir()))
    monkeypatch.setattr(agg_mod, "cached_attr_events", fake_attr)
    _file_cache.clear()

    cold = build_aggregate("daily", 0, rate=False, now=now)
    assert cold.get("rate_full") is False
    assert cold["tps_sessions"]  # mains tps still present for preview
    hot = build_aggregate("daily", 0, rate=True, now=now)
    assert hot.get("rate_full") is True
    assert hot["tps_sessions"][0]["v"] == 12.5


def test_build_aggregate_progress_callback(tmp_path, monkeypatch):
    now = _aware(2026, 8, 14, 18, 0)
    day0 = datetime(2026, 8, 14, tzinfo=timezone.utc)
    t10 = (day0 + timedelta(hours=10, minutes=5)).timestamp()
    _write_session(
        tmp_path,
        "prog1111-0000-0000-0000-000000000001",
        [(t10, 1000, 200, 50)],
        title="Prog A",
    )
    _write_session(
        tmp_path,
        "prog2222-0000-0000-0000-000000000002",
        [(t10, 2000, 100, 20)],
        title="Prog B",
    )
    monkeypatch.setattr(agg_mod, "list_session_dirs", lambda: list(tmp_path.iterdir()))
    _file_cache.clear()
    ticks = []
    cold_ticks = []

    def on_prog(d, t, cold=None):
        ticks.append((d, t))
        if cold is not None:
            cold_ticks.append(cold)

    out = build_aggregate("daily", 0, now=now, on_progress=on_prog)
    assert ticks[0] == (0, 2)
    assert ticks[-1] == (2, 2)
    assert [t[0] for t in ticks] == [0, 1, 2]
    assert out["session_count"] == 2
    # First ping reports how many attr rebuilds are needed.
    assert cold_ticks and cold_ticks[0] >= 0

    # Second pass should be warm (disk/mem calc-cache) → cold=0.
    ticks.clear()
    cold_ticks.clear()
    build_aggregate("daily", 0, now=now, on_progress=on_prog)
    assert cold_ticks and cold_ticks[0] == 0
