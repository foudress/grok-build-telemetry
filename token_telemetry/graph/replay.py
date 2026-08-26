"""Wave-3 hook: collect time-ordered ActivityEvents from session updates."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Union

from token_telemetry.graph.activity import parse_updates

_SourceItem = Union[str, Path, dict[str, Any]]


def collect_replay(
    root: str | Path,
    session_rows_or_paths: Union[_SourceItem, Iterable[_SourceItem]],
) -> list[dict]:
    """Walk given updates.jsonl paths (or session rows), parse, sort by ``t``."""
    cwd = str(root)
    if isinstance(session_rows_or_paths, (str, Path, dict)):
        items: Iterable[_SourceItem] = (session_rows_or_paths,)
    else:
        items = session_rows_or_paths
    events: list[dict] = []
    for item in items:
        path, sid, name = _resolve_item(item)
        if path is None:
            continue
        events.extend(
            parse_updates(path, cwd=cwd, session_id=sid, agent_name=name)
        )
    events.sort(key=lambda e: float(e.get("t") or 0))
    return events


def _resolve_item(item: _SourceItem) -> tuple[Path | None, str, str]:
    if isinstance(item, dict):
        sid = str(item.get("session_id") or item.get("sid") or "")
        name = str(item.get("agent_name") or item.get("name") or "")
        raw = (
            item.get("updates")
            or item.get("updates_path")
            or item.get("updates.jsonl")
        )
        if raw:
            return Path(str(raw)), sid, name
        base = item.get("path") or item.get("session_dir")
        if not base:
            return None, sid, name
        p = Path(str(base))
        if p.name != "updates.jsonl":
            p = p / "updates.jsonl"
        if not sid:
            sid = p.parent.name
        return p, sid, name
    p = Path(item)
    if p.is_dir():
        return p / "updates.jsonl", p.name, ""
    sid = p.parent.name if p.name == "updates.jsonl" else ""
    return p, sid, ""
