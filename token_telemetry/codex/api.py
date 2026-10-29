"""Codex connector endpoints. Same shapes as /api/state, /api/sessions, /api/aggregate."""

from __future__ import annotations

import hashlib
import json
import threading
from pathlib import Path
from typing import Any, Callable, Optional

from token_telemetry.codex.adapt import build_period, sessions_for_ui, state_for
from token_telemetry.codex.store import catalog_rows, clear_cache, focused_snapshot, load_snapshot


_WAITING = (
    b'{"watching":false,"error":null,"pending":true,"session_id":null,"sessions":[],'
    b'"rounds":[],"turns":[],"sub_sessions":[],"live":{},"totals":{},'
    b'"source":"codex"}'
)
class CodexFeed:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pin: Optional[str] = None
        self._bump = 0
        self._state: tuple[Any, ...] = ()
        self._state_body = b""
        self._state_etag = '"0"'
        self._last_snap: Optional[dict[str, Any]] = None

    def select(self, session_id: Optional[str], home: Optional[Path] = None) -> dict[str, Any]:
        # Headers only. Parsing every rollout here is what froze Grok Build.
        if session_id:
            ids = {str(row.get("id")) for row in catalog_rows(home)}
            if session_id not in ids:
                return {"ok": False, "error": f"unknown session {session_id}"}
        with self._lock:
            self._pin = session_id or None
            pin = self._pin
            self._state = ()
        return {
            "ok": True,
            "session_id": pin,
            "pinned": pin is not None,
            "follow_active": pin is None,
        }

    def reset(self) -> dict[str, Any]:
        clear_cache()
        with self._lock:
            self._bump += 1
            self._state = ()
            bump = self._bump
        return {"ok": True, "cleared": 0, "cache_gen": bump, "source": "codex"}

    def health(self, home: Optional[Path] = None) -> dict[str, Any]:
        snap = load_snapshot(home)
        with self._lock:
            bump = self._bump
        gen = int(snap.get("n_calls") or 0) ^ int(snap.get("scan_ms") or 0) ^ bump
        return {"ok": True, "cache_gen": gen, "source": "codex"}

    def sessions(self, home: Optional[Path] = None) -> dict[str, Any]:
        with self._lock:
            pin = self._pin
        snap = focused_snapshot(home, pin)
        rows = sessions_for_ui(snap, focus=pin, limit=10000)
        return {
            "sessions": rows,
            "current": pin or _newest_root(snap),
            "pinned": pin,
            "follow_active": pin is None,
            "source": "codex",
        }

    def state_bytes(self, home: Optional[Path] = None) -> tuple[bytes, str]:
        """Open root plus the picker. Do not parse every historical rollout.

        Child rollouts (Guardian review) keep a parent on an older day. The
        newest day alone listed only those children, and the full-history
        parse behind that list held the process until Grok Build timed out.
        """
        with self._lock:
            pin = self._pin
            bump = self._bump
        try:
            snap = focused_snapshot(home, pin)
        except Exception:
            return _WAITING, '"0"'
        with self._lock:
            if self._state == (id(snap), pin, bump) and self._state_body:
                return self._state_body, self._state_etag
        return self._remember(snap)

    def _remember(self, snap: dict[str, Any]) -> tuple[bytes, str]:
        with self._lock:
            pin = self._pin
            bump = self._bump
        payload = state_for(snap, pin, pinned=pin)
        body = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":")
        ).encode("utf-8")
        etag = '"' + hashlib.sha1(body).hexdigest()[:16] + '"'
        with self._lock:
            self._last_snap = snap
            self._state = (id(snap), pin, bump)
            self._state_body = body
            self._state_etag = etag
        return body, etag

    def aggregate(
        self,
        period: str,
        offset: int,
        grain: str,
        home: Optional[Path] = None,
        on_progress: Optional[Callable[..., None]] = None,
    ) -> dict[str, Any]:
        snap = load_snapshot(home, on_progress=on_progress)
        payload = build_period(snap, period=period, offset=offset, grain=grain)
        with self._lock:
            bump = self._bump
        payload["cache_gen"] = int(snap.get("n_calls") or 0) ^ int(snap.get("scan_ms") or 0) ^ bump
        return payload


def _newest_root(snap: dict[str, Any]) -> Optional[str]:
    rows = sessions_for_ui(snap, limit=1)
    if not rows:
        return None
    return str(rows[0]["session_id"])


FEED = CodexFeed()
