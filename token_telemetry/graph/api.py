"""HTTP payload helpers for the call-graph sidecar."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Optional

from token_telemetry.graph.activity import parse_updates
from token_telemetry.graph.projects import group_projects, normalize_cwd
from token_telemetry.graph.replay import collect_replay
from token_telemetry.graph.scan import scan_repo
from token_telemetry.session.discover import list_sessions_for_ui

# Tail-cap so live poll / replay stay cheap on huge updates.jsonl.
ACTIVITY_CAP = 400
REPLAY_CAP = 2000
LIVE_SESSION_CAP = 6
_SCAN_TTL = 30.0
_scan_cache: dict[str, tuple[float, dict]] = {}


def projects_payload() -> dict:
    return {"projects": group_projects(list_sessions_for_ui())}


def graph_payload(root: str) -> tuple[int, dict]:
    status, payload = _allowlisted(root)
    if status != 200:
        return status, payload
    key = payload["root"]
    now = time.time()
    hit = _scan_cache.get(key)
    if hit and now - hit[0] < _SCAN_TTL:
        return 200, hit[1]
    data = scan_repo(key)
    _scan_cache[key] = (now, data)
    return 200, data


def sessions_payload(root: str) -> tuple[int, dict]:
    status, payload = _allowlisted(root)
    if status != 200:
        return status, payload
    return 200, {"sessions": payload["sessions"]}


def activity_payload(root: str, session_id: Optional[str]) -> tuple[int, dict]:
    status, payload = _allowlisted(root)
    if status != 200:
        return status, payload
    rows = _session_rows(payload, session_id)
    cwd = payload["root"]
    events: list[dict] = []
    for row in rows:
        path = _updates_path(row)
        if path is None:
            continue
        events.extend(
            parse_updates(
                path,
                cwd=cwd,
                session_id=str(row.get("session_id") or ""),
                agent_name=str(row.get("agent_name") or ""),
            )
        )
    events.sort(key=lambda e: float(e.get("t") or 0))
    return 200, {"events": events[-ACTIVITY_CAP:]}


def rescan_payload(root: str) -> tuple[int, dict]:
    status, payload = _allowlisted(root)
    if status != 200:
        return status, payload
    key = payload["root"]
    _scan_cache.pop(key, None)
    data = scan_repo(key)
    _scan_cache[key] = (time.time(), data)
    return 200, data


def replay_payload(root: str) -> tuple[int, dict]:
    status, payload = _allowlisted(root)
    if status != 200:
        return status, payload
    events = collect_replay(payload["root"], payload["sessions"])
    return 200, {"events": events[-REPLAY_CAP:]}


def _allowlisted(root: str) -> tuple[int, dict]:
    # Only session cwds — arbitrary disk paths are a GitHub footgun.
    if not isinstance(root, str) or not root.strip():
        return 400, {"error": "missing root"}
    key = normalize_cwd(root)
    if key is None:
        return 400, {"error": "missing root"}
    for project in group_projects(list_sessions_for_ui()):
        if key == project["root"]:
            return 200, project
    return 403, {"error": "root not allowlisted"}


def _session_rows(project: dict[str, Any], session_id: Optional[str]) -> list[dict]:
    rows = project.get("sessions") or []
    sid = (session_id or "").strip()
    if not sid:
        return rows[:LIVE_SESSION_CAP]
    wanted = {part.strip() for part in sid.split(",") if part.strip()}
    return [row for row in rows if str(row.get("session_id") or "") in wanted]


def _updates_path(row: dict) -> Path | None:
    raw = row.get("path")
    if not raw:
        return None
    path = Path(str(raw))
    if path.name == "updates.jsonl":
        return path
    return path / "updates.jsonl"
