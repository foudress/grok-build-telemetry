"""Serve a selected connector session without blocking on its trace replay."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from concurrent.futures import CancelledError, ThreadPoolExecutor, TimeoutError
from pathlib import Path


class ConnectorFeed:
    def __init__(self, source, *, catalog, focus, history, revision, clear, sessions, state, period):
        self._source = source
        self._catalog = catalog
        self._focus = focus
        self._history = history
        self._revision = revision
        self._clear = clear
        self._sessions = sessions
        self._state = state
        self._period = period
        self._lock = threading.Lock()
        self._worker = ThreadPoolExecutor(max_workers=2, thread_name_prefix=f"{source}-state")
        self._pin = None
        self._bump = 0
        self._home = None
        self._future = None
        self._loaded = None
        self._next_refresh = 0.0
        self._waiting = self._encode(self._shell(pending=True))

    def _shell(self, *, pending, error=None):
        return {
            "source": self._source, "watching": False, "pending": pending,
            "error": error, "session_id": None, "sessions": [], "rounds": [],
            "turns": [], "sub_sessions": [], "live": {}, "totals": {},
        }

    @staticmethod
    def _encode(payload):
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        return body, '"' + hashlib.sha1(body).hexdigest()[:16] + '"'

    def _invalidate(self):
        if self._future is not None:
            self._future.cancel()
        self._future = None
        self._loaded = None
        self._next_refresh = 0.0

    def select(self, session_id: str | None, home: Path | None = None):
        if session_id and not any(row.get("id") == session_id for row in self._catalog(home)):
            return {"ok": False, "error": f"unknown session {session_id}"}
        pin = session_id or None
        with self._lock:
            if pin != self._pin:
                self._pin = pin
                self._invalidate()
        return {"ok": True, "session_id": pin, "pinned": pin is not None, "follow_active": pin is None}

    def reset(self):
        self._clear()
        with self._lock:
            self._bump += 1
            self._invalidate()
            bump = self._bump
        return {"ok": True, "cleared": 0, "cache_gen": bump, "source": self._source}

    @staticmethod
    def _generation(revision, bump):
        digest = hashlib.sha1(f"{revision}:{bump}".encode()).hexdigest()
        return int(digest[:12], 16)

    def health(self, home: Path | None = None):
        with self._lock:
            bump = self._bump
        return {"ok": True, "cache_gen": self._generation(self._revision(home), bump), "source": self._source}

    def sessions(self, home: Path | None = None):
        with self._lock:
            pin = self._pin
            loaded = self._loaded if self._home == home else None
        snapshot = loaded[0] if loaded and loaded[0] is not None else {"threads": self._catalog(home)}
        rows = self._sessions(snapshot, focus=pin, limit=10000)
        active = next((row["session_id"] for row in rows if row.get("active")), None)
        return {
            "sessions": rows, "current": pin or active, "pinned": pin,
            "follow_active": pin is None, "source": self._source,
        }

    def _read_state(self, home, pin, previous):
        try:
            snapshot = self._focus(home, pin)
            if previous is not None and snapshot is previous[0]:
                return previous
            return snapshot, self._encode(self._state(snapshot, pin, pinned=pin))
        except Exception as exc:
            error = f"Could not load {self._source} session ({type(exc).__name__})"
            return None, self._encode(self._shell(pending=False, error=error))

    def state_bytes(self, home: Path | None = None):
        with self._lock:
            if self._home != home:
                self._invalidate()
                self._home = home
            if self._future is None and time.monotonic() >= self._next_refresh:
                self._future = self._worker.submit(self._read_state, home, self._pin, self._loaded)
            future = self._future
            if future is None:
                return self._loaded[1]
            if self._loaded is not None and not future.done():
                return self._loaded[1]
        try:
            # Give warm and small sessions a chance to paint in the first request.
            future.result(timeout=0.025)
        except (CancelledError, TimeoutError):
            return self._waiting
        with self._lock:
            if future is self._future:
                self._loaded = future.result()
                self._future = None
                self._next_refresh = time.monotonic() + 0.4
            return self._loaded[1] if self._home == home and self._loaded else self._waiting

    def aggregate(self, period, offset, grain, home=None, on_progress=None, billing=False):
        with self._lock:
            bump = self._bump
        snapshot = self._history(home, on_progress=on_progress)
        payload = self._period(snapshot, period=period, offset=offset, grain=grain, billing=billing)
        payload["cache_gen"] = self._generation(snapshot["source_revision"], bump)
        return payload
