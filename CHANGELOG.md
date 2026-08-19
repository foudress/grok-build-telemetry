# Changelog

All notable changes to this project are documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.5.2] — 2026-08-19

Session titles, persisted period calcs, header cleanup, and tool-result
weights without a fake JSON envelope.

### Added

- On-disk **calc cache** (`~/.grok/token-telemetry/calc-cache/`) for period
  hierarchy replay and aggregate rows. Closed sessions are reused until
  `updates.jsonl` / `summary.json` change. Header **Reset calc** wipes disk
  + memory (`POST /api/cache/reset`).
- Shared `pick_session_title` (`session_summary` → `generated_title` →
  `last_turn_summary`).

### Changed

- Tool-result In **weights** tokenize the **content body only** (same as
  grok-build `ToolResult.content`). No ACP envelope
  `{"type":"tool_result","tool_call_id",…}`.
- Header no longer shows a truncated session UUID (`sessionMeta` empty in
  Session view; period still shows the window label).

### Fixed

- `summary.json` with a UTF-8 **BOM** failed to parse (`utf-8`), so titles
  fell back to the session-id prefix. Readers now use `utf-8-sig`.
- Period file cache ignored `summary.json` mtime, so a late title never
  appeared until updates grew. Cache key includes summary fingerprint.

## [0.5.1] — 2026-08-19

Period session hierarchy + Gantt timeline (hourglass) for Daily / Weekly / Monthly.

### Added

- Period **session list** nests parent sessions and their sub-agents (`Session N` / `Sub Agent N`).
- **Hourglass** view next to `$` / `Tok`: Gantt of session duration. Work (LLM/harness) vs wait (user) on one lane.
- Time window zoom (min = full period, max = 5 min) so weekly/monthly never blow the canvas white.
- Pan X+Y, wheel zoom on the plot, Y-axis TradingView zoom (focus under cursor), corner resize handle.
- Multi-select drill (parent includes children), **Show all**, **← Back** after opening a session from a period.
- Nav **loading overlay** so the previous session does not flash.

### Changed

- Session titles prefer `summary.json` **`session_summary`**.
- Gantt X ticks are date-first (`mer. 19`, no month) with a persistent **2h** grid when zoomed in; colliding labels rotate.
- Gantt Y labels use 1 / 2 / 5 / 10 numbering (dots only in the gaps).
- Session picker is a wrapping custom dropdown. **Follow active** always tracks the most recently active main session.
- Period `$` / `Tok` / hourglass are exclusive. Hourglass greys Hourly/Daily, I/O, Parts, Tools, Time, By label.
- Header no longer shows `phase: streaming`.

### Fixed

- Sub-agent bars were one-pixel (only `turn_completed`). Duration is first→last event.
- Legend hide on Gantt no longer drops back to `$`/`Tok` bars.
- Chart tooltips stay in the viewport when the canvas is zoomed.
- Parent→child links no longer draw a stub past the child bar.

## [0.5.0] — 2026-08-19

History watch dashboard, cost-chart I/O · Parts · Tools stacks (session +
period), and thought Out $ on LLM calls.

### Added

- **`/history`** live `chat_history.jsonl` watcher: baselines on first sight,
  then classifies append / tail / mutate / truncate. Prefix mutations (cache
  miss risk) vs streaming tail. Compact-glue badge. Isolated from R1/System
  reconstruct.
- Cost chart **I/O · Parts · Tools** stack + **Rounds / Time · By label**.
  Drill applies the same three stacks (I/O and Parts fold call cats).
- Period **Parts / Tools** replay hierarchy per session (mtime cache). Each
  round / recap / compact is bucketed by **its own timestamp** — a session
  that continues across days does not dump its past into the current bucket.
  Reasoning is **our** Enc/thought split, not API `reasoningTokens`.
- `GET /api/aggregate?stack=parts|tools` and `GET /api/history`.

### Changed

- **Thought Out $** is a real share of official output (TokZ × out rate,
  fitted with Enc / ToolReq / Message). It was hardcoded `$0` (mass parked
  on Enc), so LLM Call tree, drill, and By-label charts showed $0 thought.
- Recap In/Cached/Out stays split in **I/O**; **Parts** and **Tools** fold
  recap (and compact) into one `recap` / `compact` block.
- Cost-chart labels: In / Cached / Out and LLM Out→In keep that casing;
  everything else lowercase. Recap no longer injects a second Cached chip.
- X labels rotate when they would collide; plot height grows so they are
  not clipped. Zoom (and the “scroll to zoom” hint) only on the X-axis
  band. View switches reset to full unzoom unless the user wheel-zoomed.
- Legend wraps with no scrollbar. By-label hide keeps the chip so it can
  be shown again; zero cats are omitted. Timeframe / Cumulative / grain
  are disabled in By-label (Time layout only).
- Tooltips no longer flash across Session ↔ period (owner + stale fetch
  ignored). Hover tip stays while the pointer is on the bar through polls.

### Fixed

- Thought $0 on LLM Call / Parts / Tools By-label (reconstruct `th_usd`).
- Period Parts/Tools used API reasoning ticks instead of hierarchy cats.

## [0.4.1] — 2026-08-15

Partial reconstruct / cache / sub-agent / compact-recap pass. Round 1 System
**card** identity is unchanged (`System + R1 In = context_end`). Call-level
cache, last-call skip, and the R1 ctx line are no longer treated as frozen.

### Changed

- **Per-call Cached** is `Input − Uncached` at that prompt. Last LLM call
  (no harness after it) has **Cached = 0**; its official slice is *not*
  dumped onto Call 1. `Σ display + last_omitted = official cachedRead`.
- **R1 ctx line** is `0 → context_end` (nothing billed as prior cache before
  Call 1). System still lives on its own card.
- **R1 Cached** (official `cachedReadTokens`) is shared across every LLM
  call except last. Call 1 = System In + User [1]; later non-last calls take
  the remainder.
- **Output attribution:** `Reasoning Enc + LLM→Harness + LLM→User = official
  Out`. Leftover Out (including Thought mass) is spread onto Encrypted
  pro-rata Enc TokZ. Thought stays a summary line.
- **Session estimate** for R1 is `System $ + peeled R1` (`api_total_usd`),
  not peeled-only and not System counted twice.
- **Sub-agent tabs** use their own list-rate estimate on the estimate card
  (official ticks stay on the official card). First-round prompt is labeled
  **Super Agent** (not User). Parent-task text is not duplicated as System
  **Other**.
- **Recap vs compact** between rounds is ordered by `agent_ms`. Recaps
  triggered before a compact stay before it (tree + cost chart).
- **Compact bar:** Cached **or** In (hit vs miss), never both, plus **Out**
  = compressed history (`tokens_after`). Compact glue is not treated as the
  first user prompt or System Other.
- **Cost chart:** minimum **8** slots; canvas is not CSS-stretched, so
  tooltips stay above the bars.

### Fixed

- Last-call Cached no longer sits on the last call while Call 1 looks empty.
- Sub-agent sessions were not found when the child cwd folder differs from
  the parent (`~/.grok/sessions/*/<id>`).
- Sub-agent Super Agent header / prompt line dropped tok and $ after Other
  was removed from System — parent task tokens now land on Super Agent [1].
- Encrypted reasoning stacked on one LLM call when `chat_history` stamps
  were sparse.
- Compact rewrite (`This session is being continued…`) overwrote User [1]
  and System Other.
- Child session estimate card showed official ticks instead of list-rate.

### Added

- `test/test_cache_reconstruct.py` — last=0, Call 1 prefix, official-cache
  share, Enc leftover identity.
- `test/test_cache_miss_call1.py` — first-call reread vs compact collapse.
- `test/test_finalize_anchor.py` — stream-window anchor, last-call skip,
  cold Call 1 ctx starts at 0, compact XOR + Out.

### Notes

- Official `$` is still `costUsdTicks / 10¹⁰`. On recent `grok-4.6-build`
  turns those ticks sit at ~0.17× published list; the estimate card stays
  on docs.x.ai rates. That is a harness stamp, not a new divisor.

## [0.4.0] — 2026-08-14

### Added

- Header **Session / Daily / Weekly / Monthly** scope. Session view is unchanged (R1 / System frozen).
- Period aggregates from official `turn_completed.usage` (no hierarchy load):
  - Daily bars: **Hourly** or **15 min**
  - Weekly bars: **Hourly** or **Daily**
  - Monthly bars: **Daily** or **Weekly** (weeks clipped to the month)
  - **Timeframe** vs **Cumulative**, **$** vs **Tok**, stacked **In / Cached / Out**
- KPI cards in period mode: Total In / Cached / Out / All with $ under each.
- Session list instead of the round tree: `Session N · title · In / Cached / Out → $` (click opens that session).
- `GET /api/aggregate?period=&offset=&grain=` plus `token_telemetry/session/aggregate.py` (mtime cache).
- Sub-agent dirs appear in the list (`↳`) but are **excluded from totals/buckets** (already inside the parent bill).
- Horizontal **wheel zoom** on the cost chart (min scale = fit all bars; session and period keep separate scales).
- Cost chart **horizontal scroll** when zoomed; Y-axis is a fixed column so bars clip at the axis, not under it.
- `test/test_aggregate.py` — windows, hourly / 15‑min / weekly-hour buckets, sub-agent exclusion.

### Changed

- Period toolbar is hidden in Session (CSS `[hidden]` no longer overridden by `display: flex`).
- Dollar Y-axis uses ~5–8 nice 1-2-5 ticks (no more $0.50 grids on large period totals).
- In / Cached / Out legend chips work in period views; Y rescales to the visible stack.
- Scroll position is preserved across period poll redraws (no snap back to the left).

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
