"""HTTP dashboard server: static assets + API routes."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, urlparse

from token_telemetry import features as feat
from token_telemetry.graph import api as graph_api
from token_telemetry.session.aggregate import build_aggregate
from token_telemetry.session.discover import list_sessions_for_ui
from token_telemetry.session.history_watch import WATCHER
from token_telemetry.session.monitor import MONITOR


REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DASHBOARD_DIR = REPO_ROOT / "dashboard"
DASHBOARD_HTML = DASHBOARD_DIR / "index.html"
HISTORY_HTML = DASHBOARD_DIR / "history.html"
GRAPH_HTML = DASHBOARD_DIR / "graph.html"

# Static dashboard assets (css/, js/, …) under DASHBOARD_DIR only
_STATIC_MIME = {
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".map": "application/json; charset=utf-8",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        # quiet — only print errors
        if args and str(args[0]).startswith("5"):
            super().log_message(fmt, *args)

    def _send(self, code: int, body: bytes, content_type: str) -> None:
        # Client often aborts mid-response (Ctrl+F5, tab close, poll race).
        # That is WinError 10053 / BrokenPipe — not a server bug; stay quiet.
        try:
            self.send_response(code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()
            self.wfile.write(body)
        except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, OSError):
            return

    def _send_gated(self, surface: str) -> None:
        body = json.dumps(
            {"ok": False, "gated": True, "surface": surface, "note": feat.WIP_NOTE},
            ensure_ascii=False,
        ).encode("utf-8")
        self._send(503, body, "application/json; charset=utf-8")

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send(204, b"", "text/plain")

    def _safe_dashboard_file(self, url_path: str) -> Optional[Path]:
        """Resolve url_path under DASHBOARD_DIR only (no path traversal)."""
        # Strip leading slash; reject empty / absolute / drive paths
        rel = url_path.lstrip("/")
        if not rel or rel.startswith("/") or "\\" in rel:
            return None
        # Normalize and block .. segments
        parts = []
        for p in rel.split("/"):
            if p in ("", "."):
                continue
            if p == "..":
                return None
            parts.append(p)
        if not parts:
            return None
        candidate = (DASHBOARD_DIR.joinpath(*parts)).resolve()
        try:
            candidate.relative_to(DASHBOARD_DIR.resolve())
        except ValueError:
            return None
        if not candidate.is_file():
            return None
        return candidate

    def do_GET(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            if not DASHBOARD_HTML.is_file():
                self._send(500, b"dashboard/index.html missing", "text/plain")
                return
            data = DASHBOARD_HTML.read_bytes()
            self._send(200, data, "text/html; charset=utf-8")
            return
        if path in ("/history", "/history.html"):
            if not feat.MUTATION_HISTORY:
                self._send(200, feat.wip_html("Mutation History"), "text/html; charset=utf-8")
                return
            if not HISTORY_HTML.is_file():
                self._send(500, b"dashboard/history.html missing", "text/plain")
                return
            data = HISTORY_HTML.read_bytes()
            self._send(200, data, "text/html; charset=utf-8")
            return
        if path in ("/graph", "/graph.html"):
            if not feat.AGENT_ANIMATION_GRAPH:
                self._send(
                    200,
                    feat.wip_html("Agent Animation Graph"),
                    "text/html; charset=utf-8",
                )
                return
            if not GRAPH_HTML.is_file():
                self._send(500, b"dashboard/graph.html missing", "text/plain")
                return
            data = GRAPH_HTML.read_bytes()
            self._send(200, data, "text/html; charset=utf-8")
            return
        if path == "/api/state":
            body = MONITOR.snapshot_bytes()
            self._send(200, body, "application/json; charset=utf-8")
            return
        if path == "/api/sessions":
            body = json.dumps(
                {
                    "sessions": list_sessions_for_ui(),
                    "current": MONITOR.session_id,
                    "pinned": MONITOR.pinned_session_id,
                    "follow_active": MONITOR.pinned_session_id is None,
                },
                ensure_ascii=False,
            ).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
            return
        if path == "/api/health":
            self._send(200, b'{"ok":true}', "application/json")
            return
        if path == "/api/history":
            if not feat.MUTATION_HISTORY:
                body = json.dumps(
                    {"ok": False, "gated": True, "note": feat.WIP_NOTE},
                    ensure_ascii=False,
                ).encode("utf-8")
                self._send(503, body, "application/json; charset=utf-8")
                return
            body = json.dumps(WATCHER.snapshot(), ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
            return
        if path == "/api/aggregate":
            qs = parse_qs(urlparse(self.path).query)
            period = (qs.get("period") or ["daily"])[0]
            grain = (qs.get("grain") or ["day"])[0]
            stack = (qs.get("stack") or ["io"])[0]
            rate_raw = (qs.get("rate") or ["0"])[0]
            rate = str(rate_raw).strip().lower() in ("1", "true", "yes", "on")
            try:
                offset = int((qs.get("offset") or ["0"])[0])
            except ValueError:
                offset = 0
            try:
                payload = build_aggregate(period, offset, grain, stack=stack, rate=rate)
            except Exception as exc:  # noqa: BLE001
                payload = {"error": f"{type(exc).__name__}: {exc}"}
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
            return
        if path.startswith("/api/graph"):
            if not feat.AGENT_ANIMATION_GRAPH:
                self._send_gated("agent_animation_graph")
                return
        if path == "/api/graph/projects":
            body = json.dumps(graph_api.projects_payload(), ensure_ascii=False).encode(
                "utf-8"
            )
            self._send(200, body, "application/json; charset=utf-8")
            return
        if path == "/api/graph/sessions":
            qs = parse_qs(urlparse(self.path).query)
            status, payload = graph_api.sessions_payload((qs.get("root") or [""])[0])
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send(status, body, "application/json; charset=utf-8")
            return
        if path == "/api/graph/activity":
            qs = parse_qs(urlparse(self.path).query)
            root = (qs.get("root") or [""])[0]
            sid = (qs.get("session_id") or [""])[0] or None
            status, payload = graph_api.activity_payload(root, sid)
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send(status, body, "application/json; charset=utf-8")
            return
        if path == "/api/graph/replay":
            qs = parse_qs(urlparse(self.path).query)
            status, payload = graph_api.replay_payload((qs.get("root") or [""])[0])
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send(status, body, "application/json; charset=utf-8")
            return
        if path == "/api/graph":
            qs = parse_qs(urlparse(self.path).query)
            status, payload = graph_api.graph_payload((qs.get("root") or [""])[0])
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send(status, body, "application/json; charset=utf-8")
            return
        # Static assets: /css/*, /js/*, and other files under dashboard/
        if path.startswith("/css/") or path.startswith("/js/") or (
            path.startswith("/") and not path.startswith("/api/")
        ):
            fpath = self._safe_dashboard_file(path)
            if fpath is not None:
                data = fpath.read_bytes()
                ctype = _STATIC_MIME.get(fpath.suffix.lower(), "application/octet-stream")
                self._send(200, data, ctype)
                return
        self._send(404, b"not found", "text/plain")

    def do_POST(self) -> None:  # noqa: N802
        path = urlparse(self.path).path
        if path == "/api/session":
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            raw = self.rfile.read(n) if n > 0 else b"{}"
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._send(400, b'{"ok":false,"error":"invalid json"}', "application/json")
                return
            sid = data.get("session_id")
            if sid is not None and not isinstance(sid, str):
                sid = str(sid)
            # empty string / null → follow active
            if sid is not None and sid.strip() == "":
                sid = None
            result = MONITOR.select_session(sid)
            body = json.dumps(result, ensure_ascii=False).encode("utf-8")
            self._send(200 if result.get("ok") else 400, body, "application/json; charset=utf-8")
            return
        if path == "/api/cache/reset":
            from token_telemetry.session.calc_cache import reset_all_calcs

            n = reset_all_calcs()
            rebuilt = MONITOR.rebuild_current()
            body = json.dumps(
                {"ok": True, "cleared": n, **rebuilt},
                ensure_ascii=False,
            ).encode("utf-8")
            self._send(200, body, "application/json; charset=utf-8")
            return
        if path == "/api/graph/rescan":
            if not feat.AGENT_ANIMATION_GRAPH:
                self._send_gated("agent_animation_graph")
                return
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = 0
            raw = self.rfile.read(n) if n > 0 else b"{}"
            try:
                data = json.loads(raw.decode("utf-8") or "{}")
            except json.JSONDecodeError:
                self._send(400, b'{"error":"invalid json"}', "application/json")
                return
            root = data.get("root") if isinstance(data, dict) else None
            if not isinstance(root, str):
                root = ""
            status, payload = graph_api.rescan_payload(root)
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self._send(status, body, "application/json; charset=utf-8")
            return
        self._send(404, b"not found", "text/plain")


def background_poller(stop: threading.Event, interval: float = 0.5) -> None:
    while not stop.is_set():
        try:
            with MONITOR.lock:
                MONITOR.tick()
        except Exception:
            pass
        try:
            WATCHER.tick()
        except Exception:
            pass
        stop.wait(interval)

