"""OpenAI API list prices for Codex. See developers.openai.com/api/docs/pricing."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

# USD per 1M tokens: input, cached, output, cache-write 30m, then the long tier.
_LONG_AT = 272_000
# March 1 at midnight Pacific, when the GPT-6.2 Ultrafast promotion ends.
_ULTRAFAST_CUT = datetime(2027, 3, 1, 8, tzinfo=timezone.utc)

_ROW = tuple[float, float, float, float, float, float, float, float]

_RATES: dict[str, _ROW] = {
    # input, cached, output, write, long input, long cached, long output, long write
    "gpt-6.2-astra": (8.0, 0.40, 40.0, 10.0, 16.0, 0.80, 60.0, 20.0),
    "gpt-6.2-sol": (2.0, 0.10, 10.0, 2.50, 4.0, 0.20, 15.0, 5.0),
    "gpt-6-astra": (10.0, 1.0, 50.0, 12.50, 20.0, 2.0, 75.0, 25.0),
    "gpt-6.1-sol": (2.0, 0.10, 10.0, 2.50, 4.0, 0.20, 15.0, 5.0),
    "gpt-6.1-terra": (1.0, 0.05, 5.0, 1.25, 2.0, 0.10, 7.50, 2.50),
    "gpt-6-luna": (0.10, 0.01, 0.50, 0.125, 0.20, 0.02, 0.75, 0.25),
    "gpt-6-sol": (2.0, 0.20, 10.0, 2.50, 4.0, 0.40, 15.0, 5.0),
    "gpt-5.6-sol": (4.0, 0.40, 20.0, 5.0, 8.0, 0.80, 30.0, 10.0),
    "gpt-5.6-terra": (2.0, 0.20, 12.0, 2.50, 4.0, 0.40, 18.0, 5.0),
    "gpt-5.6-luna": (0.20, 0.02, 1.20, 0.25, 0.40, 0.04, 1.80, 0.50),
    "gpt-5.4": (2.50, 0.25, 15.0, 0.0, 2.50, 0.25, 15.0, 0.0),
    "gpt-5.3-codex": (1.75, 0.175, 14.0, 0.0, 1.75, 0.175, 14.0, 0.0),
    "gpt-5.2": (1.75, 0.175, 14.0, 0.0, 1.75, 0.175, 14.0, 0.0),
    "gpt-5.1-codex-max": (1.25, 0.125, 10.0, 0.0, 1.25, 0.125, 10.0, 0.0),
    "gpt-5.1-codex": (1.25, 0.125, 10.0, 0.0, 1.25, 0.125, 10.0, 0.0),
    "gpt-5.1": (1.25, 0.125, 10.0, 0.0, 1.25, 0.125, 10.0, 0.0),
}

_FAST_MODELS = {
    "gpt-6.2-astra", "gpt-6.2-sol", "gpt-6-astra", "gpt-6.1-sol", "gpt-5.6-sol",
}
_ULTRAFAST_MODELS = {"gpt-6.2-astra", "gpt-6.2-sol", "gpt-6-astra", "gpt-6.1-sol"}

# Longest id first so gpt-6.1-sol wins over gpt-6-sol and gpt-5.1-codex-max over gpt-5.1.
_ORDER = sorted(_RATES, key=len, reverse=True)

# Codex names this reviewer; the calls are Luna 5.6.
_ALIASES = {
    "codex-auto-review": "gpt-5.6-luna",
}


def priced_model(model: Any) -> Optional[str]:
    if not isinstance(model, str):
        return None
    key = model.strip().lower().replace("_", "-").replace(" ", "-")
    if not key:
        return None
    alias = _ALIASES.get(key)
    if alias:
        return alias
    for name in _ORDER:
        if name in key:
            return name
    return None


def _call_time(when: Any) -> datetime:
    if isinstance(when, str):
        try:
            when = datetime.fromisoformat(when.replace("Z", "+00:00"))
        except ValueError:
            when = None
    if isinstance(when, datetime):
        return when if when.tzinfo else when.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def _tier(model: Any, service_tier: Any) -> str:
    if isinstance(service_tier, str) and service_tier.strip():
        return service_tier.strip().lower()
    bits = str(model or "").lower().replace("_", "-").replace(" ", "-").split("-")
    return next((tier for tier in ("ultrafast", "fast", "priority") if tier in bits), "standard")


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
    """One API call. Uncached excludes cached. Cache-write is priced on its own row."""
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
    row = _RATES[name]
    long = prompt > _LONG_AT or (uncached + cached) > _LONG_AT
    inn, cache_r, out_r, write_r = row[4:8] if long else row[0:4]
    tier = _tier(model, service_tier)
    multiplier = 1
    if tier in ("fast", "priority") and name in _FAST_MODELS:
        multiplier = 2
    elif tier == "ultrafast" and name in _ULTRAFAST_MODELS:
        multiplier = 3 if name.startswith("gpt-6.2-") and _call_time(when) < _ULTRAFAST_CUT else 6
    write = max(0, int(cache_write))
    fresh = max(0, int(uncached) - write)
    usd_in = fresh / 1_000_000 * inn * multiplier
    usd_write = write / 1_000_000 * write_r * multiplier
    usd_cached = max(0, int(cached)) / 1_000_000 * cache_r * multiplier
    usd_out = max(0, int(output)) / 1_000_000 * out_r * multiplier
    total = usd_in + usd_write + usd_cached + usd_out
    return {
        "model": name,
        "in": usd_in + usd_write,
        "cached": usd_cached,
        "out": usd_out,
        "write": usd_write,
        "total": total,
        "priced": True,
        "long": long,
    }
