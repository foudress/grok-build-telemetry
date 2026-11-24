"""Public Google and Anthropic list prices for Antigravity estimates.

Sources: aistudio.google.com/docs/pricing, Google Cloud Agent Platform pricing,
and platform.claude.com/docs/en/about-claude/pricing. Read 2026-11-24.
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

_CLAUDE = {
    "claude-opus-5-5": (4.0, 0.20, 20.0),
    "claude-sonnet-5-5": (2.0, 0.10, 10.0),
    "claude-opus-4-6": (5.0, 0.50, 25.0),
    "claude-sonnet-4-6": (3.0, 0.30, 15.0),
}

_PUBLIC = {
    "gemini-4-hydrogen": "gemini-4-hydrogen",
    "gemini-4-carbon": "gemini-4-carbon",
    "gemini-4-argon": "gemini-4-argon",
    "gemini-4-neon": "gemini-4-neon",
    "gemini-3.8-flash": "gemini-3.8-flash",
    "gemini-3.7-flash": "gemini-3.7-flash",
    "gemini-3.6-flash": "gemini-3.6-flash",
    "gemini-3.1-pro": "gemini-3.1-pro",
    **{name: name for name in _CLAUDE},
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
    words = model.lower().replace("_", " ").replace("-", " ").replace("(", " ").replace(")", " ")
    return "-".join(words.split())


def priced_model(model: Any) -> Optional[str]:
    key = _key(model)
    if not key:
        return None
    if key.startswith("claude-") or "-claude-" in key:
        key = key.replace("5.5", "5-5").replace("4.6", "4-6")
    for needle, name in _PUBLIC.items():
        if f"-{needle}-" in f"-{key}-":
            return name
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


def _rates(name: str, when: datetime, prompt: int) -> _ROW:
    if name in _CLAUDE:
        return _CLAUDE[name]
    if name in ("gemini-4-argon", "gemini-4-carbon"):
        return _ARGON_INTRO if when < _ARGON_CUT else _ARGON
    if name in ("gemini-3.8-flash", "gemini-3.7-flash", "gemini-3.6-flash"):
        return _FLASH_INTRO if when < _FLASH_CUT else _FLASH
    if name == "gemini-3.1-pro":
        return (4.0, 0.40, 18.0) if prompt > 200_000 else (2.0, 0.20, 12.0)
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
    service_tier: Any = None,
) -> dict[str, Any]:
    """One call. Uncached does not include cached or cache-write."""
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
    inn, cache_r, out_r = _rates(name, _moment(when), max(prompt, uncached + cached))
    tier = _key(service_tier)
    if not tier:
        tier = next((t for t in ("priority", "fast") if t in _key(model).split("-")), "")
    multiplier = 1.0
    if tier == "priority" and name not in _CLAUDE:
        multiplier = 1.8
    elif tier == "fast" and name == "claude-opus-5-5":
        multiplier = 2.0
    fresh = max(0, int(uncached))
    write = max(0, int(cache_write))
    write_multiplier = 1.25 if name in _CLAUDE else 1.0
    usd_in = fresh / 1_000_000 * inn * multiplier
    usd_write = write / 1_000_000 * inn * multiplier * write_multiplier
    usd_cached = max(0, int(cached)) / 1_000_000 * cache_r * multiplier
    usd_out = max(0, int(output)) / 1_000_000 * out_r * multiplier
    return {
        "model": name,
        "in": usd_in + usd_write,
        "cached": usd_cached,
        "out": usd_out,
        "write": usd_write,
        "total": usd_in + usd_write + usd_cached + usd_out,
        "priced": True,
    }
