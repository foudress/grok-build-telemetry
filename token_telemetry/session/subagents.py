"""Link parent sessions to sub-agent sessions and peel their official usage.

Facade: ids live in ``session.ids`` / ``session.kind``; official peel in
``session.usage``; Sys/extract cards in ``session.cards``.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Optional

from token_telemetry.session.ids import (  # noqa: F401
    SUBAGENT_ID_RE,
    TASK_LINE_RE,
    UUID_RE,
    add_uuid as _add_uuid,
    child_ids_from_session_update,
    extract_ids_from_text,
    extract_task_ids,
)
from token_telemetry.session.kind import (  # noqa: F401
    SUBAGENT_KINDS,
    is_subagent_kind,
    session_kind_of,
)
from token_telemetry.session.usage import (  # noqa: F401
    add_usage,
    child_ids_to_peel,
    child_tier_context_tokens,
    collect_child_ids_from_round,
    collect_resume_alias_from_round,
    empty_usage,
    extract_resume_from,
    latest_in_resume_chain,
    load_session_official_turns,
    load_session_official_usage,
    peel_round_usage,
    price_child_usage,
    read_session_summary,
    root_subagent_id,
    sibling_session_dir,
    spawn_session_ids,
    sub_usage,
    summary_parent_session_id,
    tool_resume_from,
)
from token_telemetry.session.cards import (  # noqa: F401
    attach_subagents_after_steps,
    capture_child_sys,
    ensure_lineage_subagent_cards,
    is_spawn_tool,
    relabel_subagent_cards,
    system_box_total,
)

TYPE_RE = re.compile(r"(?:^|\n)\s*type:\s*(\S+)", re.IGNORECASE)
DESC_RE = re.compile(r"(?:^|\n)\s*description:\s*(.+)", re.IGNORECASE)



def is_subagent_session(session_dir: Optional[Path]) -> bool:
    return is_subagent_kind(session_kind_of(session_dir))


def parse_subagent_meta(text: Any) -> dict[str, Any]:
    """Pull id / type / description from a spawn or get_command result body."""
    if not isinstance(text, str) or not text.strip():
        return {}
    out: dict[str, Any] = {}
    m = SUBAGENT_ID_RE.search(text)
    if m:
        out["subagent_id"] = m.group(1).lower()
    else:
        tm = TASK_LINE_RE.search(text)
        if tm:
            out["subagent_id"] = tm.group(1).lower()
    t = TYPE_RE.search(text)
    if t:
        out["subagent_type"] = t.group(1).strip()
    d = DESC_RE.search(text)
    if d:
        desc = d.group(1).strip()
        if desc:
            out["subagent_description"] = desc[:120]
    return out


def merge_tool_subagent_ids(tool: Optional[dict[str, Any]], *parts: Any) -> None:
    """Stamp full session UUIDs onto a spawn/wait tool (never ACP call-… ids)."""
    if not isinstance(tool, dict):
        return
    seen: set[str] = set()
    ids: list[str] = []
    _add_uuid(ids, seen, tool.get("subagent_id"))
    for x in tool.get("subagent_ids") or []:
        _add_uuid(ids, seen, x)
    for part in parts:
        if isinstance(part, (list, tuple)):
            for x in part:
                _add_uuid(ids, seen, x)
        else:
            _add_uuid(ids, seen, part)
    for uid in extract_ids_from_text(*parts):
        _add_uuid(ids, seen, uid)
    if not ids:
        return
    tool["subagent_ids"] = ids
    if not tool.get("subagent_id"):
        tool["subagent_id"] = ids[0]


def apply_history_subagent_ids(
    tool: Optional[dict[str, Any]], hit: Optional[dict[str, Any]]
) -> None:
    """chat_history tool_result.content has the full spawn UUID; preview does not."""
    if not isinstance(tool, dict) or not isinstance(hit, dict):
        return
    merge_tool_subagent_ids(
        tool,
        hit.get("subagent_id"),
        hit.get("subagent_ids"),
        hit.get("body"),
        hit.get("preview"),
    )

