"""Compact continuation glue phrases — one matcher, head-only.

Workflow packed cards often quote a previous-session summary hundreds of KB
in. Matching *anywhere* classified the whole prompt as glue → user In = 0
and Tool Definitions swallowed Super Agent / User. Always search the first
``head`` characters (default 800).
"""

from __future__ import annotations

from typing import Any

COMPACT_GLUE_PHRASES: tuple[str, ...] = (
    "this session is being continued",
    "ran out of context",
    "conversation is summarized below",
    "previous conversation that ran out",
    "compaction/segment_",
)

COMPACT_GLUE_HEAD = 800


def is_compact_continuation(text: Any, head: int = COMPACT_GLUE_HEAD) -> bool:
    """True for the compact *rewrite header*, not a task that quotes it."""
    low = str(text or "").lower().lstrip()
    if not low:
        return False
    n = int(head) if head is not None else COMPACT_GLUE_HEAD
    if n < 0:
        n = COMPACT_GLUE_HEAD
    blob = low if n == 0 else low[:n]
    return any(m in blob for m in COMPACT_GLUE_PHRASES)
