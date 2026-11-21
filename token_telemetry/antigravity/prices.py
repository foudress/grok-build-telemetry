"""Gemini API standard list prices for Antigravity estimates.

Rates are the paid Standard tier on the Gemini pricing page
(https://ai.google.dev/gemini-api/docs/pricing), read 2026-11-21.
Output includes thinking. Cached input is the published context-caching
rate. These models have no separate cache-write SKU and no long-context
surcharge. A subscription may bill quota instead of this list.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

# USD per 1M tokens: input, cached, output.
_ROW = tuple[float, float, float]

_HYDROGEN: _ROW = (1.50, 0.075, 7.50)
_NEON: _ROW = (3.00, 0.15, 15.00)
_ARGON_INTRO: _ROW = (2.00, 0.10, 10.00)
_ARGON: _ROW = (4.00, 0.20, 20.00)
_FLASH_INTRO: _ROW = (0.75, 0.075, 3.75)
_FLASH: _ROW = (1.50, 0.15, 7.50)

_ARGON_CUT = datetime(2026, 12, 1, tzinfo=timezone.utc)
_FLASH_CUT = datetime(2027, 1, 1, tzinfo=timezone.utc)

# Longer ids first. flash-worker is the Hydrogen executor inside Orchestrator.
_NEEDLES = (
    "gemini-4-hydrogen",
    "gemini-4-carbon",
    "gemini-4-argon",
    "gemini-4-neon",
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "flash-worker",
    "orchestrator-neon",
    "hydrogen",
    "carbon",
    "argon",
    "neon",
)

_PUBLIC = {
    "gemini-4-hydrogen": "gemini-4-hydrogen",
    "gemini-4-carbon": "gemini-4-carbon",
    "gemini-4-argon": "gemini-4-argon",
    "gemini-4-neon": "gemini-4-neon",
    "gemini-3.8-flash": "gemini-3.8-flash",
    "gemini-3.6-flash": "gemini-3.6-flash",
    "flash-worker": "gemini-4-hydrogen",
    "orchestrator-neon": "gemini-4-neon",
    "hydrogen": "gemini-4-hydrogen",
    "carbon": "gemini-4-carbon",
    "argon": "gemini-4-argon",
    "neon": "gemini-4-neon",
}


def _key(model: Any) -> str:
    if not isinstance(model, str):
        return ""
    return model.strip().lower().replace("_", "-").replace(" ", "-")


def priced_model(model: Any) -> Optional[str]:
    key = _key(model)
    if not key:
        return None
    for needle in _NEEDLES:
        if needle in key:
            return _PUBLIC[needle]
    return None


def _moment(when: Any) -> datetime:
    if isinstance(when, datetime):
        return when if when.tzinfo else when.replace(tzinfo=timezone.utc)
    if isinstance(when, (int, float)) and not isinstance(when, bool):
        return datetime.fromtimestamp(float(when), timezone.utc)
    if isinstance(when, str) and when.strip():
        text = when.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            parsed = None
        if parsed is not None:
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def _rates(name: str, when: datetime) -> _ROW:
    if name in ("gemini-4-argon", "gemini-4-carbon"):
        return _ARGON_INTRO if when < _ARGON_CUT else _ARGON
    if name in ("gemini-3.8-flash", "gemini-3.6-flash"):
        return _FLASH_INTRO if when < _FLASH_CUT else _FLASH
    if name == "gemini-4-neon":
        return _NEON
    return _HYDROGEN


def price_usage(
    model: Any,
    *,
    uncached: int,
    cached: int,
    output: int,
    cache_write: int = 0,
    prompt: int = 0,
    when: Any = None,
) -> dict[str, Any]:
    """One call. Uncached does not include cached or cache-write."""
    del prompt
    name = priced_model(model)
    if name is None:
        return {
            "model": None,
            "in": 0.0,
            "cached": 0.0,
            "out": 0.0,
            "write": 0.0,
            "total": 0.0,
            "priced": False,
        }
    inn, cache_r, out_r = _rates(name, _moment(when))
    fresh = max(0, int(uncached))
    write = max(0, int(cache_write))
    usd_in = fresh / 1_000_000 * inn
    usd_write = write / 1_000_000 * inn
    usd_cached = max(0, int(cached)) / 1_000_000 * cache_r
    usd_out = max(0, int(output)) / 1_000_000 * out_r
    return {
        "model": name,
        "in": usd_in + usd_write,
        "cached": usd_cached,
        "out": usd_out,
        "write": usd_write,
        "total": usd_in + usd_write + usd_cached + usd_out,
        "priced": True,
    }
