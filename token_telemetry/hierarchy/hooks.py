"""Hook placement helpers (display slots)."""

from __future__ import annotations

from typing import Any


# Hook placement (display). Tokens are never billed as model In for these rows;
# UI always lists every hook_execution so new event names still appear.
_USER_SECTION_HOOK_EVENTS = frozenset({
    "user_prompt_submit",
})
_TO_USER_HOOK_EVENTS = frozenset({
    "stop",
    "session_stop",
    "agent_stop",
    "session_end",
})


def _hook_slot(event_name: Any) -> str:
    """Where a hook belongs in the tree: user | to_user | stream."""
    ev = str(event_name or "hook").strip().lower()
    if ev in _USER_SECTION_HOOK_EVENTS or ev.startswith("user_prompt"):
        return "user"
    if ev in _TO_USER_HOOK_EVENTS or ev.endswith("_stop") or ev == "stop":
        return "to_user"
    return "stream"
