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
    """Load summary.json (title, last_active_at, …). Empty dict if missing."""
    p = session_dir / "summary.json"
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


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
    Prefer generated_title / session_summary over session id hash.
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

        title = (
            summary.get("generated_title")
            or summary.get("session_summary")
            or meta.get("title")
            or meta.get("name")
            or None
        )
        if isinstance(title, str):
            title = title.strip() or None
        # Fallback: short path basename of cwd, then hash
        cwd = summary.get("info", {}).get("cwd") if isinstance(summary.get("info"), dict) else None
        cwd = cwd or meta.get("cwd")
        if not title and isinstance(cwd, str) and cwd.strip():
            # last path segment as weak label
            title = Path(cwd.replace("\\", "/")).name or cwd
        label = title or sid[:8]
        if isinstance(label, str) and len(label) > 52:
            label = label[:49] + "…"

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
            sub_bit = agent_name or "sub"
            label = f"↳ {sub_bit} · {label}"

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

    # Prefer an active session that still has a fresh updates file
    # (never auto-follow a sub-agent session — those live under the parent)
    active = read_active_session_ids()
    for sid in reversed(active):  # last opened wins
        d = by_id.get(sid)
        if d is not None and not _is_sub(d):
            return d

    mains = [d for d in dirs if not _is_sub(d)]
    pool = mains or dirs
    if not pool:
        return None
    pool.sort(key=lambda d: (d / "updates.jsonl").stat().st_mtime, reverse=True)
    return pool[0]

