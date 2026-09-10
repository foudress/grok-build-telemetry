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
from token_telemetry.session.usage import (
    price_child_usage,
    root_subagent_id,
    summary_parent_session_id,
)

_lock = threading.Lock()
_file_cache: dict[str, dict[str, Any]] = {}
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

def _child_ids_from_update(upd: dict[str, Any]) -> list[str]:
    """Strict harvest — see ``session.ids.child_ids_from_update``."""
    return child_ids_from_update(upd)

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
            add_uuid(child_ids, child_seen, cid)
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
    # Tests patch ``aggregate.list_session_dirs``; resolve at call time.
    from token_telemetry.session import aggregate as _agg

    dirs_fn = getattr(_agg, "list_session_dirs", list_session_dirs)
    for d in dirs_fn():
        p = str(d / "updates.jsonl")
        live.add(p)
        rows.append(_cached_file(d))
    for stale in list(_file_cache.keys()):
        if stale not in live:
            _file_cache.pop(stale, None)
    return rows
