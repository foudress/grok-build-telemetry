"""Moved from session/aggregate.py — same bodies."""
from __future__ import annotations

import json
import threading
from calendar import monthrange
from datetime import date, datetime, timedelta, timezone
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

# Grok Build billing calendar (Europe/Paris).
# Weekly: Thu 21:59 CEST / 20:59 CET (= 19:59 UTC), from 2026-07-30.
# Monthly: day-30 at that same clock until 2026-10-30; transition to
# 2026-11-15 23:50; then day-15 23:50 each month.
_BILLING_ORIGIN = date(2026, 7, 30)
_BILLING_WEEKLY_UTC_HM = (19, 59)  # Paris 21:59 summer / 20:59 winter
_BILLING_DAY30_LAST = date(2026, 10, 30)
_BILLING_DAY15_START = date(2026, 11, 15)
_BILLING_DAY15_HM = (23, 50)


def _last_sunday(year: int, month: int) -> date:
    last = date(year, month, monthrange(year, month)[1])
    return last - timedelta(days=(last.weekday() - 6) % 7)


def _utc_in_paris_dst(utc: datetime) -> bool:
    """EU DST: last Sunday March 01:00 UTC ≤ t < last Sunday October 01:00 UTC."""
    u = utc.astimezone(timezone.utc)
    y = u.year
    start_d = _last_sunday(y, 3)
    end_d = _last_sunday(y, 10)
    start = datetime(start_d.year, start_d.month, start_d.day, 1, 0, tzinfo=timezone.utc)
    end = datetime(end_d.year, end_d.month, end_d.day, 1, 0, tzinfo=timezone.utc)
    return start <= u < end


def _paris_offset(utc: datetime) -> timedelta:
    return timedelta(hours=2 if _utc_in_paris_dst(utc) else 1)


def _as_paris(dt: datetime) -> datetime:
    """Aware datetime in a fixed CET/CEST offset (no tzdata)."""
    u = dt.astimezone(timezone.utc)
    off = _paris_offset(u)
    name = "CEST" if off == timedelta(hours=2) else "CET"
    return (u + off).replace(tzinfo=timezone(off, name))


def _paris_wall_to_utc(y: int, m: int, d: int, hh: int, mm: int) -> datetime:
    """Convert Europe/Paris wall time to UTC (safe away from 02:00 fold)."""
    # Billing clocks (20:59 / 21:59 / 23:50) never sit on the fold.
    # Probe with winter offset: if that UTC instant is inside DST, use +2.
    winter_utc = datetime(y, m, d, hh, mm, tzinfo=timezone.utc) - timedelta(hours=1)
    if _utc_in_paris_dst(winter_utc):
        return datetime(y, m, d, hh, mm, tzinfo=timezone.utc) - timedelta(hours=2)
    return winter_utc


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


def week_thursday(d: date) -> date:
    """Thursday on or before ``d`` (billing week align)."""
    return d - timedelta(days=(d.weekday() - 3) % 7)


def _billing_label(start: datetime, end: datetime) -> str:
    """Paris wall-clock range for billing windows."""
    a = _as_paris(start)
    b = _as_paris(end)
    return (
        f"{a.strftime('%d %b %H:%M')} – "
        f"{b.strftime('%d %b %Y %H:%M')}"
    )


def _weekly_reset_utc(d: date) -> datetime:
    """Thursday billing reset as aware UTC (19:59)."""
    hh, mm = _BILLING_WEEKLY_UTC_HM
    return datetime(d.year, d.month, d.day, hh, mm, tzinfo=timezone.utc)


def _billing_weekly_window(
    offset: int,
    now: datetime,
) -> tuple[datetime, datetime, str]:
    now_p = _as_paris(now)
    # Thursday on or before today; if still before that day's reset, step back.
    thu = week_thursday(_as_date(now_p))
    cur = _weekly_reset_utc(thu)
    if now_p < _as_paris(cur):
        thu = thu - timedelta(days=7)
        cur = _weekly_reset_utc(thu)
    origin = _weekly_reset_utc(_BILLING_ORIGIN)
    if cur < origin:
        cur = origin
    weeks_from_origin = max(0, int((cur - origin).days // 7))
    off = int(offset or 0)
    if off > 0:
        off = 0
    if off < -weeks_from_origin:
        off = -weeks_from_origin
    start = cur + timedelta(weeks=off)
    end = start + timedelta(days=7)
    return _as_paris(start), _as_paris(end), _billing_label(start, end)


def _day30_reset(y: int, m: int) -> datetime:
    last = monthrange(y, m)[1]
    d = min(30, last)
    hh, mm = _BILLING_WEEKLY_UTC_HM
    return datetime(y, m, d, hh, mm, tzinfo=timezone.utc)


def _day15_reset(y: int, m: int) -> datetime:
    hh, mm = _BILLING_DAY15_HM
    return _paris_wall_to_utc(y, m, 15, hh, mm)


def _billing_monthly_bounds(until: datetime) -> list[datetime]:
    """Ascending reset instants covering origin .. past ``until``."""
    bounds: list[datetime] = []
    y, m = _BILLING_ORIGIN.year, _BILLING_ORIGIN.month
    while True:
        b = _day30_reset(y, m)
        bounds.append(b)
        if date(y, m, min(30, monthrange(y, m)[1])) >= _BILLING_DAY30_LAST:
            break
        m += 1
        if m > 12:
            m = 1
            y += 1
    # Transition end / new-plan start (15 Nov 23:50 Paris).
    bounds.append(_day15_reset(_BILLING_DAY15_START.year, _BILLING_DAY15_START.month))
    y, m = _BILLING_DAY15_START.year, _BILLING_DAY15_START.month + 1
    if m > 12:
        m = 1
        y += 1
    until_u = until.astimezone(timezone.utc)
    # One bound past ``until`` so the open period always has an end.
    while bounds[-1] <= until_u:
        bounds.append(_day15_reset(y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return bounds


def _billing_monthly_window(
    offset: int,
    now: datetime,
) -> tuple[datetime, datetime, str]:
    bounds = _billing_monthly_bounds(now)
    now_u = now.astimezone(timezone.utc)
    idx = 0
    for i in range(len(bounds) - 1):
        if bounds[i] <= now_u < bounds[i + 1]:
            idx = i
            break
    else:
        idx = max(0, len(bounds) - 2)
    idx = max(0, min(idx + int(offset or 0), len(bounds) - 2))
    start, end = bounds[idx], bounds[idx + 1]
    return _as_paris(start), _as_paris(end), _billing_label(start, end)


def period_window(
    period: str,
    offset: int = 0,
    now: Optional[datetime] = None,
    *,
    billing: bool = False,
) -> tuple[datetime, datetime, str]:
    """Inclusive-start / exclusive-end local window + human label."""
    now = _local_now(now)
    tz = now.tzinfo
    today = _as_date(now)
    period = (period or "daily").strip().lower()
    off = int(offset or 0)
    use_billing = bool(billing) and period in ("weekly", "monthly")

    if use_billing and period == "weekly":
        return _billing_weekly_window(off, now)
    if use_billing and period == "monthly":
        return _billing_monthly_window(off, now)

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
    *,
    billing: bool = False,
) -> list[dict[str, Any]]:
    """Ordered empty buckets covering [start, end). Empty for session grain."""
    period = (period or "daily").lower()
    grain = normalize_grain(period, grain)
    if grain == "session":
        return []
    specs: list[dict[str, Any]] = []
    tz = start.tzinfo
    use_billing = bool(billing) and period in ("weekly", "monthly")

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

    # Billing windows often end mid-day (Thu 20:59 / day-15 23:50). Civil
    # months end at midnight, so date(end) already excludes that day; when the
    # end has a clock time, cover that calendar day then clip epochs to `end`.
    def _cover_end_date() -> date:
        end_d = _as_date(end)
        if use_billing and (end.hour or end.minute or end.second or end.microsecond):
            return end_d + timedelta(days=1)
        return end_d

    if grain == "week":
        align = week_thursday if use_billing else week_monday
        d = align(_as_date(start))
        end_cover = _cover_end_date()
        month_start_d = _as_date(start)
        while d < end_cover:
            week_end_d = d + timedelta(days=7)
            clip0 = max(d, month_start_d)
            clip1 = min(week_end_d, end_cover)
            if clip0 < clip1:
                b0 = datetime(clip0.year, clip0.month, clip0.day, tzinfo=tz)
                b1 = datetime(clip1.year, clip1.month, clip1.day, tzinfo=tz)
                b0_ep = max(b0.timestamp(), start.timestamp())
                b1_ep = min(b1.timestamp(), end.timestamp())
                if b0_ep < b1_ep:
                    last = clip1 - timedelta(days=1)
                    if use_billing:
                        key = f"B{d.isoformat()}"
                    else:
                        key = f"W{d.isocalendar()[0]}-{d.isocalendar()[1]:02d}"
                    specs.append(
                        {
                            "key": key,
                            "label": f"{clip0.strftime('%d %b')}–{last.strftime('%d %b')}",
                            "start_epoch": b0_ep,
                            "end_epoch": b1_ep,
                        }
                    )
            d += timedelta(days=7)
        return specs

    # daily bars
    d = _as_date(start)
    end_cover = _cover_end_date()
    while d < end_cover:
        b0 = datetime(d.year, d.month, d.day, tzinfo=tz)
        b1 = b0 + timedelta(days=1)
        b0_ep = max(b0.timestamp(), start.timestamp()) if use_billing else b0.timestamp()
        b1_ep = min(b1.timestamp(), end.timestamp()) if use_billing else b1.timestamp()
        if b0_ep < b1_ep:
            specs.append(
                {
                    "key": d.isoformat(),
                    "label": d.strftime("%a %d"),
                    "start_epoch": b0_ep,
                    "end_epoch": b1_ep,
                }
            )
        d += timedelta(days=1)
    return specs
