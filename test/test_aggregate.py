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


def _write_session(root: Path, sid: str, turns, *, kind=None, title="Hello"):
    d = root / sid
    d.mkdir(parents=True)
    summary = {"generated_title": title}
    if kind:
        summary["session_kind"] = kind
        summary["agent_name"] = "explore"
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
