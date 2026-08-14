"""Period aggregates (daily / weekly / monthly) from official turn usage.

Does not load hierarchy. Parent ``turn_completed`` already includes sub-agent
bills — subagent session dirs are listed but excluded from totals/buckets.
"""

from __future__ import annotations

import json
import threading
from calendar import monthrange
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

from token_telemetry.session.discover import (
    _read_session_summary,
    list_session_dirs,
)
from token_telemetry.session.subagents import price_child_usage


_lock = threading.Lock()
# path -> {mtime, size, turns, session_id, title, kind, agent_name}
_file_cache: dict[str, dict[str, Any]] = {}


def _local_now(now: Optional[datetime] = None) -> datetime:
    if now is None:
        return datetime.now().astimezone()
    if now.tzinfo is None:
        return now.astimezone()
    return now


def _as_date(dt: datetime) -> date:
    return dt.date()


def week_monday(d: date) -> date:
    return d - timedelta(days=d.weekday())


def period_window(
    period: str,
    offset: int = 0,
    now: Optional[datetime] = None,
) -> tuple[datetime, datetime, str]:
    """Inclusive-start / exclusive-end local window + human label."""
    now = _local_now(now)
    tz = now.tzinfo
    today = _as_date(now)
    period = (period or "daily").strip().lower()
    off = int(offset or 0)

    if period == "weekly":
        start_d = week_monday(today) + timedelta(weeks=off)
        end_d = start_d + timedelta(days=7)
        start = datetime(start_d.year, start_d.month, start_d.day, tzinfo=tz)
        end = datetime(end_d.year, end_d.month, end_d.day, tzinfo=tz)
        label = f"{start_d.strftime('%d %b')} – {(end_d - timedelta(days=1)).strftime('%d %b %Y')}"
        return start, end, label

    if period == "monthly":
        # offset months from current month
        y, m = today.year, today.month
        m0 = m - 1 + off
        y += m0 // 12
        m = m0 % 12 + 1
        last = monthrange(y, m)[1]
        start = datetime(y, m, 1, tzinfo=tz)
        if m == 12:
            end = datetime(y + 1, 1, 1, tzinfo=tz)
        else:
            end = datetime(y, m + 1, 1, tzinfo=tz)
        label = start.strftime("%B %Y")
        return start, end, label

    # daily
    day = today + timedelta(days=off)
    start = datetime(day.year, day.month, day.day, tzinfo=tz)
    end = start + timedelta(days=1)
    label = start.strftime("%a %d %b %Y")
    return start, end, label


def _event_epoch(obj: dict[str, Any]) -> Optional[float]:
    ts = obj.get("timestamp")
    if isinstance(ts, (int, float)):
        v = float(ts)
        if v > 1e12:
            return v / 1000.0
        if v > 1e9:
            return v
    params = obj.get("params") if isinstance(obj.get("params"), dict) else {}
    meta = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
    ms = meta.get("agentTimestampMs")
    if isinstance(ms, (int, float)) and float(ms) > 1e12:
        return float(ms) / 1000.0
    upd = params.get("update") if isinstance(params.get("update"), dict) else {}
    um = upd.get("_meta") if isinstance(upd.get("_meta"), dict) else {}
    ms2 = um.get("agentTimestampMs")
    if isinstance(ms2, (int, float)) and float(ms2) > 1e12:
        return float(ms2) / 1000.0
    return None


def _empty_acc() -> dict[str, float]:
    return {
        "tokens_in": 0,
        "tokens_cached": 0,
        "tokens_out": 0,
        "cost_in_usd": 0.0,
        "cost_cached_usd": 0.0,
        "cost_out_usd": 0.0,
        "official_usd": 0.0,
        "turns": 0,
    }


def _add_priced(acc: dict[str, float], priced: dict[str, Any]) -> None:
    acc["tokens_in"] += int(priced.get("tokens_in") or 0)
    acc["tokens_cached"] += int(priced.get("tokens_cached") or 0)
    acc["tokens_out"] += int(priced.get("tokens_out") or 0)
    acc["cost_in_usd"] += float(priced.get("cost_in_usd") or 0)
    acc["cost_cached_usd"] += float(priced.get("cost_cached_usd") or 0)
    acc["cost_out_usd"] += float(priced.get("cost_out_usd") or 0)
    acc["official_usd"] += float(priced.get("official_usd") or 0)
    acc["turns"] += 1


def _round_acc(acc: dict[str, float]) -> dict[str, Any]:
    tin = int(acc.get("tokens_in") or 0)
    tc = int(acc.get("tokens_cached") or 0)
    tout = int(acc.get("tokens_out") or 0)
    ci = float(acc.get("cost_in_usd") or 0)
    cc = float(acc.get("cost_cached_usd") or 0)
    co = float(acc.get("cost_out_usd") or 0)
    tot = float(acc.get("official_usd") or 0) or (ci + cc + co)
    return {
        "tokens_in": tin,
        "tokens_cached": tc,
        "tokens_out": tout,
        "tokens_all": tin + tc + tout,
        "cost_in_usd": round(ci, 6),
        "cost_cached_usd": round(cc, 6),
        "cost_out_usd": round(co, 6),
        "official_usd": round(tot, 6),
        "turns": int(acc.get("turns") or 0),
    }


def _parse_turns(path: Path) -> list[dict[str, Any]]:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    out: list[dict[str, Any]] = []
    for line in raw.splitlines():
        if "turn_completed" not in line:
            continue
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        params = o.get("params") if isinstance(o.get("params"), dict) else {}
        upd = params.get("update") if isinstance(params.get("update"), dict) else {}
        if upd.get("sessionUpdate") != "turn_completed":
            continue
        usage = upd.get("usage")
        if not isinstance(usage, dict):
            continue
        epoch = _event_epoch(o)
        if epoch is None:
            continue
        priced = price_child_usage(usage)
        if (
            not priced["tokens_in"]
            and not priced["tokens_cached"]
            and not priced["tokens_out"]
            and not priced["official_usd"]
        ):
            continue
        out.append({"epoch": epoch, **priced})
    return out


def _session_meta(session_dir: Path) -> tuple[str, Optional[str], Optional[str]]:
    summary = _read_session_summary(session_dir)
    kind = summary.get("session_kind")
    if isinstance(kind, str):
        kind = kind.strip().lower() or None
    else:
        kind = None
    agent = summary.get("agent_name")
    if not isinstance(agent, str) or not agent.strip():
        agent = None
    else:
        agent = agent.strip()
    title = (
        summary.get("generated_title")
        or summary.get("session_summary")
        or agent
        or session_dir.name[:8]
    )
    if isinstance(title, str):
        title = title.strip() or session_dir.name[:8]
        if len(title) > 72:
            title = title[:69] + "…"
    else:
        title = session_dir.name[:8]
    return str(title), kind, agent


def _cached_file(session_dir: Path) -> dict[str, Any]:
    p = session_dir / "updates.jsonl"
    key = str(p)
    try:
        st = p.stat()
        mtime = st.st_mtime
        size = st.st_size
    except OSError:
        return {
            "mtime": 0,
            "size": 0,
            "turns": [],
            "session_id": session_dir.name,
            "title": session_dir.name[:8],
            "kind": None,
            "agent_name": None,
        }
    hit = _file_cache.get(key)
    if hit and hit.get("mtime") == mtime and hit.get("size") == size:
        return hit
    title, kind, agent = _session_meta(session_dir)
    row = {
        "mtime": mtime,
        "size": size,
        "turns": _parse_turns(p),
        "session_id": session_dir.name,
        "title": title,
        "kind": kind,
        "agent_name": agent,
    }
    _file_cache[key] = row
    return row


def _scan_files() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    live: set[str] = set()
    for d in list_session_dirs():
        p = str(d / "updates.jsonl")
        live.add(p)
        rows.append(_cached_file(d))
    for stale in list(_file_cache.keys()):
        if stale not in live:
            _file_cache.pop(stale, None)
    return rows


def normalize_grain(period: str, grain: Optional[str]) -> str:
    g = (grain or "").strip().lower().replace(" ", "")
    g = g.replace("min", "m")
    if g in ("15", "15m", "m15"):
        g = "15m"
    elif g in ("hour", "hourly", "hr"):
        g = "hour"
    elif g in ("day", "daily"):
        g = "day"
    elif g in ("week", "weekly"):
        g = "week"
    allowed = {
        "daily": ("hour", "15m"),
        "weekly": ("hour", "day"),
        "monthly": ("day", "week"),
    }
    opts = allowed.get(period, ("hour",))
    if g not in opts:
        return {"daily": "hour", "weekly": "day", "monthly": "day"}.get(period, "hour")
    return g


def _bucket_specs(
    period: str,
    start: datetime,
    end: datetime,
    grain: str,
) -> list[dict[str, Any]]:
    """Ordered empty buckets covering [start, end)."""
    period = (period or "daily").lower()
    grain = normalize_grain(period, grain)
    specs: list[dict[str, Any]] = []
    tz = start.tzinfo

    if grain == "15m":
        t = start
        step = timedelta(minutes=15)
        while t < end:
            t1 = t + step
            specs.append(
                {
                    "key": t.strftime("%Y-%m-%dT%H:%M"),
                    "label": t.strftime("%H:%M"),
                    "start_epoch": t.timestamp(),
                    "end_epoch": t1.timestamp(),
                }
            )
            t = t1
        return specs

    if grain == "hour":
        t = start
        step = timedelta(hours=1)
        while t < end:
            t1 = t + step
            if period == "weekly" and t.hour == 0:
                label = t.strftime("%a %Hh")
            else:
                label = f"{t.hour:02d}h"
            specs.append(
                {
                    "key": t.strftime("%Y-%m-%dT%H"),
                    "label": label,
                    "start_epoch": t.timestamp(),
                    "end_epoch": t1.timestamp(),
                }
            )
            t = t1
        return specs

    if grain == "week":
        d = week_monday(_as_date(start))
        end_d = _as_date(end)
        month_start_d = _as_date(start)
        while d < end_d:
            week_end_d = d + timedelta(days=7)
            clip0 = max(d, month_start_d)
            clip1 = min(week_end_d, end_d)
            if clip0 < clip1:
                b0 = datetime(clip0.year, clip0.month, clip0.day, tzinfo=tz)
                b1 = datetime(clip1.year, clip1.month, clip1.day, tzinfo=tz)
                last = clip1 - timedelta(days=1)
                specs.append(
                    {
                        "key": f"W{d.isocalendar()[0]}-{d.isocalendar()[1]:02d}",
                        "label": f"{clip0.strftime('%d %b')}–{last.strftime('%d %b')}",
                        "start_epoch": b0.timestamp(),
                        "end_epoch": b1.timestamp(),
                    }
                )
            d += timedelta(days=7)
        return specs

    # daily bars
    d = _as_date(start)
    end_d = _as_date(end)
    while d < end_d:
        b0 = datetime(d.year, d.month, d.day, tzinfo=tz)
        b1 = b0 + timedelta(days=1)
        specs.append(
            {
                "key": d.isoformat(),
                "label": d.strftime("%a %d"),
                "start_epoch": b0.timestamp(),
                "end_epoch": b1.timestamp(),
            }
        )
        d += timedelta(days=1)
    return specs


def _place(epoch: float, specs: list[dict[str, Any]]) -> Optional[int]:
    for i, s in enumerate(specs):
        if s["start_epoch"] <= epoch < s["end_epoch"]:
            return i
    return None


def build_aggregate(
    period: str = "daily",
    offset: int = 0,
    grain: str = "day",
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    period = (period or "daily").strip().lower()
    if period not in ("daily", "weekly", "monthly"):
        period = "daily"
    grain = normalize_grain(period, grain)
    try:
        offset = int(offset)
    except (TypeError, ValueError):
        offset = 0

    start, end, label = period_window(period, offset, now=now)
    start_e, end_e = start.timestamp(), end.timestamp()
    specs = _bucket_specs(period, start, end, grain)
    buckets = [{**s, **_empty_acc()} for s in specs]
    totals = _empty_acc()
    sessions: list[dict[str, Any]] = []

    with _lock:
        files = _scan_files()

    for row in files:
        kind = row.get("kind")
        billed = kind != "subagent"
        sess_acc = _empty_acc()
        last_ep = None
        for t in row.get("turns") or []:
            ep = t.get("epoch")
            if ep is None or ep < start_e or ep >= end_e:
                continue
            _add_priced(sess_acc, t)
            if last_ep is None or ep > last_ep:
                last_ep = ep
            if billed:
                _add_priced(totals, t)
                idx = _place(float(ep), specs)
                if idx is not None:
                    _add_priced(buckets[idx], t)
        if sess_acc["turns"] <= 0:
            continue
        title = row.get("title") or row["session_id"][:8]
        if kind == "subagent":
            sub = row.get("agent_name") or "sub"
            title = f"↳ {sub} · {title}"
        sessions.append(
            {
                "session_id": row["session_id"],
                "title": title,
                "session_kind": kind or "main",
                "agent_name": row.get("agent_name"),
                "last_epoch": last_ep,
                **_round_acc(sess_acc),
            }
        )

    sessions.sort(key=lambda s: float(s.get("last_epoch") or 0), reverse=True)
    for i, s in enumerate(sessions, start=1):
        s["n"] = i
        s.pop("last_epoch", None)

    return {
        "period": period,
        "offset": offset,
        "grain": grain,
        "label": label,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "totals": _round_acc(totals),
        "buckets": [
            {
                "key": b["key"],
                "label": b["label"],
                **_round_acc(b),
            }
            for b in buckets
        ],
        "sessions": sessions,
        "session_count": len(sessions),
    }
