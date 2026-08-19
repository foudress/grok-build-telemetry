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
    _parse_iso_to_epoch,
    _read_session_summary,
    list_session_dirs,
    pick_session_title,
)
from token_telemetry.session.calc_cache import load_calc, save_calc
from token_telemetry.session.period_attr import cached_attr_events, clear_attr_mem
from token_telemetry.session.subagents import UUID_RE, price_child_usage


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
        "tokens_reason": 0,
        "cost_in_usd": 0.0,
        "cost_cached_usd": 0.0,
        "cost_out_usd": 0.0,
        "cost_reason_usd": 0.0,
        "official_usd": 0.0,
        "turns": 0,
    }


def _add_priced(acc: dict[str, float], priced: dict[str, Any]) -> None:
    acc["tokens_in"] += int(priced.get("tokens_in") or 0)
    acc["tokens_cached"] += int(priced.get("tokens_cached") or 0)
    acc["tokens_out"] += int(priced.get("tokens_out") or 0)
    acc["tokens_reason"] += int(priced.get("tokens_reason") or 0)
    acc["cost_in_usd"] += float(priced.get("cost_in_usd") or 0)
    acc["cost_cached_usd"] += float(priced.get("cost_cached_usd") or 0)
    acc["cost_out_usd"] += float(priced.get("cost_out_usd") or 0)
    acc["cost_reason_usd"] += float(priced.get("cost_reason_usd") or 0)
    acc["official_usd"] += float(priced.get("official_usd") or 0)
    acc["turns"] += 1


def _round_acc(acc: dict[str, float]) -> dict[str, Any]:
    tin = int(acc.get("tokens_in") or 0)
    tc = int(acc.get("tokens_cached") or 0)
    tout = int(acc.get("tokens_out") or 0)
    tr = int(acc.get("tokens_reason") or 0)
    ci = float(acc.get("cost_in_usd") or 0)
    cc = float(acc.get("cost_cached_usd") or 0)
    co = float(acc.get("cost_out_usd") or 0)
    cr = float(acc.get("cost_reason_usd") or 0)
    tot = float(acc.get("official_usd") or 0) or (ci + cc + co)
    return {
        "tokens_in": tin,
        "tokens_cached": tc,
        "tokens_out": tout,
        "tokens_reason": tr,
        "tokens_all": tin + tc + tout,
        "cost_in_usd": round(ci, 6),
        "cost_cached_usd": round(cc, 6),
        "cost_out_usd": round(co, 6),
        "cost_reason_usd": round(cr, 6),
        "official_usd": round(tot, 6),
        "turns": int(acc.get("turns") or 0),
    }


_SPAWN_HINTS = ("spawn_subagent", "get_command_or_subagent_output")


def _extract_spawned_ids(raw: str) -> list[str]:
    """Child session ids referenced by spawn / wait tools in this file."""
    seen: set[str] = set()
    out: list[str] = []
    for line in raw.splitlines():
        if not any(h in line for h in _SPAWN_HINTS):
            continue
        for m in UUID_RE.finditer(line):
            uid = m.group(0).lower()
            if uid in seen:
                continue
            seen.add(uid)
            out.append(uid)
    return out


_USER_UPDATES = frozenset(
    {"user_message_chunk", "user_message", "user_prompt_submit"}
)


def _build_spans(markers: list[tuple[float, str]]) -> list[dict[str, Any]]:
    """Continuous work / wait spans from first event to last."""
    if not markers:
        return []
    rank = {"user": 0, "evt": 1, "turn": 2}
    markers = sorted(markers, key=lambda x: (x[0], rank.get(x[1], 1)))
    first = markers[0][0]
    last = markers[-1][0]
    spans: list[dict[str, Any]] = []
    work_start: Optional[float] = first
    wait_start: Optional[float] = None
    mode = "work"
    in_user = False
    for ep, kind in markers:
        if kind == "user":
            if not in_user:
                if mode == "wait" and wait_start is not None and ep > wait_start:
                    spans.append({"start": wait_start, "end": ep, "kind": "wait"})
                work_start = ep
                wait_start = None
                mode = "work"
                in_user = True
            continue
        in_user = False
        if kind == "turn":
            ws = work_start if work_start is not None else ep
            if ep > ws:
                spans.append({"start": ws, "end": ep, "kind": "work"})
            wait_start = ep
            work_start = None
            mode = "wait"
    if mode == "work" and work_start is not None and last > work_start:
        spans.append({"start": work_start, "end": last, "kind": "work"})
    if mode == "wait" and wait_start is not None and last > wait_start:
        spans.append({"start": wait_start, "end": last, "kind": "wait"})
    if not spans:
        spans.append({"start": first, "end": max(last, first), "kind": "work"})
    return spans


def _parse_updates(path: Path) -> tuple[list[dict[str, Any]], list[str], list[tuple[float, str]]]:
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return [], [], []
    out: list[dict[str, Any]] = []
    markers: list[tuple[float, str]] = []
    for line in raw.splitlines():
        if not line or line[0] not in "{[":
            continue
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(o, dict):
            continue
        epoch = _event_epoch(o)
        if epoch is None:
            continue
        params = o.get("params") if isinstance(o.get("params"), dict) else {}
        upd = params.get("update") if isinstance(params.get("update"), dict) else {}
        kind = upd.get("sessionUpdate")
        if kind in _USER_UPDATES:
            markers.append((float(epoch), "user"))
        elif kind == "turn_completed":
            markers.append((float(epoch), "turn"))
            usage = upd.get("usage")
            if isinstance(usage, dict):
                priced = price_child_usage(usage)
                if (
                    priced["tokens_in"]
                    or priced["tokens_cached"]
                    or priced["tokens_out"]
                    or priced["official_usd"]
                ):
                    out.append({"epoch": epoch, **priced})
        else:
            markers.append((float(epoch), "evt"))
    return out, _extract_spawned_ids(raw), markers


def _summary_parent_id(summary: dict[str, Any]) -> Optional[str]:
    for key in ("parent_session_id", "parent_id"):
        v = summary.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip().lower()
    info = summary.get("info")
    if isinstance(info, dict):
        for key in ("parent_session_id", "parent_id"):
            v = info.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip().lower()
    return None


def _session_meta(
    session_dir: Path,
) -> tuple[str, Optional[str], Optional[str], Optional[str], Optional[float], Optional[float]]:
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
    title = pick_session_title(
        summary, session_id=session_dir.name, extra=agent, max_len=72
    )
    created = _parse_iso_to_epoch(summary.get("created_at"))
    last_active = _parse_iso_to_epoch(
        summary.get("last_active_at") or summary.get("updated_at")
    )
    return str(title), kind, agent, _summary_parent_id(summary), created, last_active


def _stat_pair(path: Path) -> tuple[float, int]:
    try:
        st = path.stat()
        return float(st.st_mtime), int(st.st_size)
    except OSError:
        return 0.0, 0


def _cached_file(session_dir: Path) -> dict[str, Any]:
    p = session_dir / "updates.jsonl"
    key = str(p)
    mtime, size = _stat_pair(p)
    sum_mtime, sum_size = _stat_pair(session_dir / "summary.json")
    if mtime == 0 and size == 0 and not p.is_file():
        return {
            "mtime": 0,
            "size": 0,
            "sum_mtime": sum_mtime,
            "sum_size": sum_size,
            "turns": [],
            "child_ids": [],
            "spans": [],
            "first_all": None,
            "last_all": None,
            "session_id": session_dir.name,
            "path": str(session_dir),
            "title": pick_session_title({}, session_id=session_dir.name),
            "kind": None,
            "agent_name": None,
            "parent_id": None,
        }
    hit = _file_cache.get(key)
    if (
        hit
        and hit.get("mtime") == mtime
        and hit.get("size") == size
        and hit.get("sum_mtime") == sum_mtime
        and hit.get("sum_size") == sum_size
        and "spans" in hit
    ):
        return hit
    blob = load_calc(session_dir)
    if blob and isinstance(blob.get("agg"), dict) and "spans" in blob["agg"]:
        row = dict(blob["agg"])
        row["mtime"] = mtime
        row["size"] = size
        row["sum_mtime"] = sum_mtime
        row["sum_size"] = sum_size
        row["path"] = str(session_dir)
        row["session_id"] = session_dir.name
        _file_cache[key] = row
        return row
    title, kind, agent, parent_id, created, last_active = _session_meta(session_dir)
    turns, child_ids, markers = _parse_updates(p)
    sid = session_dir.name.lower()
    child_ids = [c for c in child_ids if c != sid]
    spans = _build_spans(markers)
    first_all = created
    last_all = last_active
    if markers:
        first_m = min(m[0] for m in markers)
        last_m = max(m[0] for m in markers)
        first_all = min(first_m, first_all) if first_all else first_m
        last_all = max(last_m, last_all) if last_all else last_m
    row = {
        "mtime": mtime,
        "size": size,
        "sum_mtime": sum_mtime,
        "sum_size": sum_size,
        "turns": turns,
        "child_ids": child_ids,
        "spans": spans,
        "first_all": first_all,
        "last_all": last_all,
        "session_id": session_dir.name,
        "path": str(session_dir),
        "title": title,
        "kind": kind,
        "agent_name": agent,
        "parent_id": parent_id,
    }
    _file_cache[key] = row
    disk_agg = {
        k: row[k]
        for k in (
            "turns",
            "child_ids",
            "spans",
            "first_all",
            "last_all",
            "title",
            "kind",
            "agent_name",
            "parent_id",
        )
    }
    save_calc(session_dir, agg=disk_agg)
    return row


def clear_file_mem() -> None:
    _file_cache.clear()


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


def _order_sessions(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Parents first (recency), children indented under their parent."""
    by_id = {str(s["session_id"]).lower(): s for s in sessions}
    children: dict[str, list[dict[str, Any]]] = {}
    roots: list[dict[str, Any]] = []
    orphans: list[dict[str, Any]] = []
    for s in sessions:
        pid = (s.get("parent_id") or "").lower() or None
        kind = s.get("session_kind")
        if kind == "subagent" and pid and pid in by_id:
            children.setdefault(pid, []).append(s)
        elif kind == "subagent":
            orphans.append(s)
        else:
            roots.append(s)

    def _last(s: dict[str, Any]) -> float:
        return float(s.get("last_epoch") or 0)

    def _first(s: dict[str, Any]) -> float:
        return float(s.get("first_epoch") or s.get("last_epoch") or 0)

    roots.sort(key=_first)
    orphans.sort(key=_first)
    for kids in children.values():
        kids.sort(key=_first)

    ordered: list[dict[str, Any]] = []
    n_parent = 0
    for p in roots:
        n_parent += 1
        p["n"] = n_parent
        p["child_n"] = None
        p["depth"] = 0
        p["label"] = f"Session {n_parent}"
        ordered.append(p)
        pid = str(p["session_id"]).lower()
        for i, c in enumerate(children.get(pid) or [], start=1):
            c["n"] = n_parent
            c["child_n"] = i
            c["depth"] = 1
            c["label"] = f"Sub Agent {i}"
            ordered.append(c)
    if orphans:
        n_parent += 1 if not roots else 0
        # keep orphan subs after known trees
        for i, c in enumerate(orphans, start=1):
            c["n"] = n_parent or 1
            c["child_n"] = i
            c["depth"] = 1
            c["label"] = f"Sub Agent {i}"
            ordered.append(c)
    return ordered


def _place(epoch: float, specs: list[dict[str, Any]]) -> Optional[int]:
    for i, s in enumerate(specs):
        if s["start_epoch"] <= epoch < s["end_epoch"]:
            return i
    return None


def _add_cat_list(dest: dict[str, dict[str, Any]], segs: list[dict[str, Any]]) -> None:
    for s in segs or []:
        key = str(s.get("key") or "")
        if not key:
            continue
        prev = dest.get(key)
        if prev:
            prev["usd"] += float(s.get("usd") or 0)
            prev["tok"] += float(s.get("tok") or 0)
        else:
            dest[key] = {
                "key": key,
                "k": s.get("k") or key,
                "label": s.get("label") or key,
                "usd": float(s.get("usd") or 0),
                "tok": float(s.get("tok") or 0),
            }


def _cats_out(acc: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = [v for v in acc.values() if (v.get("usd") or 0) > 0 or (v.get("tok") or 0) > 0]
    rows.sort(key=lambda x: str(x.get("key") or ""))
    return [
        {
            "key": r["key"],
            "k": r["k"],
            "label": r["label"],
            "usd": round(float(r["usd"]), 8),
            "tok": round(float(r["tok"]), 4),
        }
        for r in rows
    ]


def build_aggregate(
    period: str = "daily",
    offset: int = 0,
    grain: str = "day",
    now: Optional[datetime] = None,
    stack: str = "io",
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
    buckets = [{**s, **_empty_acc(), "_parts": {}, "_tools": {}} for s in specs]
    totals = _empty_acc()
    tot_parts: dict[str, dict[str, Any]] = {}
    tot_tools: dict[str, dict[str, Any]] = {}
    sessions: list[dict[str, Any]] = []
    want_attr = (stack or "io").strip().lower() in ("parts", "tools")

    with _lock:
        files = _scan_files()

    child_to_parent: dict[str, str] = {}
    for row in files:
        pid = str(row.get("session_id") or "").lower()
        if not pid or row.get("kind") == "subagent":
            continue
        for cid in row.get("child_ids") or []:
            child_to_parent[str(cid).lower()] = pid

    for row in files:
        kind = row.get("kind")
        billed = kind != "subagent"
        sess_acc = _empty_acc()
        last_ep = None
        first_ep = None
        for t in row.get("turns") or []:
            ep = t.get("epoch")
            if ep is None or ep < start_e or ep >= end_e:
                continue
            _add_priced(sess_acc, t)
            if last_ep is None or ep > last_ep:
                last_ep = ep
            if first_ep is None or ep < first_ep:
                first_ep = ep
            if billed:
                _add_priced(totals, t)
                idx = _place(float(ep), specs)
                if idx is not None:
                    _add_priced(buckets[idx], t)
        if billed and want_attr:
            try:
                d = Path(row.get("path") or "")
                if not d.is_dir():
                    # recover from updates path
                    d = Path(str(row.get("updates") or ""))
                    if d.is_file():
                        d = d.parent
                if d.is_dir():
                    for ev in cached_attr_events(d):
                        ep = ev.get("epoch")
                        if ep is None or ep < start_e or ep >= end_e:
                            continue
                        idx = _place(float(ep), specs)
                        if idx is None:
                            continue
                        _add_cat_list(buckets[idx]["_parts"], ev.get("parts") or [])
                        _add_cat_list(buckets[idx]["_tools"], ev.get("tools") or [])
                        _add_cat_list(tot_parts, ev.get("parts") or [])
                        _add_cat_list(tot_tools, ev.get("tools") or [])
            except Exception:
                pass
        life0 = row.get("first_all")
        life1 = row.get("last_all")
        if life0 is None:
            life0 = first_ep
        if life1 is None:
            life1 = last_ep
        overlaps = (
            life0 is not None
            and life1 is not None
            and float(life0) < end_e
            and float(life1) >= start_e
        )
        if sess_acc["turns"] <= 0 and not overlaps:
            continue
        clip0 = max(float(life0), start_e) if life0 is not None else start_e
        clip1 = min(float(life1), end_e) if life1 is not None else end_e
        if clip1 < clip0:
            clip1 = clip0
        spans_out: list[dict[str, Any]] = []
        for sp in row.get("spans") or []:
            a = max(float(sp.get("start") or 0), start_e)
            b = min(float(sp.get("end") or 0), end_e)
            if b > a:
                spans_out.append({"start": a, "end": b, "kind": sp.get("kind") or "work"})
        if not spans_out and clip1 > clip0:
            spans_out.append({"start": clip0, "end": clip1, "kind": "work"})
        sid = str(row["session_id"])
        title = row.get("title") or sid[:8]
        parent_id = row.get("parent_id") or child_to_parent.get(sid.lower())
        role = (row.get("agent_name") or "").strip()
        if (
            kind == "subagent"
            and role
            and role.lower() not in ("general-purpose", "general purpose")
            and role not in title
        ):
            title = f"{role} · {title}"
        sessions.append(
            {
                "session_id": sid,
                "title": title,
                "session_kind": kind or "main",
                "agent_name": row.get("agent_name"),
                "parent_id": parent_id,
                "first_epoch": clip0,
                "last_epoch": clip1,
                "spans": spans_out,
                **_round_acc(sess_acc),
            }
        )

    sessions = _order_sessions(sessions)

    return {
        "period": period,
        "offset": offset,
        "grain": grain,
        "label": label,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "totals": {**_round_acc(totals), "parts": _cats_out(tot_parts), "tools": _cats_out(tot_tools)},
        "buckets": [
            {
                "key": b["key"],
                "label": b["label"],
                "start_epoch": b.get("start_epoch"),
                "end_epoch": b.get("end_epoch"),
                **_round_acc(b),
                "parts": _cats_out(b.get("_parts") or {}),
                "tools": _cats_out(b.get("_tools") or {}),
            }
            for b in buckets
        ],
        "sessions": sessions,
        "session_count": len(sessions),
    }
