"""Moved from session/aggregate.py — same bodies."""
from __future__ import annotations

import json
import threading
from calendar import monthrange
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from token_telemetry.session.discover import (
    _parse_iso_to_epoch,
    _read_session_summary,
    list_session_dirs,
    pick_session_title,
)
from token_telemetry.session.calc_cache import code_sig, load_calc, save_calc
from token_telemetry.session.period_attr import (
    cached_attr_events,
    clear_attr_mem,
    is_attr_warm,
)
from token_telemetry.pricing.rates import normalize_model_id
from token_telemetry.session.ids import add_uuid, child_ids_from_update
from token_telemetry.session.kind import is_subagent_kind
from token_telemetry.session.subagents import (
    price_child_usage,
    root_subagent_id,
    summary_parent_session_id,
)

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
    elif g in ("session", "sess", "sessions"):
        g = "session"
    allowed = {
        "daily": ("hour", "15m", "session"),
        "weekly": ("hour", "day", "session"),
        "monthly": ("day", "week", "session"),
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
    """Ordered empty buckets covering [start, end). Empty for session grain."""
    period = (period or "daily").lower()
    grain = normalize_grain(period, grain)
    if grain == "session":
        return []
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
