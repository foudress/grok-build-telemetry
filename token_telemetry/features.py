"""UI/API feature gates for surfaces still in polish/debug.

Flip a flag to ``True`` to re-enable that surface. Code for gated features
stays in the tree; only routes and dashboard chrome are suppressed for V1.0.0.
"""

from __future__ import annotations

# Mutation History — /history + /api/history (chat_history.jsonl watch)
MUTATION_HISTORY = False

# Period hourglass Gantt — ⌛ unit on Daily/Weekly/Monthly
GANTT = False

# Tokens-per-second charts — session Context tok/s + period tok/s
TOKS_PER_SEC = False

# Agent Animation Graph — /graph page + session Context "Graph" mode + /api/graph*
AGENT_ANIMATION_GRAPH = False

# Period D/W/M step chart — estimated In/Cached/Out $ per session (square jumps)
# Off for v1.0.0 UI; flip True (+ features.js) to re-enable.
PERIOD_IO_PRICE_STEP = False

WIP_NOTE = (
    "Still in polish/debug for UI integration. "
    "Implementation remains in the repo; enable via token_telemetry.features "
    "(and dashboard/js/features.js)."
)


def wip_html(title: str) -> bytes:
    """Minimal page when a gated HTML route is hit directly."""
    body = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>{title} · WIP</title>
  <style>
    body {{ font-family: system-ui, sans-serif; background:#0f1115; color:#e8eaed;
           max-width:40rem; margin:4rem auto; padding:0 1.25rem; line-height:1.5; }}
    a {{ color:#8ab4f8; }}
    .badge {{ display:inline-block; font-size:0.75rem; padding:0.15rem 0.5rem;
              border:1px solid #5f6368; border-radius:999px; color:#9aa0a6; }}
    h1 {{ font-size:1.35rem; margin:0.75rem 0 0.5rem; }}
    p {{ color:#bdc1c6; }}
  </style>
</head>
<body>
  <span class="badge">polish / debug</span>
  <h1>{title}</h1>
  <p>{WIP_NOTE}</p>
  <p><a href="/">← Back to telemetry</a></p>
</body>
</html>
"""
    return body.encode("utf-8")
