"""Shared coercions for accounting (tokens, $)."""

from __future__ import annotations

from typing import Any, Optional, Union


def as_int(v: Any, default: int = 0) -> int:
    """Best-effort int; ``None`` / bad values → ``default``."""
    try:
        if v is None:
            return default
        return int(v)
    except (TypeError, ValueError):
        try:
            return int(v or default)
        except (TypeError, ValueError):
            return default


def as_float(v: Any, default: float = 0.0) -> float:
    """Best-effort float; ``None`` / bad values → ``default``."""
    try:
        if v is None:
            return default
        return float(v)
    except (TypeError, ValueError):
        try:
            return float(v or default)
        except (TypeError, ValueError):
            return default


def as_opt_int(v: Any) -> Optional[int]:
    """int or None; does not treat missing as 0."""
    if v is None:
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
