#!/usr/bin/env python3
"""
Near real-time Grok Build token/cost companion (outside the TUI).

Tails session updates.jsonl, polls signals.json + events.jsonl, applies
published xAI rates, serves a local dashboard on http://127.0.0.1:8765/

  python scripts/live_dashboard.py
  python scripts/live_dashboard.py --port 8765 --session-id <uuid>

Implementation is split under ``token_telemetry.session`` and
``token_telemetry.server``; this module wires them and re-exports the
public surface for shims and ``python -m token_telemetry``.
"""

from __future__ import annotations

import argparse
import sys
import threading
import webbrowser
from http.server import HTTPServer
from typing import Optional

from token_telemetry.tokenizer import preload as _preload_tokenizer, tokenizer_info

# Session discovery + monitor
from token_telemetry.session.discover import (
    ACTIVE_SESSIONS,
    SESSIONS_ROOT,
    format_age,
    list_session_dirs,
    list_sessions_for_ui,
    read_active_session_ids,
    read_active_sessions_meta,
    resolve_session_dir,
)
from token_telemetry.session.monitor import (
    API_ROUNDS,
    MAX_CONTEXT_POINTS,
    MAX_READ_CHUNK,
    MAX_TURNS,
    MONITOR,
    SessionMonitor,
    _enrich_system_prompt,
    _enrich_user_prompt,
    _slim_signals,
)

# HTTP server
from token_telemetry.server.http import (
    DASHBOARD_DIR,
    DASHBOARD_HTML,
    REPO_ROOT,
    Handler,
    _STATIC_MIME,
    background_poller,
)


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Grok token telemetry live dashboard")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--session-id", help="Pin to a session UUID")
    ap.add_argument("--no-open", action="store_true", help="Do not open browser")
    args = ap.parse_args(argv)

    # Load HF Grok-2 tokenizer once at startup (weights for In/Out pro-rata).
    try:
        mode = _preload_tokenizer()
        info = tokenizer_info()
        src = info.get("source") or info.get("hf_id") or ""
        print(f"tokenizer: {mode}" + (f"  [{src}]" if src else ""))
        if info.get("load_error") and mode == "bytes4":
            print(f"  tokenizer fallback: {info['load_error']}", file=sys.stderr)
    except Exception as e:
        print(f"tokenizer preload skipped: {e}", file=sys.stderr)

    d = resolve_session_dir(args.session_id)
    if d:
        MONITOR.attach(d)
        print(f"tracking session {d.name}")
        print(f"  updates: {d / 'updates.jsonl'}")
    else:
        print("no session yet — will attach when one appears", file=sys.stderr)

    stop = threading.Event()
    th = threading.Thread(target=background_poller, args=(stop,), daemon=True)
    th.start()

    # Single-threaded: concurrent snapshot rebuilds were a major RAM amplifier
    httpd = HTTPServer((args.host, args.port), Handler)
    url = f"http://{args.host}:{args.port}/"
    print(f"dashboard: {url}")
    print(f"history:   {url}history")
    print("Ctrl+C to stop")
    if not args.no_open:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        stop.set()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
