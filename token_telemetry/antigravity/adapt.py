"""Map an Antigravity snapshot onto the dashboard wire Codex already renders.

Call rows use the Codex field meanings: ``input_tokens`` is the full prompt
(uncached plus cache read), and ``reasoning_output_tokens`` sits inside
``output_tokens``. ``official_usd`` stays 0. ``estimate_usd`` is the Gemini
API standard list price. The conversation logs store no dollar amount.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Optional

from token_telemetry.antigravity.prices import price_usage, priced_model
from token_telemetry.codex.adapt import build_period as _codex_period
from token_telemetry.codex.adapt import sessions_for_ui, state_for as _codex_state

_PRICE_NOTE = (
    "Estimate uses Gemini API standard list prices "
    "(ai.google.dev/gemini-api/docs/pricing). "
    "Cached input uses the published context-caching rate. "
    "Thinking tokens are included in output. "
    "There is no long-context surcharge on these models. "
    "Gemini 4 Argon and Carbon use the introductory rate through 30 Nov 2026. "
    "Gemini 3.8 Flash uses its introductory rate through 31 Dec 2026. "
    "The Hydrogen worker inside Orchestrator is priced as Gemini 4 Hydrogen. "
    "An Antigravity plan may bill quota instead of this list. "
    "The local logs do not include a dollar amount from the API."
)


def state_for(
    snapshot: dict[str, Any],
    thread_id: Optional[str],
    *,
    pinned: Optional[str],
    now: Optional[float] = None,
) -> dict[str, Any]:
    payload = _codex_state(
        snapshot,
        thread_id,
        pinned=pinned,
        now=now,
        price=price_usage,
        name_of=priced_model,
    )
    payload["ledger_source"] = "antigravity"
    err = payload.get("error")
    if isinstance(err, str):
        payload["error"] = err.replace("Codex", "Antigravity")
    if payload.get("source") in (None, "", "codex"):
        payload["source"] = "antigravity"
    pricing = payload.get("pricing")
    if isinstance(pricing, dict):
        pricing["note"] = _PRICE_NOTE
        pricing["assumed"] = False
        if not pricing.get("model"):
            pricing["model_label"] = "Antigravity"
    payload["context_now_estimate_note"] = (
        "Context is the last prompt size: uncached input plus cache read."
    )
    return payload


def build_period(
    snapshot: dict[str, Any],
    *,
    period: str = "daily",
    offset: int = 0,
    grain: str = "hour",
    now: Optional[datetime] = None,
    billing: bool = False,
) -> dict[str, Any]:
    payload = _codex_period(
        snapshot,
        period=period,
        offset=offset,
        grain=grain,
        now=now,
        billing=billing,
        price=price_usage,
    )
    payload["ledger_source"] = "antigravity"
    return payload


__all__ = ["build_period", "sessions_for_ui", "state_for"]
