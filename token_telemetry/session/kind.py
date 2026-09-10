"""Single parse of ``session_kind`` (main / subagent / subagent_resume)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

SUBAGENT_KINDS = frozenset({"subagent", "subagent_resume"})


def normalize_session_kind(kind: Any) -> Optional[str]:
    if not isinstance(kind, str):
        return None
    s = kind.strip().lower()
    return s or None


def is_subagent_kind(kind: Any) -> bool:
    s = normalize_session_kind(kind)
    return s in SUBAGENT_KINDS if s else False


def session_kind_from_summary(summary: Optional[dict[str, Any]]) -> Optional[str]:
    if not isinstance(summary, dict):
        return None
    kind = summary.get("session_kind")
    if not (isinstance(kind, str) and kind.strip()):
        info = summary.get("info")
        if isinstance(info, dict):
            kind = info.get("session_kind")
    return normalize_session_kind(kind)


def session_kind_of(session_dir: Optional[Path]) -> Optional[str]:
    if session_dir is None:
        return None
    from token_telemetry.session.discover import _read_session_summary

    return session_kind_from_summary(_read_session_summary(Path(session_dir)))


def session_kind_from_dir_files(session_dir: Optional[Path]) -> Optional[str]:
    """Kind from summary.json, then a short ``system_prompt.txt`` peek.

    Bootstrap uses the peek so Super Agent [1] still shows when summary is
    missing. Live session_kind_of stays summary-only (period / peel).
    """
    if session_dir is None:
        return None
    sm = Path(session_dir) / "summary.json"
    data: Any = None
    if sm.is_file():
        try:
            data = json.loads(sm.read_text(encoding="utf-8-sig", errors="replace"))
        except (OSError, json.JSONDecodeError, UnicodeError):
            data = None
    kind = session_kind_from_summary(data if isinstance(data, dict) else None)
    if kind:
        return kind
    sp = Path(session_dir) / "system_prompt.txt"
    if sp.is_file():
        try:
            blob = sp.read_text(encoding="utf-8-sig", errors="replace")[:400].lower()
        except OSError:
            blob = ""
        if "subagent" in blob:
            return "subagent"
    return None
