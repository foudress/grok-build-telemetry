"""Parse Grok updates.jsonl tool events into ActivityEvent dicts."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable, Union

# Tools that never map onto a repo file node.
_IGNORE = frozenset(
    {
        "web_search",
        "web_fetch",
        "open_page",
        "todo_write",
        "spawn_subagent",
    }
)
_IGNORE_PREFIXES = ("x_", "image_")

_KIND_BY_TOOL = {
    "read_file": "read",
    "Read": "read",
    "write": "write",
    "search_replace": "write",
    "Write": "write",
    "grep": "search",
    "search_codebase": "search",
    "search_tool": "search",
    "list_dir": "list",
    "run_terminal_command": "exec",
}

_TOOL_FROM_KIND = {
    "read": "read_file",
    "edit": "search_replace",
    "search": "grep",
    "execute": "run_terminal_command",
}

_Source = Union[str, Path, Iterable[dict[str, Any]]]


def parse_updates(
    jsonl_path: _Source,
    *,
    cwd: str,
    session_id: str,
    agent_name: str = "",
) -> list[dict]:
    """Parse tool events from a jsonl path or an iterable of already-parsed dicts.

    Each event: ``{t, tool, path, kind, sid, name}``.
    """
    events: list[dict] = []
    seen: set[str] = set()
    cwd_s = str(cwd or "")
    sid = str(session_id or "")
    name = agent_name or ""
    for rec in _iter_records(jsonl_path):
        ev = _event_from_record(rec, cwd=cwd_s, sid=sid, name=name)
        if ev is None:
            continue
        tid = ev.pop("_tid", "")
        if tid:
            if tid in seen:
                continue
            seen.add(tid)
        events.append(ev)
    return events


def _iter_records(source: _Source) -> Iterable[dict[str, Any]]:
    if isinstance(source, (str, Path)):
        path = Path(source)
        try:
            raw = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return
        for line in raw.splitlines():
            if not line or line[0] not in "{[":
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                yield obj
        return
    for obj in source:
        if isinstance(obj, dict):
            yield obj


def _unwrap_update(rec: dict[str, Any]) -> dict[str, Any]:
    params = rec.get("params")
    if isinstance(params, dict):
        upd = params.get("update")
        if isinstance(upd, dict):
            return upd
    return rec


def _tool_name(update: dict[str, Any]) -> str | None:
    um = update.get("_meta") or {}
    xai = um.get("x.ai/tool") if isinstance(um, dict) else None
    if isinstance(xai, dict) and xai.get("name"):
        return str(xai["name"])
    title = update.get("title")
    if isinstance(title, str) and title:
        if title.startswith("Read `") or title.startswith("Read "):
            return "read_file"
        if title.startswith("Write `") or title.startswith("Write "):
            return "write"
        if title.startswith("Search") or "grep" in title.lower():
            return "grep"
        if title.startswith("List ") or title.startswith("List `"):
            return "list_dir"
        if title.startswith("Execute `") or title.startswith("Execute "):
            return "run_terminal_command"
    kind = update.get("kind")
    if isinstance(kind, str) and kind in _TOOL_FROM_KIND:
        return _TOOL_FROM_KIND[kind]
    return None


def _pick_path(update: dict[str, Any], tool: str | None) -> Any:
    raw_in = update.get("rawInput") or {}
    path = None
    if isinstance(raw_in, dict):
        path = raw_in.get("target_file") or raw_in.get("path") or raw_in.get("file_path")
    um = update.get("_meta") or {}
    xai = um.get("x.ai/tool") if isinstance(um, dict) else None
    if isinstance(xai, dict):
        inp = xai.get("input") or {}
        if isinstance(inp, dict):
            path = path or inp.get("path") or inp.get("target_file") or inp.get("file_path")
            tool = xai.get("name") or tool
    if not path and isinstance(update.get("rawOutput"), dict):
        ea = update["rawOutput"].get("EditsApplied")
        if isinstance(ea, dict):
            path = ea.get("absolute_path") or ea.get("file_path") or path
    # list_dir uses target_directory, not the read/write keys above
    if not path and isinstance(raw_in, dict):
        path = raw_in.get("target_directory")
    if not path and isinstance(xai, dict):
        inp = xai.get("input") or {}
        if isinstance(inp, dict):
            path = inp.get("target_directory")
    return path, tool


def _norm_path(path: str, cwd: str) -> str:
    raw = str(path).strip()
    if not raw:
        return ""
    cwd_n = os.path.normpath(cwd) if cwd else ""
    if cwd_n and not os.path.isabs(raw):
        joined = os.path.normpath(os.path.join(cwd_n, raw))
    else:
        joined = os.path.normpath(raw)
    if not cwd_n:
        return joined.replace("\\", "/") if not os.path.isabs(joined) else joined
    try:
        rel = os.path.relpath(joined, cwd_n)
    except ValueError:
        return joined
    if rel == os.curdir:
        return "."
    if not rel.startswith("..") and not os.path.isabs(rel):
        return rel.replace("\\", "/")
    return joined


def _event_t(rec: dict[str, Any], update: dict[str, Any]) -> float:
    params = rec.get("params") if isinstance(rec.get("params"), dict) else {}
    blobs: list[Any] = [update, rec, params]
    um = update.get("_meta")
    if isinstance(um, dict):
        blobs.append(um)
    pm = params.get("_meta") if isinstance(params, dict) else None
    if isinstance(pm, dict):
        blobs.append(pm)
    for obj in blobs:
        if not isinstance(obj, dict):
            continue
        for key in ("timestamp", "ts"):
            v = obj.get(key)
            if isinstance(v, (int, float)):
                t = float(v)
                return t / 1000.0 if t > 1e12 else t
        if "_meta" in obj and obj is not um and obj is not pm:
            meta = obj.get("_meta")
            if isinstance(meta, dict):
                v = meta.get("timestamp") or meta.get("ts")
                if isinstance(v, (int, float)):
                    t = float(v)
                    return t / 1000.0 if t > 1e12 else t
    return 0.0


def _ignored(tool: str) -> bool:
    if tool in _IGNORE:
        return True
    return tool.startswith(_IGNORE_PREFIXES)


def _event_from_record(
    rec: dict[str, Any],
    *,
    cwd: str,
    sid: str,
    name: str,
) -> dict | None:
    update = _unwrap_update(rec)
    su = update.get("sessionUpdate")
    if su and su not in ("tool_call", "tool_call_update"):
        return None
    tool = _tool_name(update)
    path, tool = _pick_path(update, tool)
    if not tool or _ignored(str(tool)):
        return None
    kind = _KIND_BY_TOOL.get(str(tool))
    if kind is None:
        return None
    path_s = _norm_path(str(path), cwd) if path else ""
    if kind != "exec" and not path_s:
        return None
    tid = update.get("toolCallId") or update.get("tool_call_id") or ""
    return {
        "t": _event_t(rec, update),
        "tool": str(tool),
        "path": path_s,
        "kind": kind,
        "sid": sid,
        "name": name,
        "_tid": str(tid) if tid else "",
    }
