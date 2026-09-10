"""Session UUID harvest — two policies, do not mix.

- ``extract_ids_from_text``: aggressive (tool preview / spawn body / Task line).
- ``child_ids_from_update``: strict (subagent_spawned + spawn|wait tool fields).
  Mixing the two reparents kids from skill docs / chat dumps.
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)
SUBAGENT_ID_RE = re.compile(
    r"subagent_id:\s*(" + UUID_RE.pattern + r")",
    re.IGNORECASE,
)
TASK_LINE_RE = re.compile(
    r"(?:---\s*)?Task\s+(" + UUID_RE.pattern + r")",
    re.IGNORECASE,
)


def add_uuid(ids: list[str], seen: set[str], uid: Any) -> None:
    s = str(uid or "").strip().lower()
    if not s or not UUID_RE.fullmatch(s) or s in seen:
        return
    seen.add(s)
    ids.append(s)


def extract_task_ids(raw_in: Any) -> list[str]:
    if not isinstance(raw_in, dict):
        return []
    ids = raw_in.get("task_ids") or raw_in.get("taskIds")
    if not isinstance(ids, list):
        return []
    out: list[str] = []
    for x in ids:
        s = str(x or "").strip().lower()
        if UUID_RE.fullmatch(s):
            out.append(s)
    return out


def extract_ids_from_text(*parts: Any) -> list[str]:
    """Aggressive: every ``subagent_id:`` / ``Task <uuid>`` in the blobs."""
    found: list[str] = []
    seen: set[str] = set()
    for part in parts:
        if part is None:
            continue
        if not isinstance(part, str):
            try:
                part = json.dumps(part, ensure_ascii=False)
            except (TypeError, ValueError):
                part = str(part)
        for m in SUBAGENT_ID_RE.finditer(part):
            uid = m.group(1).lower()
            if uid not in seen:
                seen.add(uid)
                found.append(uid)
        for m in TASK_LINE_RE.finditer(part):
            uid = m.group(1).lower()
            if uid not in seen:
                seen.add(uid)
                found.append(uid)
    return found


def child_ids_from_session_update(upd: Optional[dict[str, Any]]) -> list[str]:
    """Ids from ``subagent_spawned`` (D/W/M lists these even without tool parse)."""
    if not isinstance(upd, dict):
        return []
    kind = str(upd.get("sessionUpdate") or "")
    seen: set[str] = set()
    out: list[str] = []
    if kind == "subagent_spawned":
        add_uuid(out, seen, upd.get("subagent_id"))
        add_uuid(out, seen, upd.get("child_session_id"))
        return out
    return out


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
    return (
        "spawn_subagent" in n
        or "get_command_or_subagent_output" in n
        or "direct_foudres" in n
        or "spawn_foudres" in n
    )


def child_ids_from_update(upd: dict[str, Any]) -> list[str]:
    """Strict: subagent_spawned / spawn|wait tool fields. Not every UUID nearby."""
    if not isinstance(upd, dict):
        return []
    kind = str(upd.get("sessionUpdate") or "")
    out: list[str] = []
    seen: set[str] = set()
    if kind == "subagent_spawned":
        for key in ("subagent_id", "child_session_id"):
            add_uuid(out, seen, upd.get(key))
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
        add_uuid(out, seen, uid)
    if "spawn_subagent" in name or "foudres" in name:
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
            add_uuid(out, seen, uid)
    return out
