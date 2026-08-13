# Grok Build Telemetry

Live companion dashboard for **[Grok Build](https://x.ai)** sessions: exact token fields Grok already writes, cache vs uncached split, official `$` ticks, and list-price estimates — outside the TUI.

> Package name on PyPI-style installs: `token-telemetry` (`pip install -e .` / `token-telemetry` CLI).

```
~/.grok/sessions/.../updates.jsonl  ──tail──►  live dashboard  ──►  http://127.0.0.1:8765/
```

## Features

- **Context size** live (`_meta.totalTokens` + `signals.json`)
- **Official $** from `costUsdTicks` (1 USD = 10¹⁰ ticks)
- **Estimate $** from published xAI rates (≤200k / >200k); **model-aware cache** (4.5 vs 4.6)
- **Round tree** — user / system / tools / thoughts with In · Cached · Out · $ (Standard / Expert density)
- **Cost chart** — composition per round (drill into calls; harness In green)
- **Context over time** chart (200k rate-cliff line)
- Header **KV** chip (warm / stale idle / miss) + context pressure bar
- Latency & tools from `signals.json`

## Requirements

- Python **3.10+**
- Grok Build sessions on this machine (`~/.grok/sessions/`)
- Optional but recommended: `transformers` + local Grok-2 tokenizer under `vendor/grok-2-tokenizer/` (offline weights for In/Out pro-rata). Falls back to bytes÷4 if unavailable.

## Install

```bash
git clone https://github.com/foudress/grok-build-telemetry.git
cd grok-build-telemetry
python -m venv .venv
# Windows: .venv\Scripts\activate
# Unix:    source .venv/bin/activate
pip install -e .
```

## Run

```bash
token-telemetry
# equivalent:
python -m token_telemetry
```

Windows helper (same process):

```powershell
.\launch_dashboard.ps1
# optional: .\launch_dashboard.ps1 -Port 8765 -SessionId <uuid>
```

Legacy (no install):

```bash
python scripts/live_dashboard.py
python scripts/live_dashboard.py --port 8765 --session-id <uuid> --no-open
```

Opens **http://127.0.0.1:8765/** — leave it beside Grok Build.

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
- Official server `$` (ticks) may differ slightly; both are shown

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
  tokenizer.py                   # offline token weights
  hierarchy/                     # session reconstruction (bootstrap, builder, …)
  pricing/                       # list rates + usage reconstruct
  session/                       # discovery + live tail/monitor
  server/                        # local HTTP API + static files
scripts/                         # thin shims → token_telemetry.* (legacy imports)
  extract_session_events.py      # offline batch extract
  live_dashboard.py, hierarchy.py, pricing.py, tokenizer.py
dashboard/                       # zero-build UI
  index.html
  css/                           # tokens, layout, components, charts
  js/                            # main, tree, charts, sessions, fmt
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
