"""Period aggregates (daily / weekly / monthly) from official turn usage.

Parent ``turn_completed`` includes sub-agent bills — those are peeled so I/O
matches Parts/Tools (hierarchy is already parent-only). Subagent dirs are
listed on Session grain but excluded from period totals / time buckets.
"""

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
from token_telemetry.session.subagents import (
    UUID_RE,
    extract_ids_from_text,
    extract_task_ids,
    is_subagent_kind,
    price_child_usage,
    root_subagent_id,
    summary_parent_session_id,
)


_lock = threading.Lock()
# path -> {mtime, size, turns, session_id, title, kind, agent_name}
_file_cache: dict[str, dict[str, Any]] = {}
# Same cap as Session Official $/M card — skip high-ctx turns for rate imply.
RATE_CTX_CAP = 190_000


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
        "estimate_usd": 0.0,
        "turns": 0,
    }


def _add_priced(acc: dict[str, float], priced: dict[str, Any], *, count_turn: bool = True) -> None:
    acc["tokens_in"] += int(priced.get("tokens_in") or 0)
    acc["tokens_cached"] += int(priced.get("tokens_cached") or 0)
    acc["tokens_out"] += int(priced.get("tokens_out") or 0)
    acc["tokens_reason"] += int(priced.get("tokens_reason") or 0)
    acc["cost_in_usd"] += float(priced.get("cost_in_usd") or 0)
    acc["cost_cached_usd"] += float(priced.get("cost_cached_usd") or 0)
    acc["cost_out_usd"] += float(priced.get("cost_out_usd") or 0)
    acc["cost_reason_usd"] += float(priced.get("cost_reason_usd") or 0)
    acc["official_usd"] += float(priced.get("official_usd") or 0)
    est = priced.get("estimate_usd")
    if est is None:
        est = (
            float(priced.get("cost_in_usd") or 0)
            + float(priced.get("cost_cached_usd") or 0)
            + float(priced.get("cost_out_usd") or 0)
        )
    acc["estimate_usd"] += float(est or 0)
    if count_turn:
        acc["turns"] += 1


def _priced_from_io_segs(segs: list[dict[str, Any]]) -> dict[str, Any]:
    """Map period_attr io segs (in/cached/out) onto a priced turn-shaped dict."""
    tin = tc = tout = 0
    ci = cc = co = 0.0
    for s in segs or []:
        if not isinstance(s, dict):
            continue
        k = s.get("k")
        tok = int(round(float(s.get("tok") or 0)))
        usd = float(s.get("usd") or 0)
        if k == "in":
            tin += tok
            ci += usd
        elif k == "cached":
            tc += tok
            cc += usd
        elif k == "out":
            tout += tok
            co += usd
    return {
        "tokens_in": tin,
        "tokens_cached": tc,
        "tokens_out": tout,
        "tokens_reason": 0,
        "cost_in_usd": ci,
        "cost_cached_usd": cc,
        "cost_out_usd": co,
        "cost_reason_usd": 0.0,
        "official_usd": 0.0,
        "estimate_usd": ci + cc + co,
    }


def _round_acc(acc: dict[str, float]) -> dict[str, Any]:
    tin = int(acc.get("tokens_in") or 0)
    tc = int(acc.get("tokens_cached") or 0)
    tout = int(acc.get("tokens_out") or 0)
    tr = int(acc.get("tokens_reason") or 0)
    ci = float(acc.get("cost_in_usd") or 0)
    cc = float(acc.get("cost_cached_usd") or 0)
    co = float(acc.get("cost_out_usd") or 0)
    cr = float(acc.get("cost_reason_usd") or 0)
    off = float(acc.get("official_usd") or 0) or (ci + cc + co)
    est = float(acc.get("estimate_usd") or 0) or (ci + cc + co)
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
        "official_usd": round(off, 6),
        "estimate_usd": round(est, 6),
        "turns": int(acc.get("turns") or 0),
    }


def _tool_link_name(upd: dict[str, Any]) -> str:
    """Structured tool name only — never free-text body dumps."""
    tc = upd.get("toolCall") if isinstance(upd.get("toolCall"), dict) else {}
    meta = upd.get("_meta") if isinstance(upd.get("_meta"), dict) else {}
    xai = meta.get("x.ai/tool") if isinstance(meta.get("x.ai/tool"), dict) else {}
    parts = (
        xai.get("name"),
        upd.get("toolName"),
        tc.get("toolName"),
        upd.get("title"),
        tc.get("title"),
    )
    for p in parts:
        if isinstance(p, str) and p.strip():
            return p.strip().lower()
    return ""


def _is_spawn_or_wait_tool(name: str) -> bool:
    n = (name or "").lower()
    return "spawn_subagent" in n or "get_command_or_subagent_output" in n


def _add_uid(out: list[str], seen: set[str], uid: Any) -> None:
    s = str(uid or "").strip().lower()
    if not s or not UUID_RE.fullmatch(s) or s in seen:
        return
    seen.add(s)
    out.append(s)


def _child_ids_from_update(upd: dict[str, Any]) -> list[str]:
    """Authoritative child ids from subagent_spawned / spawn|wait tool fields.

    Do **not** scrape every UUID on lines that merely mention ``spawn_subagent``
    (skill docs, chat, read_file dumps) — that steals kids across sessions.
    """
    if not isinstance(upd, dict):
        return []
    kind = str(upd.get("sessionUpdate") or "")
    out: list[str] = []
    seen: set[str] = set()
    if kind == "subagent_spawned":
        for key in ("subagent_id", "child_session_id"):
            _add_uid(out, seen, upd.get(key))
        return out
    if kind not in ("tool_call", "tool_call_update"):
        return out
    name = _tool_link_name(upd)
    if not _is_spawn_or_wait_tool(name):
        return out
    tc = upd.get("toolCall") if isinstance(upd.get("toolCall"), dict) else {}
    raw_in = tc.get("rawInput") if isinstance(tc.get("rawInput"), dict) else None
    if raw_in is None:
        raw_in = upd.get("rawInput") if isinstance(upd.get("rawInput"), dict) else {}
    for uid in extract_task_ids(raw_in):
        _add_uid(out, seen, uid)
    # Spawn completion body: "subagent_id: <uuid>" (structured tool only).
    if "spawn_subagent" in name:
        chunks: list[Any] = []
        content = upd.get("content")
        if isinstance(content, list):
            chunks.extend(content)
        elif isinstance(content, str):
            chunks.append(content)
        raw_out = upd.get("rawOutput")
        if raw_out is not None:
            chunks.append(raw_out)
        tc_content = tc.get("content")
        if tc_content is not None:
            chunks.append(tc_content)
        for uid in extract_ids_from_text(*chunks):
            _add_uid(out, seen, uid)
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
    child_ids: list[str] = []
    child_seen: set[str] = set()
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
        for cid in _child_ids_from_update(upd):
            _add_uid(child_ids, child_seen, cid)
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
                    or priced.get("estimate_usd")
                ):
                    out.append({"epoch": epoch, **priced})
        else:
            markers.append((float(epoch), "evt"))
    return out, child_ids, markers


def _summary_parent_id(summary: dict[str, Any]) -> Optional[str]:
    return summary_parent_session_id(summary)


def _session_meta(
    session_dir: Path,
) -> tuple[
    str,
    Optional[str],
    Optional[str],
    Optional[str],
    Optional[float],
    Optional[float],
    Optional[str],
]:
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
    model = normalize_model_id(
        summary.get("current_model_id") or summary.get("model_id")
    )
    return (
        str(title),
        kind,
        agent,
        _summary_parent_id(summary),
        created,
        last_active,
        model,
    )


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
    sig = code_sig()
    if mtime == 0 and size == 0 and not p.is_file():
        return {
            "mtime": 0,
            "size": 0,
            "sum_mtime": sum_mtime,
            "sum_size": sum_size,
            "code": sig,
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
            "model_family": None,
        }
    hit = _file_cache.get(key)
    if (
        hit
        and hit.get("mtime") == mtime
        and hit.get("size") == size
        and hit.get("sum_mtime") == sum_mtime
        and hit.get("sum_size") == sum_size
        and hit.get("code") == sig
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
        row["code"] = sig
        row["path"] = str(session_dir)
        row["session_id"] = session_dir.name
        if not row.get("model_family"):
            row["model_family"] = normalize_model_id(
                (_read_session_summary(session_dir) or {}).get("current_model_id")
            )
        _file_cache[key] = row
        return row
    title, kind, agent, parent_id, created, last_active, model_family = _session_meta(
        session_dir
    )
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
        "code": sig,
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
        "model_family": model_family,
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
            "model_family",
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


def _merge_resume_chain(chain: list[dict[str, Any]]) -> dict[str, Any]:
    """One daily row per agent: keep the latest resume/spawn only (no sum).

    Harness wake/resume creates a new session dir per round. Listing or summing
    every dir duplicates Sub Agent rows and double-counts In/Cached/Out. Parent
    stays the orchestrator (already remapped on each row before merge).
    """
    if not chain:
        return {}
    if len(chain) == 1:
        row = dict(chain[0])
        row["session_kind"] = "subagent"
        return row
    chain = sorted(
        chain, key=lambda c: float(c.get("first_epoch") or c.get("last_epoch") or 0)
    )
    latest = dict(chain[-1])
    # Prefer orchestrator parent_id / root from any earlier node if latest lacks them.
    for c in chain:
        if not latest.get("parent_id") and c.get("parent_id"):
            latest["parent_id"] = c["parent_id"]
        rid = c.get("root_session_id")
        if rid:
            latest["root_session_id"] = rid
            break
    else:
        if not latest.get("root_session_id"):
            latest["root_session_id"] = chain[0].get("session_id")
    latest["session_kind"] = "subagent"
    latest["resume_index"] = 0
    return latest


def _order_sessions(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Parents first; one Sub Agent row per root (latest resume), indented under parent."""
    by_id = {str(s["session_id"]).lower(): s for s in sessions}

    def _first(s: dict[str, Any]) -> float:
        return float(s.get("first_epoch") or s.get("last_epoch") or 0)

    def _root_id(s: dict[str, Any]) -> str:
        sid = str(s.get("session_id") or "").lower()
        if s.get("root_session_id"):
            return str(s["root_session_id"]).lower()
        if not is_subagent_kind(s.get("session_kind")):
            return sid
        pid = (s.get("parent_id") or "").lower()
        # Walk resume → original spawn while parent is also a sub-agent
        cur = sid
        seen: set[str] = set()
        while cur and cur not in seen:
            seen.add(cur)
            node = by_id.get(cur)
            if node is None:
                break
            if str(node.get("session_kind") or "") != "subagent_resume":
                return cur
            nxt = (node.get("parent_id") or "").lower()
            if not nxt or nxt == cur:
                return cur
            # parent_id is orchestrator (not a sub) → this node is the spawn
            pnode = by_id.get(nxt)
            if pnode is not None and not is_subagent_kind(pnode.get("session_kind")):
                return cur
            cur = nxt
        return cur or sid

    roots: list[dict[str, Any]] = []
    children: dict[str, list[dict[str, Any]]] = {}
    orphans: list[dict[str, Any]] = []
    for s in sessions:
        if is_subagent_kind(s.get("session_kind")):
            pid = (s.get("parent_id") or "").lower() or None
            if pid and pid in by_id and not is_subagent_kind(
                (by_id[pid] or {}).get("session_kind")
            ):
                children.setdefault(pid, []).append(s)
            else:
                orphans.append(s)
        else:
            roots.append(s)

    roots.sort(key=_first)
    orphans.sort(key=_first)
    for kids in children.values():
        kids.sort(key=_first)

    def _label_kids(kids: list[dict[str, Any]], n_parent: int) -> list[dict[str, Any]]:
        groups: dict[str, list[dict[str, Any]]] = {}
        order: list[str] = []
        for c in kids:
            rid = _root_id(c)
            c["root_session_id"] = rid
            if rid not in groups:
                groups[rid] = []
                order.append(rid)
            groups[rid].append(c)
        out: list[dict[str, Any]] = []
        for i, rid in enumerate(order, start=1):
            chain = groups[rid]
            chain.sort(key=_first)
            merged = _merge_resume_chain(chain)
            merged["n"] = n_parent
            merged["child_n"] = i
            merged["resume_index"] = 0
            merged["depth"] = 1
            merged["label"] = f"Sub Agent {i}"
            out.append(merged)
        return out

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
        ordered.extend(_label_kids(children.get(pid) or [], n_parent))
    if orphans:
        n_parent = n_parent or 1
        ordered.extend(_label_kids(orphans, n_parent))
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


_PEEL_KEYS = (
    "tokens_in",
    "tokens_cached",
    "tokens_out",
    "tokens_reason",
    "cost_in_usd",
    "cost_cached_usd",
    "cost_out_usd",
    "cost_reason_usd",
    "official_usd",
    "estimate_usd",
)


def _acc_from_session_row(s: dict[str, Any]) -> dict[str, float]:
    acc = _empty_acc()
    for k in _PEEL_KEYS:
        if k.startswith("tokens_") or k == "turns":
            acc[k] = int(s.get(k) or 0)
        else:
            acc[k] = float(s.get(k) or 0)
    acc["turns"] = int(s.get("turns") or 0)
    return acc


def _sub_acc(dest: dict[str, float], src: dict[str, float]) -> None:
    """Subtract child bill from parent (floor 0). Turns stay on the parent."""
    for k in _PEEL_KEYS:
        if k == "turns":
            continue
        if k.startswith("tokens_"):
            dest[k] = max(0, int(dest.get(k) or 0) - int(src.get(k) or 0))
        else:
            dest[k] = max(0.0, float(dest.get(k) or 0) - float(src.get(k) or 0))


def _apply_acc_to_session_row(s: dict[str, Any], acc: dict[str, float]) -> None:
    rounded = _round_acc(acc)
    for k, v in rounded.items():
        s[k] = v


def _peel_parent_rows(sessions: list[dict[str, Any]]) -> None:
    """Parent turn_completed includes sub bills — subtract linked latest kids."""
    for p in sessions:
        if is_subagent_kind(p.get("session_kind")) or int(p.get("depth") or 0) > 0:
            continue
        pid = str(p.get("session_id") or "").lower()
        if not pid:
            continue
        pac = _acc_from_session_row(p)
        for c in sessions:
            if int(c.get("depth") or 0) <= 0 and not is_subagent_kind(
                c.get("session_kind")
            ):
                continue
            if str(c.get("parent_id") or "").lower() != pid:
                continue
            _sub_acc(pac, _acc_from_session_row(c))
        _apply_acc_to_session_row(p, pac)


def _scale_turn_to_peeled(
    turn: dict[str, Any],
    raw: dict[str, float],
    peeled: dict[str, float],
) -> dict[str, Any]:
    """Scale one parent turn so session-sum matches peeled parent-only totals."""
    out = dict(turn)
    for k in _PEEL_KEYS:
        if k == "turns":
            continue
        r = float(raw.get(k) or 0)
        p = float(peeled.get(k) or 0)
        if r > 0:
            out[k] = (float(turn.get(k) or 0) * p / r)
        else:
            out[k] = 0 if k.startswith("tokens_") else 0.0
    if any(k.startswith("tokens_") for k in _PEEL_KEYS):
        for k in (
            "tokens_in",
            "tokens_cached",
            "tokens_out",
            "tokens_reason",
        ):
            out[k] = int(round(float(out.get(k) or 0)))
    return out


def build_aggregate(
    period: str = "daily",
    offset: int = 0,
    grain: str = "day",
    now: Optional[datetime] = None,
    stack: str = "io",
    rate: bool = False,
    on_progress: Optional[Callable[[int, int], None]] = None,
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
    session_grain = grain == "session"
    specs = _bucket_specs(period, start, end, grain)
    buckets = (
        []
        if session_grain
        else [{**s, **_empty_acc(), "_parts": {}, "_tools": {}} for s in specs]
    )
    totals = _empty_acc()
    tot_parts: dict[str, dict[str, Any]] = {}
    tot_tools: dict[str, dict[str, Any]] = {}
    sessions: list[dict[str, Any]] = []
    sess_parts: dict[str, dict[str, dict[str, Any]]] = {}
    sess_tools: dict[str, dict[str, dict[str, Any]]] = {}
    # Recap/compact I/O sides (not in turn_completed) — applied after peel.
    sess_side_io: dict[str, list[dict[str, Any]]] = {}
    # In-window turns kept until after parent peel (I/O includes sub bills).
    sess_turns: dict[str, list[dict[str, Any]]] = {}
    raw_sess_acc: dict[str, dict[str, float]] = {}
    want_rate = bool(rate)
    # `stack` is kept for API compat; Parts/Tools cats are always filled when
    # attr runs so the client can switch I/O↔Parts↔Tools without a refetch.
    # Attr = HierarchyBuilder (cold) or calc-cache hit. I/O needs recap/compact
    # sides. Subs are attr'd only when rate=1 or session grain — lighter D/W/M entry.

    with _lock:
        files = _scan_files()

    # First parent (by session start) that authoritatively spawned/waited a
    # child wins — never overwrite (later chats/skill dumps re-mention UUIDs).
    child_to_parent: dict[str, str] = {}
    mains_for_link = [
        r for r in files if not is_subagent_kind(r.get("kind"))
    ]
    mains_for_link.sort(
        key=lambda r: float(r.get("first_all") or r.get("last_all") or 0)
    )
    for row in mains_for_link:
        pid = str(row.get("session_id") or "").lower()
        if not pid:
            continue
        for cid in row.get("child_ids") or []:
            c = str(cid).lower()
            if c and c not in child_to_parent:
                child_to_parent[c] = pid

    def _turns_in_window(row: dict[str, Any]) -> int:
        n = 0
        for t in row.get("turns") or []:
            ep = t.get("epoch")
            if ep is None or ep < start_e or ep >= end_e:
                continue
            n += 1
        return n

    progress_total = sum(1 for row in files if _turns_in_window(row) > 0)
    progress_done = 0

    def _session_dir(row: dict[str, Any]) -> Optional[Path]:
        d = Path(row.get("path") or "")
        if not d.is_dir():
            d = Path(str(row.get("updates") or ""))
            if d.is_file():
                d = d.parent
        return d if d.is_dir() else None

    # How many sessions will rebuild attr (cache miss → HierarchyBuilder + save).
    cold = 0
    for row in files:
        if _turns_in_window(row) <= 0:
            continue
        billed = not is_subagent_kind(row.get("kind"))
        if not (billed or want_rate or session_grain):
            continue
        d = _session_dir(row)
        if d is not None and not is_attr_warm(d):
            cold += 1

    def _emit_progress(done: int, total: int, *, cold_n: Optional[int] = None) -> None:
        if on_progress is None:
            return
        try:
            if cold_n is None:
                on_progress(done, total)
            else:
                on_progress(done, total, cold=cold_n)
        except TypeError:
            try:
                on_progress(done, total)
            except Exception:
                pass
        except Exception:
            pass

    _emit_progress(0, progress_total, cold_n=cold)

    for row in files:
        kind = row.get("kind")
        billed = not is_subagent_kind(kind)
        sess_acc = _empty_acc()
        last_ep = None
        first_ep = None
        sid = str(row["session_id"])
        sid_l = sid.lower()
        in_turns: list[dict[str, Any]] = []
        for t in row.get("turns") or []:
            ep = t.get("epoch")
            if ep is None or ep < start_e or ep >= end_e:
                continue
            _add_priced(sess_acc, t)
            in_turns.append(t)
            if last_ep is None or ep > last_ep:
                last_ep = ep
            if first_ep is None or ep < first_ep:
                first_ep = ep
        # Drop title-only / no-bill sessions before attr — previously every
        # main paid HierarchyBuilder/disk attr even when outside the window.
        if int(sess_acc.get("turns") or 0) <= 0:
            continue
        sess_round_tps: list[dict[str, Any]] = []
        # Mains always (I/O sides + cats); subs when tok/s or session grain
        # (session bars need per-row Parts/Tools without a second fetch).
        need_attr = billed or want_rate or session_grain
        if need_attr:
            try:
                d = _session_dir(row)
                if d is not None:
                    for ev in cached_attr_events(d):
                        ep = ev.get("epoch")
                        if ep is None or ep < start_e or ep >= end_e:
                            continue
                        # tok/s points (mains always when attr'd; subs when rate).
                        if ev.get("tps") is not None:
                            sess_round_tps.append({
                                "epoch": float(ep),
                                "v": float(ev["tps"]),
                                "round": ev.get("round"),
                                "gen_ms": ev.get("gen_ms"),
                                "gen_out_tokens": ev.get("gen_out_tokens"),
                            })
                        io_segs = ev.get("io") or []
                        if billed and io_segs:
                            priced_io = _priced_from_io_segs(io_segs)
                            if (
                                int(priced_io.get("tokens_in") or 0)
                                or int(priced_io.get("tokens_cached") or 0)
                                or int(priced_io.get("tokens_out") or 0)
                                or float(priced_io.get("estimate_usd") or 0)
                            ):
                                sess_side_io.setdefault(sid_l, []).append({
                                    "epoch": float(ep),
                                    "priced": priced_io,
                                })
                        # Always fold Parts+Tools once attr is paid (client stack switch).
                        parts = ev.get("parts") or []
                        tools = ev.get("tools") or []
                        if session_grain:
                            _add_cat_list(
                                sess_parts.setdefault(sid_l, {}), parts
                            )
                            _add_cat_list(
                                sess_tools.setdefault(sid_l, {}), tools
                            )
                        else:
                            if not billed:
                                continue
                            idx = _place(float(ep), specs)
                            if idx is None:
                                continue
                            _add_cat_list(buckets[idx]["_parts"], parts)
                            _add_cat_list(buckets[idx]["_tools"], tools)
                            _add_cat_list(tot_parts, parts)
                            _add_cat_list(tot_tools, tools)
            except Exception:
                pass
        life0 = row.get("first_all")
        life1 = row.get("last_all")
        if life0 is None:
            life0 = first_ep
        if life1 is None:
            life1 = last_ep
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
        title = row.get("title") or sid[:8]
        # Mains keep summary parent only (never scrape — UUID chatter is noise).
        parent_id = row.get("parent_id")
        root_id = None
        if is_subagent_kind(kind):
            raw_path = str(row.get("path") or "")
            root_id = root_subagent_id(
                sid.lower(),
                parent_dir=Path(raw_path) if raw_path else None,
            )
            orch = child_to_parent.get(root_id) or child_to_parent.get(sid_l)
            if orch:
                parent_id = orch
            elif not parent_id:
                parent_id = child_to_parent.get(sid_l)
        role = (row.get("agent_name") or "").strip()
        if (
            is_subagent_kind(kind)
            and role
            and role.lower() not in ("general-purpose", "general purpose")
            and role not in title
        ):
            title = f"{role} · {title}"
        sess_tps = None
        if sess_round_tps:
            sess_tps = round(
                sum(x["v"] for x in sess_round_tps) / len(sess_round_tps),
                3,
            )
        sess_turns[sid_l] = in_turns
        raw_sess_acc[sid_l] = dict(sess_acc)
        model_family = row.get("model_family")
        if not model_family:
            model_family = normalize_model_id(
                (_read_session_summary(Path(row.get("path") or "")) or {}).get(
                    "current_model_id"
                )
            )
        sessions.append(
            {
                "session_id": sid,
                "title": title,
                "session_kind": kind or "main",
                "agent_name": row.get("agent_name"),
                "parent_id": parent_id,
                "root_session_id": root_id,
                "model_family": model_family,
                "first_epoch": clip0,
                "last_epoch": clip1,
                "spans": spans_out,
                "gen_tokens_per_sec": sess_tps,
                "_tps_rounds": sess_round_tps,
                **_round_acc(sess_acc),
            }
        )
        progress_done += 1
        _emit_progress(progress_done, progress_total, cold_n=cold)

    sessions = _order_sessions(sessions)
    # Parent API bill includes subs — peel latest linked kids so I/O matches
    # Parts/Tools (hierarchy already parent-only) and session rows.
    _peel_parent_rows(sessions)

    # Rebuild period totals / time buckets from peeled parent turns only.
    totals = _empty_acc()
    if not session_grain:
        for b in buckets:
            for k in _empty_acc():
                b[k] = 0 if k == "turns" or k.startswith("tokens_") else 0.0
    by_sid = {str(s.get("session_id") or "").lower(): s for s in sessions}
    for sid_l, turns in sess_turns.items():
        s = by_sid.get(sid_l)
        if s is None:
            continue
        if is_subagent_kind(s.get("session_kind")) or int(s.get("depth") or 0) > 0:
            continue
        raw = raw_sess_acc.get(sid_l) or _empty_acc()
        peeled = _acc_from_session_row(s)
        for t in turns:
            pt = _scale_turn_to_peeled(t, raw, peeled)
            _add_priced(totals, pt)
            if not session_grain:
                ep = t.get("epoch")
                if ep is None:
                    continue
                idx = _place(float(ep), specs)
                if idx is not None:
                    _add_priced(buckets[idx], pt)

    # Stamp API-only tok (turn_completed, peeled) before recap/compact sides
    # inflate tokens_* — used by I/O $/M Official reverse-engineer.
    for s in sessions:
        if is_subagent_kind(s.get("session_kind")) or int(s.get("depth") or 0) > 0:
            continue
        s["api_tokens_in"] = int(s.get("tokens_in") or 0)
        s["api_tokens_cached"] = int(s.get("tokens_cached") or 0)
        s["api_tokens_out"] = int(s.get("tokens_out") or 0)

    # I/O $/M rate pool: Official + API tok from turns with ctx ≤190k only
    # (aligns with Session cost Official subline; avoids high-tier mix).
    for sid_l, turns in sess_turns.items():
        s = by_sid.get(sid_l)
        if s is None:
            continue
        if is_subagent_kind(s.get("session_kind")) or int(s.get("depth") or 0) > 0:
            continue
        raw = raw_sess_acc.get(sid_l) or _empty_acc()
        peeled = {
            "tokens_in": int(s.get("api_tokens_in") or 0),
            "tokens_cached": int(s.get("api_tokens_cached") or 0),
            "tokens_out": int(s.get("api_tokens_out") or 0),
            "tokens_reason": int(s.get("tokens_reason") or 0),
            "cost_in_usd": float(s.get("cost_in_usd") or 0),
            "cost_cached_usd": float(s.get("cost_cached_usd") or 0),
            "cost_out_usd": float(s.get("cost_out_usd") or 0),
            "cost_reason_usd": float(s.get("cost_reason_usd") or 0),
            "official_usd": float(s.get("official_usd") or 0),
            "estimate_usd": float(s.get("estimate_usd") or 0),
            "turns": int(s.get("turns") or 0),
        }
        rate_acc = _empty_acc()
        n_rate = 0
        for t in turns:
            pt = _scale_turn_to_peeled(t, raw, peeled)
            try:
                ctx = int(
                    pt.get("context_tokens_for_tier")
                    or t.get("context_tokens_for_tier")
                    or 0
                )
            except (TypeError, ValueError):
                ctx = 0
            if ctx > RATE_CTX_CAP:
                continue
            if not (float(pt.get("official_usd") or 0) > 0):
                continue
            if not (
                int(pt.get("tokens_in") or 0)
                or int(pt.get("tokens_cached") or 0)
                or int(pt.get("tokens_out") or 0)
            ):
                continue
            _add_priced(rate_acc, pt, count_turn=False)
            n_rate += 1
        if n_rate > 0:
            s["rate_official_usd"] = round(float(rate_acc.get("official_usd") or 0), 6)
            s["rate_tokens_in"] = int(rate_acc.get("tokens_in") or 0)
            s["rate_tokens_cached"] = int(rate_acc.get("tokens_cached") or 0)
            s["rate_tokens_out"] = int(rate_acc.get("tokens_out") or 0)
            s["rate_turns"] = n_rate
        else:
            # No low-ctx turns — fall back to full peeled API bill.
            s["rate_official_usd"] = float(s.get("official_usd") or 0)
            s["rate_tokens_in"] = int(s.get("api_tokens_in") or 0)
            s["rate_tokens_cached"] = int(s.get("api_tokens_cached") or 0)
            s["rate_tokens_out"] = int(s.get("api_tokens_out") or 0)
            s["rate_turns"] = 0

    # Recap/compact I/O after peel (keep turn scaling turn-only).
    for sid_l, sides in sess_side_io.items():
        s = by_sid.get(sid_l)
        if s is None:
            continue
        if is_subagent_kind(s.get("session_kind")) or int(s.get("depth") or 0) > 0:
            continue
        sac = _acc_from_session_row(s)
        for side in sides:
            priced = side.get("priced") or {}
            _add_priced(sac, priced, count_turn=False)
            _add_priced(totals, priced, count_turn=False)
            if not session_grain:
                ep = side.get("epoch")
                if ep is None:
                    continue
                idx = _place(float(ep), specs)
                if idx is not None:
                    _add_priced(buckets[idx], priced, count_turn=False)
        _apply_acc_to_session_row(s, sac)

    # Session-grain parent parts/tools → period totals (subs listed, not totaled).
    if session_grain:
        tot_parts = {}
        tot_tools = {}
        for s in sessions:
            if is_subagent_kind(s.get("session_kind")) or int(s.get("depth") or 0) > 0:
                continue
            sid_l = str(s.get("session_id") or "").lower()
            for cat in _cats_out(sess_parts.get(sid_l) or {}):
                _add_cat_list(tot_parts, [cat])
            for cat in _cats_out(sess_tools.get(sid_l) or {}):
                _add_cat_list(tot_tools, [cat])

    if session_grain:
        buckets = []
        for i, s in enumerate(sessions, start=1):
            sid_l = str(s.get("session_id") or "").lower()
            # X labels: main = N, sub = N.M (parent session · agent). Order unchanged.
            n_parent = s.get("n")
            child_n = s.get("child_n")
            is_sub = int(s.get("depth") or 0) > 0 or is_subagent_kind(
                s.get("session_kind")
            )
            if is_sub and n_parent is not None and child_n is not None:
                x_label = f"{n_parent}.{child_n}"
            elif n_parent is not None:
                x_label = str(n_parent)
            else:
                x_label = str(i)
            buckets.append(
                {
                    "key": f"sess:{sid_l or i}",
                    "label": x_label,
                    "start_epoch": s.get("first_epoch"),
                    "end_epoch": s.get("last_epoch"),
                    "session_id": s.get("session_id"),
                    "session_label": s.get("label") or s.get("title"),
                    "tokens_in": int(s.get("tokens_in") or 0),
                    "tokens_cached": int(s.get("tokens_cached") or 0),
                    "tokens_out": int(s.get("tokens_out") or 0),
                    "tokens_reason": int(s.get("tokens_reason") or 0),
                    "tokens_all": int(s.get("tokens_all") or 0),
                    "cost_in_usd": float(s.get("cost_in_usd") or 0),
                    "cost_cached_usd": float(s.get("cost_cached_usd") or 0),
                    "cost_out_usd": float(s.get("cost_out_usd") or 0),
                    "cost_reason_usd": float(s.get("cost_reason_usd") or 0),
                    "official_usd": float(s.get("official_usd") or 0),
                    "estimate_usd": float(s.get("estimate_usd") or 0),
                    "turns": int(s.get("turns") or 0),
                    "parts": _cats_out(sess_parts.get(sid_l) or {}),
                    "tools": _cats_out(sess_tools.get(sid_l) or {}),
                }
            )

    tot_rounded = _round_acc(totals)
    tot_parts_out = _cats_out(tot_parts)
    tot_tools_out = _cats_out(tot_tools)

    tps_rounds: list[dict[str, Any]] = []
    tps_sessions: list[dict[str, Any]] = []
    for s in sessions:
        rounds = s.pop("_tps_rounds", None) or []
        # X labels match Session grain: main = N, sub = N.M (no "Session"/"Round" words).
        n_parent = s.get("n")
        child_n = s.get("child_n")
        is_sub = int(s.get("depth") or 0) > 0 or is_subagent_kind(
            s.get("session_kind")
        )
        if is_sub and n_parent is not None and child_n is not None:
            x_sess = f"{n_parent}.{child_n}"
        elif n_parent is not None:
            x_sess = str(n_parent)
        else:
            x_sess = str(s.get("label") or s.get("title") or "?")
        for rp in rounds:
            rn = rp.get("round") or "?"
            tps_rounds.append({
                "epoch": rp["epoch"],
                "v": rp["v"],
                "round": rp.get("round"),
                "gen_ms": rp.get("gen_ms"),
                "gen_out_tokens": rp.get("gen_out_tokens"),
                "session_id": s["session_id"],
                "n": n_parent,
                "child_n": child_n,
                "depth": s.get("depth") or 0,
                "label": f"{x_sess} R{rn}",
            })
        if s.get("gen_tokens_per_sec") is not None:
            tps_sessions.append({
                "epoch": s.get("last_epoch") or s.get("first_epoch"),
                "v": s["gen_tokens_per_sec"],
                "session_id": s["session_id"],
                "n": n_parent,
                "child_n": child_n,
                "depth": s.get("depth") or 0,
                "label": x_sess,
            })
    tps_rounds.sort(key=lambda x: (x.get("epoch") or 0, x.get("round") or 0))
    tps_sessions.sort(key=lambda x: (
        x.get("epoch") or 0,
        x.get("n") or 0,
        x.get("child_n") or 0,
    ))

    return {
        "period": period,
        "offset": offset,
        "grain": grain,
        "label": label,
        "start": start.isoformat(),
        "end": end.isoformat(),
        # cats always filled when mains were attr'd; rate_full ⇒ subs included in tps.
        "cats_ready": True,
        "rate_full": bool(want_rate),
        "totals": {**tot_rounded, "parts": tot_parts_out, "tools": tot_tools_out},
        "buckets": (
            buckets
            if session_grain
            else [
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
            ]
        ),
        "sessions": sessions,
        "session_count": len(sessions),
        "tps_rounds": tps_rounds,
        "tps_sessions": tps_sessions,
    }
