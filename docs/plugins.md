# Connector plugins

token-telemetry ships three built-in sources:

| Source | API | Ledgers |
|---|---|---|
| **Grok Build** | `/api/*` | Official API, Tree In, System R1 |
| **Codex** | `/api/codex/*` | Separate Codex wire (estimate only) |
| **Antigravity** | `/api/antigravity/*` | Separate Antigravity wire (counts only, estimate stays 0) |

Extra products can add a header tab **without** forking the dashboard or merging into the Grok ledgers.

## Contract

A plugin exposes a connector with:

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | URL slug (`/api/<id>/…`). Reserved: `grok`, `codex`, `antigravity`, `api`, `css`, `js`, … |
| `label` | yes | Header tab text |
| `feed` | yes | Object with `select`, `reset`, `health`, `sessions`, `state_bytes`, `aggregate` |
| `title` | no | `document.title` |
| `context_cliff` | no | Rate-tier notch for the context bar (default UI falls back per source) |
| `last_active_mtime` | no | Callable `() -> float` for future launch preference |

`feed.state_bytes()` and friends should return the **same JSON shapes** Codex already serves (`rounds`, `turns`, `sub_sessions`, `totals`, `signals`, …). Keep `ledger_source` / `source` set to your id. Leave Grok Official / Tree In / System R1 alone.

Module surface — either:

```python
CONNECTOR = {"id": "myapp", "label": "My App", "feed": MyFeed()}
```

or:

```python
def register():
    return {"id": "myapp", "label": "My App", "feed": MyFeed()}
```

## Discovery

Checked on dashboard start (later sources override the same `id`):

1. `~/.grok-telemetry/connectors/<id>/connector.py`
2. `TOKEN_TELEMETRY_PLUGINS` — `os.pathsep`-separated dirs, `connector.py` files, or importable modules
3. setuptools entry points group `token_telemetry.connectors`

List what loaded:

```text
GET /api/sources
```

Failed plugins appear under `errors` and do not crash the server.

## Minimal install

```toml
# your private package pyproject.toml
[project.entry-points."token_telemetry.connectors"]
myapp = "myapp_telemetry:register"
```

```bash
# into the same token-telemetry virtualenv
pip install -e /path/to/myapp-telemetry
python launch_dashboard.py
# → http://127.0.0.1:8765/?source=myapp
```

Or drop a single file:

```text
~/.grok-telemetry/connectors/myapp/connector.py
```

## UI behaviour

Serving `/` injects `window.__TT_SOURCES__`. The header switch is rebuilt from that list. For any non-`grok` source, `fetch('/api/…')` is rewritten to `/api/<id>/…` (except `/api/sources`).
