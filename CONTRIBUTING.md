# Contributing

Thanks for helping improve **Grok Build Telemetry** (`token-telemetry`).

## Setup

```bash
git clone https://github.com/foudress/grok-build-telemetry.git
cd grok-build-telemetry
python -m venv .venv
# Windows: .venv\Scripts\activate
# Unix:    source .venv/bin/activate
pip install -e .
```

Run the dashboard:

```bash
token-telemetry
# or
python -m token_telemetry
# or (legacy)
python scripts/live_dashboard.py
```

## Scope rules

1. **One focused change per PR** — easier review, safer accounting.
2. **Do not change Round 1 / System accounting or UI reconstruct** unless the PR description explicitly says so. That surface is frozen for pixel-correct cold bootstrap.
3. **Prefer pure functions** for pricing math; keep `HierarchyBuilder` orchestration thin.
4. **Zero-build UI** — modular HTML/CSS/JS only (no React/npm build). Design tokens live in `dashboard/css/tokens.css`.
5. **No personal paths or session dumps** in the repo (prompts, `updates.jsonl`, machine-specific cwd encodings).

## Where to edit

| Area | Path |
|------|------|
| Package entry / CLI | `token_telemetry/__main__.py`, `token_telemetry/live_dashboard.py` |
| Hierarchy / bootstrap | `token_telemetry/hierarchy/` (`builder.py`, `bootstrap.py`, …) |
| Pricing rates & reconstruct | `token_telemetry/pricing/` (`rates.py`, `reconstruct.py`) |
| Session discovery & monitor | `token_telemetry/session/` (`discover.py`, `monitor.py`) |
| HTTP API & static serving | `token_telemetry/server/http.py` |
| Tokenizer weights | `token_telemetry/tokenizer.py` |
| Dashboard UI | `dashboard/index.html`, `dashboard/css/*`, `dashboard/js/*` |
| Offline batch extract | `scripts/extract_session_events.py` |
| Legacy import shims | `scripts/*.py` (re-export package modules; prefer editing `token_telemetry/`) |

Import the package (`from token_telemetry.hierarchy import HierarchyBuilder`, etc.). Shims under `scripts/` exist so older `import hierarchy` / `python scripts/live_dashboard.py` paths keep working.

## Pricing

- List rates live in `token_telemetry/pricing/rates.py` (`RATES_BY_FAMILY`).
- Detect the session model; **do not** hardcode 4.5 cache when pricing 4.6.
- Reconstruct math stays model-agnostic — use `pricing_model_scope` / `rates_for`, do not thread `model` through `reconstruct.py` (R1 freeze).
- Public rate tests: `pytest test/test_model_rates.py`.

## Style

- Python 3.10+, type hints on new public functions.
- No drive-by refactors in feature PRs; open a dedicated PR for structure moves.
- Keep API routes stable: `/api/state`, `/api/sessions`, `/api/session`, `/api/health`.

## License

By contributing, you agree your changes are licensed under the MIT License (see `LICENSE`).
