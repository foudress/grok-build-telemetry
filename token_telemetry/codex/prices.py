"""OpenAI list prices for Codex estimates.

Standard rates from the API pricing table (developers.openai.com/api/docs/pricing).
Short context is ≤272k input tokens. Above that, the published long-context
rates apply. codex-auto-review is Luna 5.6. Any other unknown model stays at $0.
This module does not touch Grok rate tables.
"""

from __future__ import annotations

from typing import Any, Optional

# USD per 1M tokens: input, cached, output, cache-write 30m, then the long tier.
_LONG_AT = 272_000

_ROW = tuple[float, float, float, float, float, float, float, float]

_RATES: dict[str, _ROW] = {
    # input, cached, output, write, long input, long cached, long output, long write
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

# Longest id first so gpt-6.1-sol wins over gpt-6-sol and gpt-5.1-codex-max over gpt-5.1.
_ORDER = sorted(_RATES, key=len, reverse=True)

# Codex names this reviewer; the calls are Luna 5.6.
_ALIASES = {
    "codex-auto-review": "gpt-5.6-luna",
}


def priced_model(model: Any) -> Optional[str]:
    if not isinstance(model, str):
        return None
    key = model.strip().lower().replace("_", "-")
    if not key:
        return None
    alias = _ALIASES.get(key)
    if alias:
        return alias
    for name in _ORDER:
        if name in key:
            return name
    return None


def price_usage(
    model: Any,
    *,
    uncached: int,
    cached: int,
    output: int,
    cache_write: int = 0,
    prompt: int = 0,
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
    write = max(0, int(cache_write))
    fresh = max(0, int(uncached) - write)
    usd_in = fresh / 1_000_000 * inn
    usd_write = write / 1_000_000 * (write_r or inn)
    usd_cached = max(0, int(cached)) / 1_000_000 * cache_r
    usd_out = max(0, int(output)) / 1_000_000 * out_r
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
