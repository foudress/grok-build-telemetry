# Grok Build Telemetry

Live companion dashboard for **[Grok Build](https://x.ai)** sessions: exact token fields Grok already writes, cache vs uncached split, official `$` ticks, and list-price estimates — outside the TUI.

> Package: `token-telemetry` **v1.0.3** (`pip install -e .` / `token-telemetry` CLI).

```
~/.grok/sessions/.../updates.jsonl  ──tail──►  live dashboard  ──►  http://127.0.0.1:8765/
```

## Features

- **Context size** live (`_meta.totalTokens` + `signals.json`)
- **Official $** from `costUsdTicks` (1 USD = 10¹⁰ ticks)
- **Estimate $** from published xAI rates (≤200k / >200k); **model-aware cache** (4.5 vs 4.6). Session estimate = parent rounds + each sub-agent tab (one `$` per agent, not spawn+resume)
- **Round tree** — user / system / tools / thoughts with In · Cached · Out · $ (Standard / Expert density). Recap / Compact / System ledgers right-pad like Round heads. Recaps and compacts sit in trigger order (`agent_ms`)
- **System card** — history parts (system / user info / reminders / MCP) plus one **Tool definitions + Message** bucket so `System + R1 In = context_end` (not a hardcoded 8.2k, not official `Σ uncached`)
- **Per-call cache** — last LLM call Cached = 0 (no harness after it). R1 ctx line is `0 → end`. R1 official Cached is shared across every call except last (Call 1 = System + User)
- **Compact / Recap** — compact is Cached **or** In (miss) plus compressed **Out**; recaps and compacts keep trigger order. Compact continuation glue is not treated as the first user prompt
- **Cost chart** — **I/O · Parts · Tools** stacks on rounds, drill, or **By label**. Recap stays In/Cached/Out in I/O; Parts/Tools fold it. Wheel-zoom only on the X-axis (full unzoom by default). Sub-agent $ stacks on the parent (violet)
- **Daily / Weekly / Monthly** — same I/O · Parts · Tools. Parts/Tools use our hierarchy cats, bucketed by each turn’s time (not the whole session). Timeframe / Cumulative / **Normalized**; grains including **Session**. Session list is parent → sub-agent. Titles from `summary.json` `session_summary` (UTF-8 BOM-safe). **← Back** returns from a session opened out of a period. Closed sessions are **cached on disk**; **Reset calc** forces a full recompute. Long period calcs stream **`X/N` progress** (full-page or chart-local); warm cache option switches skip the graph spinner. Total All `$` matches In+Cached+Out; By label always uses the observation window (not Cumulative prefixes)
- Session picker shows the summary title (not the UUID) in the header
- **Sub-agents** — child sessions are peeled out of the parent LLM-call math, then shown as tabs + Sub Agent N rows (In / Cached / Out / $). **Sub Agent N Sys** sits after the spawn LLM (In = copy of that tab’s System header; display-only, not Round 2 In). Click the line to open the tab; **Sub Agent N RN** opens Round N. `get_command` In is the parent-facing tool_result body. First-round prompt is **Super Agent** (not User / not System Other), including resume tabs. Period totals skip child dirs (parent bill already includes them)
- **Context over time** chart (200k rate-cliff line) — Session view only
- Header **KV** chip (warm / stale idle / miss) + context pressure bar
- Latency & tools from `signals.json`

### Gated (in tree — polish/debug)

These surfaces ship in the codebase but are **off by default** for v1.0.0 UI. Flip flags in `token_telemetry/features.py` and `dashboard/js/features.js` to re-enable:

| Surface | What it is |
|---------|------------|
| **Mutation History** | `/history` — tails `chat_history.jsonl` for prefix mutations vs append |
| **Gantt (hourglass)** | Period ⌛ timeline of session duration (work vs wait) |
| **Tok/s** | Tokens-per-second charts (session Context + period) |
| **Agent Animation Graph** | `/graph` + session Context **Graph** mode (file activity animation) |
| **Period I/O $/M** | Daily/Weekly/Monthly step chart of estimated In/Cached/Out `$` per 1M tokens |

Direct hits to gated pages show a short polish/debug note; gated APIs return `503` with `{ "gated": true }`.

## Requirements

- Python **3.10+**
- Grok Build sessions on this machine (`~/.grok/sessions/`)
- Optional but recommended: `transformers` + local Grok-2 tokenizer under `vendor/grok-2-tokenizer/` (offline weights for In/Out pro-rata). Falls back to bytes÷4 if unavailable.

## Install (Grok Build — one line)

Paste in Grok Build (agent follows the doc):

```text
Follow https://github.com/foudress/grok-build-telemetry/blob/main/docs/install.md exactly.
```

Works on **Windows, macOS, and Linux**. Clones (if needed), creates `.venv`, `pip install -e .`, and installs **`/telemetry`** under `~/.grok/skills/telemetry/`.

Manual equivalent: [docs/install.md](docs/install.md).

```bash
git clone https://github.com/foudress/grok-build-telemetry.git
cd grok-build-telemetry
python3 bootstrap_install.py   # or: python bootstrap_install.py
```

## Run

**From Grok Build:** end your first prompt with **`/telemetry`**, or run `/telemetry` alone. The agent kills anything on port **8765**, starts a clean detached dashboard (Windows-safe under Grok’s Job Object), **pins the current `GROK_SESSION_ID`**, opens the browser to `http://127.0.0.1:8765/?session=<id>`, then continues your prompt.

First paint only recalculates the **pinned session** (not every session on disk). The full recent picker loads when you open the session dropdown.

```bash
token-telemetry
# equivalent:
python -m token_telemetry
```

Launcher (used by `/telemetry`):

```bash
python3 launch_dashboard.py              # foreground (Ctrl+C)
python3 launch_dashboard.py --detached   # background (survives agent shell exit)
# optional: --port 8765 --session-id <uuid> --no-open
# If --session-id is omitted, GROK_SESSION_ID is used when set.
```

Legacy (no install):

```bash
python scripts/live_dashboard.py
python scripts/live_dashboard.py --port 8765 --session-id <uuid> --no-open
```

Opens **http://127.0.0.1:8765/** (with `?session=` when pinned) — leave it beside Grok Build.

## Pricing estimates

List rates (docs.x.ai). Session model is read from `signals.modelsUsed` / `_meta.modelId` / `usage.modelUsage` / `chat_history.model_id`. **Cached input is the only 4.5↔4.6 delta.**

| Model | Context | Input /1M | Output /1M | Cached input /1M |
|-------|---------|-----------|------------|------------------|
| grok-4.5 | ≤ 200k | $2 | $6 | $0.30 |
| grok-4.5 | > 200k | $4 | $12 | $0.60 |
| grok-4.6 | ≤ 200k | $2 | $6 | $0.50 |
| grok-4.6 | > 200k | $4 | $12 | $1.00 |

- `uncached = inputTokens − cachedReadTokens` (cache ⊆ input)
- `$ ≈ uncached×input + cache×cache_rate + output×out`
- **Tier** is per LLM call from `context_start` (not multi-call sum)
- Reasoning is **not** billed on top of output (`totalTokens ≈ input+output` in logs)
- Attribution Out: `Reasoning (Thought+Enc) + LLM→Harness + LLM→User = official Out` (tree still shows Thought and Enc as separate lines)
- Official server `$` is `costUsdTicks / 10¹⁰`. List-rate estimate can diverge when the harness stamps a different internal scale; both cards are shown

## Data source

Per session directory:

```text
~/.grok/sessions/<encoded-cwd>/<session-id>/
  updates.jsonl      # stream + turn_completed.usage
  signals.json       # latency, tools, context window
  events.jsonl       # phases
  chat_history.jsonl # bootstrap / tool results / reasonings
```

### Stream chunks

| `sessionUpdate` | Meaning | Token field |
|-----------------|---------|-------------|
| `agent_thought_chunk` | Thought stream fragment | `_meta.totalTokens` = **context snapshot** (not per-chunk gen) |
| `agent_message_chunk` | Answer stream fragment | same |
| `user_message_chunk` | User prompt text | usually no totalTokens |

On stream chunks, `totalTokens` stays flat during generation and **jumps** when context grows (e.g. after tools). Official generation counts appear on **turn end**.

### Official usage (`turn_completed.usage`)

`inputTokens`, `outputTokens`, `reasoningTokens`, `cachedReadTokens`, `totalTokens`, `apiDurationMs`, `modelCalls`, `costUsdTicks`, …

### Cost ticks

- `cost_usd = costUsdTicks / 10_000_000_000`
- Integer ticks allow summing without float drift
- `0` / missing often means cost was not stamped (e.g. some pool/OAuth paths)

## Offline extract (batch)

```bash
python scripts/extract_session_events.py
python scripts/extract_session_events.py --session-id <uuid> --summary
```

Writes `out/<session-id>-events.jsonl` and summary JSON.

## Project layout

```text
token_telemetry/                 # installable package
  __main__.py                    # token-telemetry / python -m token_telemetry
  live_dashboard.py              # CLI wiring (session monitor + HTTP server)
  features.py                    # UI/API feature gates (History/Gantt/tok/s/Graph/I/O $/M)
  tokenizer.py                   # offline token weights
  hierarchy/                     # session reconstruction (bootstrap, builder, …)
  pricing/                       # list rates + usage reconstruct
  session/                       # discovery, live tail, period aggregate
  graph/                         # agent animation graph (gated)
  server/                        # local HTTP API + static files
bootstrap_install.py             # venv + pip install -e . + /telemetry skill
install_skills.py                # copy skills/telemetry → ~/.grok/skills
launch_dashboard.py              # kill :8765 + clean start (used by /telemetry)
skills/telemetry/                # Grok Build slash skill source
docs/install.md                  # OS-independent one-line install procedure
scripts/                         # thin shims → token_telemetry.* (legacy imports)
  extract_session_events.py      # offline batch extract
  live_dashboard.py, hierarchy.py, pricing.py, tokenizer.py
dashboard/                       # zero-build UI
  index.html, history.html, graph.html
  css/                           # tokens, layout, components, charts, …
  js/                            # main, tree, charts, sessions, period, fmt, features, …
vendor/grok-2-tokenizer/         # offline tokenizer assets (see vendor note)
```

## Development

See [CONTRIBUTING.md](CONTRIBUTING.md).

```bash
pip install -e .
token-telemetry
```

## Non-goals

- Not a hosted multi-user SaaS
- Not a replacement for official xAI billing
- Does not inject custom TUI panels into Grok Build

## License

MIT — see [LICENSE](LICENSE).

### Vendor note

`vendor/grok-2-tokenizer/` is third-party tokenizer data used offline for weight estimates. Respect its upstream license/terms when redistributing. The dashboard falls back to a bytes÷4 heuristic if the tokenizer cannot load.
