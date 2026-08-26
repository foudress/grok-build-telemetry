"""Group Grok sessions into projects by normalized session cwd."""

from __future__ import annotations

import os
import time
from typing import Any, Optional

from token_telemetry.session.discover import format_age

__all__ = ["group_projects", "normalize_cwd"]


def normalize_cwd(cwd: Optional[str]) -> Optional[str]:
    """Absolute, slash/case-normalized cwd, or None if missing/empty.

    Windows: ``normcase(normpath(abspath(cwd)))``. Trailing slashes and
    mixed ``C:\\`` / ``c:/`` collapse. 8.3 short names expand when the
    path (or a prefix) exists so they share a bucket with the long form.
    """
    if not isinstance(cwd, str):
        return None
    raw = cwd.strip()
    if not raw:
        return None
    try:
        # realpath expands 8.3 / junctions when a prefix exists; abspath
        # is the fallback if the handle lookup is rejected.
        resolved = os.path.realpath(raw)
    except (OSError, ValueError):
        resolved = os.path.abspath(raw)
    if os.name == "nt":
        resolved = _strip_win_ext_prefix(resolved)
    return os.path.normcase(os.path.normpath(resolved))


def _strip_win_ext_prefix(path: str) -> str:
    # GetFinalPathNameByHandle may yield \\?\C:\...; strip so buckets match
    # ordinary abspath results.
    if path.startswith("\\\\?\\UNC\\"):
        return "\\\\" + path[8:]
    if path.startswith("\\\\?\\"):
        return path[4:]
    return path


def _session_epoch(row: dict[str, Any]) -> float:
    try:
        return float(row.get("last_active_epoch") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _folder_label(root: str) -> str:
    trimmed = root.rstrip("\\/")
    name = os.path.basename(trimmed) if trimmed else ""
    return name or root


def group_projects(session_rows: list[dict]) -> list[dict]:
    """Bucket UI session rows by ``normalize_cwd(row['cwd'])``.

    Rows with missing/empty cwd are dropped (no unknown bucket). Projects
    and their ``sessions`` lists are newest-``last_active_epoch`` first.
    """
    buckets: dict[str, list[dict]] = {}
    for row in session_rows:
        if not isinstance(row, dict):
            continue
        key = normalize_cwd(row.get("cwd"))
        if key is None:
            continue
        buckets.setdefault(key, []).append(row)

    now = time.time()
    projects: list[dict] = []
    for root, rows in buckets.items():
        sessions = sorted(rows, key=_session_epoch, reverse=True)
        last_epoch = _session_epoch(sessions[0]) if sessions else 0.0
        projects.append(
            {
                "root": root,
                "label": _folder_label(root),
                "session_count": len(sessions),
                "last_active_epoch": last_epoch,
                "age_label": format_age(max(0.0, now - last_epoch)),
                "sessions": sessions,
            }
        )
    projects.sort(key=lambda p: p["last_active_epoch"], reverse=True)
    return projects
