# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

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
