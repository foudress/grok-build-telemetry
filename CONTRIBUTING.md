# Contributing

Thanks for helping improve **Grok Build Telemetry** (`token-telemetry`).

## Setup

Preferred (Grok Build agent / one-shot): follow [docs/install.md](docs/install.md)
or run `python3 bootstrap_install.py` after clone. That creates `.venv`,
`pip install -e .`, and installs the `/telemetry` skill.

```bash
git clone https://github.com/foudress/grok-build-telemetry.git
cd grok-build-telemetry
python3 bootstrap_install.py   # or: python bootstrap_install.py
```

Manual equivalent:

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# Unix:    source .venv/bin/activate
pip install -e .
python install_skills.py
```

Run the dashboard:

```bash
# From Grok Build: /telemetry  (kill :8765, prefer .venv, open browser)
python3 launch_dashboard.py
token-telemetry
# or
python -m token_telemetry
# or (legacy)
python scripts/live_dashboard.py
```

Feature gates (v1.0.0 UI off by default): `token_telemetry/features.py` and
`dashboard/js/features.js` — keep both in sync (History, Gantt, tok/s, Graph,
Period I/O $/M).

## Scope rules

1. **One focused change per PR** — easier review, safer accounting.
2. **Three distinct In ledgers** (do not unify — see [docs/ledger.md](docs/ledger.md)):
   - **Window:** `System + R1 In = context_end`. The R1 **ctx line** is `0 → context_end`.
   - **Bill:** `System + tree_in = paid_unc` (`paid_unc = inputTokens − cachedReadTokens`).
   - **Tree In:** `tree_in = user_in + harness_in + cache_miss` (Compact Out already in `user_in`).
   System card = history parts (TokZ) + ToolDef jump (`tt_high − hist − user − call1_out`), not a hardcoded 8.2k, not official `Σ off_unc`. Call-level cache: last = 0; R1 official Cached is shared across non-last calls (Call 1 = System + User). R1 User Cached = 0. Parent Agent line = child *round* I/O, never System. Lock these in `test/test_ledger_identities.py`, not helper names or a live SID.
3. **Prefer pure functions** for pricing math; keep `HierarchyBuilder` orchestration thin. After `_merge_bootstrap_into_breakdown`, readers (monitor, tree.js, charts.js, period, subagent cards) **read** `breakdown.*` — they do not re-sum In.
4. **Zero-build UI** — modular HTML/CSS/JS only (no React/npm build). Design tokens live in `dashboard/css/tokens.css`.
5. **No personal paths or session dumps** in the repo (prompts, `updates.jsonl`, machine-specific cwd encodings). Local ledger goldens: `python scripts/freeze_ledger.py --check`.
6. **Frozen quirks** (do not “fix” in a cleanup PR): window fallback 500000; `$/M` palier 190k vs 200k cliff; chart resize reads main `state.turns`; context chart User Y via `display_in`; reread detector flags ignored for miss tokens; `_is_first_round` if `system_prompt` present; double ToolDef inject; `API_ROUNDS` = 4000.

## Where to edit

| Area | Path |
|------|------|
| Package entry / CLI | `token_telemetry/__main__.py`, `token_telemetry/live_dashboard.py` |
| Install / `/telemetry` | `docs/install.md`, `bootstrap_install.py`, `install_skills.py`, `launch_dashboard.py`, `skills/telemetry/` |
| Feature gates | `token_telemetry/features.py`, `dashboard/js/features.js` |
| Hierarchy / bootstrap | `token_telemetry/hierarchy/` (`builder.py`, `bootstrap.py`, `finalize.py`, …) |
| Pricing rates & reconstruct | `token_telemetry/pricing/` (`rates.py`, `reconstruct/` package) |
| Session discovery & monitor | `token_telemetry/session/` (`discover.py`, `monitor.py`, `kind.py`, `ids.py`, `subagents.py`, `aggregate.py`) |
| Shared coercions / compact glue | `token_telemetry/util.py`, `token_telemetry/compact_glue.py` |
| Ledger UI catalog | `docs/ledger.md` |
| Agent graph (gated) | `token_telemetry/graph/`, `dashboard/graph.html`, `dashboard/js/graph-*.js` |
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
- Reconstruct / cache / R1 ctx: `pytest test/test_cache_reconstruct.py test/test_cache_miss_call1.py test/test_finalize_anchor.py test/test_system_window.py`.
- Ledger identities (tree_in, R1 bill, Compact Out ∈ user, last Cached=0, Agent ≠ System): `pytest test/test_ledger_identities.py`.
- Continuity / gates: `pytest test/test_prev_llm_continuity.py test/test_features.py`.
- Full public suite: `pytest test` (only whitelisted files under `test/` are tracked).
- After accounting edits: `python scripts/freeze_ledger.py --check` (local goldens; skip if none). `CACHE_VER` in `session/calc_cache.py` must stay listed with every new accounting file in `_CODE_FILES`.

## Style

- Python 3.10+, type hints on new public functions.
- No drive-by refactors in feature PRs; open a dedicated PR for structure moves.
- Keep API routes stable: `/api/state`, `/api/sessions`, `/api/session`, `/api/health`, `/api/aggregate`.
- Period aggregates: `pytest test/test_aggregate.py` (do not load hierarchy; skip `session_kind=subagent` in totals).

## License

By contributing, you agree your changes are licensed under the MIT License (see `LICENSE`).
