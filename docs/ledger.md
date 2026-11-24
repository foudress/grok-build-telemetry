# Round ledger — UI field catalog

After `_merge_bootstrap_into_breakdown`, the round **is** the ledger. Monitor,
tree, charts, period, and subagent cards **read** these fields. They recompute
`user+harness+miss` only when a field is absent (legacy payload).

Three ledgers stay **distinct**. Unifying them breaks the UI.

| Ledger | Where | Formula | UI |
|---|---|---|---|
| **A. Official API** | `turn_completed.usage` | `uncached = inputTokens − cachedReadTokens` | KPI2 verso « Session tokens », Official `$`, `$/M` ≤190k |
| **B. Tree In** | `breakdown.tree_in_*` | `user_in + harness_in + cache_miss` (Compact Out already in `user_in`) | Round header In, Cost Per Turn I/O In, attribution |
| **C. System R1** | `system_prompt.tokens_in` | hist TokZ + ToolDef (`tt_high − hist − user − call1_out`) | System card, Sys bar |

Identities (frozen):

```
tree_in              = user_in + harness_in + miss
miss                 = max(0, paid_unc − [System si R1] − user − harness)
R1: System + tree_in = paid_unc          # bill
R1 ctx line          = 0 → context_end   # window
Compact Out          ∈ User In. Compact pre-read = anchor call cache + In tail (else warm Cached) + Out. A round miss chip does not flip the window to In.
Last call Cached     = 0
R1 User Cached       = 0
Call In              = harness
Agent line parent    = child *round* I/O, never System, never lifetime
TokZ                 = meta (eol chip), never billed In/Out (TokF)
```

Zero is present (v1.5). Do not replace a real 0 with another ledger.

- Measured `paid_unc` 0 clamps Tree In. Compact Out is not eaten. No usage and no paid key: do not clamp. The clamp does not write `model_steps[].tokens_in`.
- Live Out with no TokZ and no official Out is 0. A weight that rounds away to 0 still floors at 1.
- Period `official_usd` is the tick sum, including 0. Estimate stays on `estimate_usd`.
- User Cached in the tree is the stamp. Continuity peels cache miss off tokens, not off `tokens_cached`.
- A present Call In of 0 stays 0. The harness sum is the fallback only when `tokens_in` is absent. Call In is still the harness figure when the step was stamped from harness; the clamp does not rewrite the step.
- Codex cache-write rate 0.0 bills 0, not the input rate.
- The >200k list rate follows each LLM call's `context_start`. Round `context_end` does not reprice the other calls. Header Cached tokens stay the official sum. Header Cached $ is each call's share at that call's tier, including the omitted last prefix.

## System attribution across connectors

Grok Build records the opening history and the context checkpoints needed for
the System R1 identities above. Codex records per-call prompt and cache usage.
Some rollouts also contain `session_meta.base_instructions`, a fragment of the
effective prompt. Antigravity records per-call usage in `gen_metadata`, along with
conversation steps. Neither trace supplies the billed size of the system and
developer messages, tool definitions, or their split across the cache.

For exact attribution, the connector would need the fully assembled first
request with provider token counts by component and cache status. Measured
system input could then populate the existing System card, with a separate
unattributed balance whenever a component is missing. A count from locally
visible instructions can be shown as an estimate, independent of the call totals.

## Canonical round fields

```
breakdown.{tree_in_tokens, tree_in_usd,
           user_in_tokens, user_in_usd,
           harness_in_tokens, harness_in_usd,
           cache_miss_in_tokens, cache_miss_in_usd,
           system_in_tokens, system_in_usd,
           cached_tokens, cached_usd,
           output_tokens, output_usd,
           round_total_peeled_system, api_total_usd,
           llm_thought_summary_*, llm_reasoning_encrypted_*,
           llm_out_to_harness_*, llm_out_to_user_*}
user_prompt.{tokens_in, prompt_tokens_in, compact_out, compact_out_in_user_in,
             prev_llm_answer, tokenizer_tokens}
system_prompt.{tokens_in, parts[], tool_definitions_tokens, message_residual_tokens}
model_steps[].{tokens_in, tokens_cached, tokens_out, children[]}
```

## KPI session (`main.js` `render`)

| DOM | Source |
|---|---|
| `#ctxNow` | `live.context_tokens_ui` ?? `context_tokens` ?? `signals.contextTokensUsed` |
| `#ctxSub` | `signals.contextWindowTokens` **or 500000 hardcoded** |
| `#costOfficial` | `totals.official_usd` (sub tab: `sub.official_usd`) |
| `#costOfficialSub` `$/M` | `view.turns[]` — skip peak ctx > **190000**, imply ±$0.02 |
| KPI2 verso tokens | `sumTurnIoTokens(turns)` — all turns, not 190k |
| `#costEstimate` | `totals.estimate_usd` = parent `_sum_rounds_estimate` (api_total R1) + child tabs |
| estimate subline | `parent_estimate_usd` + `children_estimate_usd` |
| `#genRate` | last `turns[].gen_tokens_per_sec` |

`totals.official_usd` = Σ turns **unpeeled**. Estimate = parent hierarchy **peeled** + tabs. Do not align the two.

## Cost Per Turn (`turnCostParts`)

- I/O In = `bd.tree_in_usd` / `tree_in_tokens` (never R1 `uncached_in` — recounts System).
- R1: separate **Sys** bar (`peelSystem` / `round_total_peeled_system`).
- Then add `subagents_after` slices on the parent bar. Bar height ≠ round header ≠ KPI.
- Compact / Recap: extra bars, In XOR Cached + Out.
- Tick officiel = `t.official_usd`.
- Frozen quirk: resize re-reads `state.turns/rounds` (main), not the active sub tab.

## Round tree

| Line | Field | Forbidden |
|---|---|---|
| Round In | `bd.tree_in_tokens` | re-sum user+calls unless `tree_in` absent |
| Round Cached/Out/$ | `r.cached_read_tokens` / `output_tokens` / `estimate_usd` | — |
| User header In | `bd.user_in_tokens` (prompt + prev billed + Compact Out) | TokZ |
| Cache miss | `cache_miss_in_*` | under User |
| User Cached R1 | **0** | invent prior |
| Enc TokZ | `tokenizer_tokens` if **> 0** else `encrypted_tokens` | treat `0` as stamp |
| System | `parts` + Tool Definitions if residual | 8.2k, Σ off_unc |
| Call In | `step.tokens_in` | double peel compact |
| Sub Agent N | `sa.tokens_in/cached/out` of the **round** | lifetime + System |

Frozen quirks: System spliced in HTML looking for `>Round 1<`. Enc `prevAbsorbed` hardcoded `false`. Context chart User Y uses `display_in_tokens` (may ≠ billed User In). Compact glue = `head[:800]` only. `MAX_ROUNDS_RETAINED` / wire window = **4000**. ToolDef jump is System remainder, never Round 1 tree In.

## Period (`/api/aggregate`)

Four cards: Total In / Cached / Out / All. All `$` = sum of the 3 costs (not official). Period totals exclude native child dirs except `_fold_kids` (workflow mapper→workers, so parent D/W/M is not zero). Recap/compact I/O added after peel. Session grain labels `1` / `1.1`. Reset calc bumps `cache_gen`; the period client refetches.

## Gated surfaces

History, Gantt, tok/s, Graph, I/O $/M step: flags `False` in `features.py` + `features.js`. Isolate, do not rewrite.
