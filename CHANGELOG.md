# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.3.1] — 2026-08-13

### Fixed

- **System card Message residual** no longer uses official multi-call `Σ uncached` (`off_unc`). That leftover (~6.4k here) was later-call uncached parked as if it were missing bootstrap.
- Identity is now **System + Round 1 In = `context_end`**. Last LLM Out is next-round In (already inside later-call In when not last).
- **Tool definitions + Message** is one window remainder, not a hardcoded 8.2k plus a second Message line:

  `bucket = max(0, context_end − R1_In − System − User info − Reminders − MCP)`

- R1 displayed `context_start` peels as `end − tree` (no Out), so it matches the System total.
- Recap / Compact / System header numbers **right-pad** like Round heads (tag | spacer | ledger | ctx).

### Added

- `test/test_system_window.py` — window identity, peel, overshoot, no invented bucket without `context_end`.

## [0.3.0] — 2026-08-13

### Added

- **Sub-agent sessions** as first-class telemetry: spawn / `get_command_or_subagent_output` links to `session_kind: subagent` dirs.
- Parent `turn_completed.usage` is **peeled** of child API bills (input / cache / output / modelCalls / ticks) before per-LLM-call reconstruct. R1 / System stay frozen.
- Dashboard **Main / Sub N** tabs (cost chart + tree). Main card keeps the harness général $ with parent + children split.
- Tree **Sub Agent N** row between LLM calls: In (green) · Cached (yellow) · Out (red) · $ — one line.
- Cost per Round: child $ **stacked on the parent round** (violet). Drill keeps a dedicated S# bar per agent; click opens that tab.
- Legend hide list **resets** when switching Rounds ↔ Drill (Cached no longer leaks).
- `token_telemetry/session/subagents.py` + `test/test_subagent_peel.py`.

### Changed

- Per-call displayed context on R2+ is anchored to official-share Input; last call stays on the stream window and is clamped so it cannot exceed session end (`_meta.totalTokens` is a harness estimate).
- Session picker marks sub-agent dirs `↳` and auto-follow skips them.

## [0.2.0] — 2026-08-13

### Added

- Session **model detection** so list $ uses the right table: `signals.modelsUsed` / `primaryModelId` / `_meta.modelId` / `usage.modelUsage` / `chat_history.model_id`.
- **Grok 4.6** cache rates: $0.50 / $1.00 per 1M (≤200k / >200k). In/out unchanged ($2/$6 · $4/$12). 4.5 stays $0.30 / $0.60.
- `pricing_model_scope` contextvar so reconstruct stays frozen (no model threaded through the R1 path).
- Dashboard: model badge, KV warm/stale/miss chip, context pressure strip with 200k notch, Standard/Expert tree density, Collapse, cost↔tree drill highlight.
- `test/test_model_rates.py` (normalize / pick_tier / estimate 4.5 vs 4.6).

### Changed

- Cost-per-round **harness** segment is green (`#3ecf8e`, same as In / tool results). Tool-request stays light red; LLM Out→In stays dark green.
- Cost-chart copy: tier is each call’s `context_start`, never the round peak.
- Tree: ledger-aligned round heads; no per-round model id; `›` chevron on the title line; drill highlight is stable (no poll flicker) and clears on Back.
- Pricing is selected via contextvar / `rates_for` so 4.6 cache is not billed at 4.5 rates.

### Removed

- `scripts/_patch_chain.py` (one-shot editor that rewrote the old pricing monolith).
- Dead helpers: unused tokenizer image/char estimators, `SessionMonitor.snapshot` / `_file_key`, `HierarchyBuilder.current_open`, unused reconstruct imports.

## [0.1.0] — 2026-08-11

### Added

- Installable package layout under `token_telemetry/` with console entry `token-telemetry` and `python -m token_telemetry`.
- Modular package modules: `hierarchy/`, `pricing/`, `session/`, `server/`, plus `tokenizer.py` and `live_dashboard.py` wiring.
- Hierarchy submodules: `bootstrap`, `tools_meta`, `recap_compact`, `cache_miss`, `finalize`, `text_metrics`, `hooks`, `compact_out`.
- Pricing submodules: `rates`, `reconstruct`.
- Session/server split: `session/discover`, `session/monitor`, `server/http`.
- Modular zero-build dashboard under `dashboard/css/` and `dashboard/js/`, including design tokens (`css/tokens.css`).
- Professional UI chrome: glass header, metric tiles, reduced-motion/transparency support.
- Tree UX: accordion chevrons, stagger enter on new rounds, hook secondary styling.
- Chart UX: tip fade/translate, legend states, empty states, unit-toggle a11y.

### Changed

- Core logic lives in the package; `scripts/*.py` are thin shims for legacy imports and direct script runs.
- Dashboard UI split out of a single HTML monolith into linked CSS/JS modules.
- `HierarchyBuilder` thinned to orchestration wrappers; heavy paths live in dedicated modules (behavior unchanged).
