"""Session filesystem discovery for the live dashboard."""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any, Optional


SESSIONS_ROOT = Path.home() / ".grok" / "sessions"
ACTIVE_SESSIONS = Path.home() / ".grok" / "active_sessions.json"


def list_session_dirs(root: Path = SESSIONS_ROOT) -> list[Path]:
    if not root.is_dir():
        return []
    out: list[Path] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        if (child / "updates.jsonl").is_file():
            out.append(child)
        else:
            for sub in child.iterdir():
                if sub.is_dir() and (sub / "updates.jsonl").is_file():
                    out.append(sub)
    return out


def read_active_session_ids() -> list[str]:
    if not ACTIVE_SESSIONS.is_file():
        return []
    try:
        data = json.loads(ACTIVE_SESSIONS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [x.get("session_id") for x in data if isinstance(x, dict) and x.get("session_id")]


def read_active_sessions_meta() -> list[dict[str, Any]]:
    """Raw entries from active_sessions.json (session_id + optional labels)."""
    if not ACTIVE_SESSIONS.is_file():
        return []
    try:
        data = json.loads(ACTIVE_SESSIONS.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(data, list):
        return []
    return [x for x in data if isinstance(x, dict) and x.get("session_id")]


def _read_session_summary(session_dir: Path) -> dict[str, Any]:
    """Load summary.json (title, last_active_at, …). Empty dict if missing.

    Grok often writes a UTF-8 BOM; ``utf-8`` alone then fails and callers
    fall back to the session-id prefix.
    """
    p = session_dir / "summary.json"
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


def _nonempty_str(v: Any) -> Optional[str]:
    if isinstance(v, str) and v.strip():
        return v.strip()
    return None


def pick_session_title(
    summary: dict[str, Any],
    *,
    session_id: str,
    cwd: Any = None,
    extra: Any = None,
    max_len: int = 72,
) -> str:
    """Prefer session_summary, then generated_title, then last_turn_summary."""
    title = (
        _nonempty_str(summary.get("session_summary"))
        or _nonempty_str(summary.get("generated_title"))
        or _nonempty_str(summary.get("last_turn_summary"))
        or _nonempty_str(extra)
    )
    if not title:
        info = summary.get("info")
        info_cwd = info.get("cwd") if isinstance(info, dict) else None
        raw_cwd = cwd or info_cwd
        if isinstance(raw_cwd, str) and raw_cwd.strip():
            title = Path(raw_cwd.replace("\\", "/")).name or raw_cwd.strip()
    if not title:
        title = (session_id or "")[:8] or "session"
    if len(title) > max_len:
        title = title[: max_len - 3] + "…"
    return title


def _parse_iso_to_epoch(s: Any) -> Optional[float]:
    """Parse Grok ISO timestamps (often …Z or with fractional seconds)."""
    if not s or not isinstance(s, str):
        return None
    t = s.strip()
    if not t:
        return None
    # fromisoformat handles most; normalize trailing Z
    if t.endswith("Z"):
        t = t[:-1] + "+00:00"
    try:
        from datetime import datetime

        return datetime.fromisoformat(t).timestamp()
    except ValueError:
        return None


def format_age(seconds: float) -> str:
    """Human relative age: 12s, 5m, 3h, 2d."""
    if seconds < 0:
        seconds = 0
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m = s // 60
    if m < 60:
        return f"{m}m"
    h = m // 60
    if h < 48:
        return f"{h}h"
    d = h // 24
    return f"{d}d"


def list_sessions_for_ui() -> list[dict[str, Any]]:
    """
    Sessions for the dashboard dropdown.
    Active sessions first (from active_sessions.json), then other recent dirs.
    Prefer session_summary (then generated_title) over session id hash.
    """
    dirs = list_session_dirs()
    by_id = {d.name: d for d in dirs}
    active_meta = {m["session_id"]: m for m in read_active_sessions_meta()}
    active_ids = read_active_session_ids()
    now = time.time()

    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _row(sid: str, *, active: bool) -> Optional[dict[str, Any]]:
        d = by_id.get(sid)
        if d is None:
            return None
        updates = d / "updates.jsonl"
        try:
            mtime = updates.stat().st_mtime
            size = updates.stat().st_size
        except OSError:
            mtime = 0.0
            size = 0

        summary = _read_session_summary(d)
        meta = active_meta.get(sid) or {}

        cwd = summary.get("info", {}).get("cwd") if isinstance(summary.get("info"), dict) else None
        cwd = cwd or meta.get("cwd")
        title = pick_session_title(
            summary,
            session_id=sid,
            cwd=cwd,
            extra=meta.get("title") or meta.get("name"),
            max_len=52,
        )
        label = title

        last_active_iso = summary.get("last_active_at") or summary.get("updated_at")
        last_epoch = _parse_iso_to_epoch(last_active_iso)
        if last_epoch is None:
            last_epoch = mtime
        age_seconds = max(0.0, now - float(last_epoch or 0))
        age_label = format_age(age_seconds)

        kind = summary.get("session_kind")
        if isinstance(kind, str):
            kind = kind.strip().lower() or None
        else:
            kind = None
        agent_name = summary.get("agent_name")
        if not isinstance(agent_name, str) or not agent_name.strip():
            agent_name = None
        else:
            agent_name = agent_name.strip()
        if kind == "subagent":
            role = (agent_name or "").strip()
            if role.lower() in ("general-purpose", "general purpose"):
                role = ""
            label = f"↳ {role + ' · ' if role else ''}{label}"

        return {
            "session_id": sid,
            "label": str(label),
            "title": title,
            "active": active,
            "mtime": mtime,
            "last_active_at": last_active_iso,
            "last_active_epoch": last_epoch,
            "age_seconds": round(age_seconds, 1),
            "age_label": age_label,
            "updates_bytes": size,
            "path": str(d),
            "cwd": cwd,
            "session_kind": kind,
            "agent_name": agent_name,
        }

    # Active first (stable order from file, last = most recent open)
    for sid in active_ids:
        row = _row(sid, active=True)
        if row:
            out.append(row)
            seen.add(sid)

    # Other sessions with updates, newest first (cap for UI)
    rest = [d for d in dirs if d.name not in seen]
    rest.sort(key=lambda d: (d / "updates.jsonl").stat().st_mtime, reverse=True)
    for d in rest[:30]:
        row = _row(d.name, active=False)
        if row:
            out.append(row)

    return out


def resolve_session_dir(session_id: Optional[str] = None) -> Optional[Path]:
    dirs = list_session_dirs()
    by_id = {d.name: d for d in dirs}

    if session_id:
        return by_id.get(session_id)

    def _is_sub(d: Path) -> bool:
        summary = _read_session_summary(d)
        kind = summary.get("session_kind")
        return isinstance(kind, str) and kind.strip().lower() == "subagent"

    def _recency(d: Path) -> float:
        summary = _read_session_summary(d)
        ep = _parse_iso_to_epoch(
            summary.get("last_active_at") or summary.get("updated_at")
        )
        try:
            mt = (d / "updates.jsonl").stat().st_mtime
        except OSError:
            mt = 0.0
        return max(float(ep or 0), float(mt or 0))

    # Follow the most recently active main session (never a sub-agent).
    active = read_active_session_ids()
    active_mains = []
    for sid in active:
        d = by_id.get(sid)
        if d is not None and not _is_sub(d):
            active_mains.append(d)
    if active_mains:
        active_mains.sort(key=_recency, reverse=True)
        return active_mains[0]

    mains = [d for d in dirs if not _is_sub(d)]
    pool = mains or dirs
    if not pool:
        return None
    pool.sort(key=_recency, reverse=True)
    return pool[0]

