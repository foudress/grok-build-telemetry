# token-telemetry

Grok Build token telemetry dashboard (ledgers must stay pixel-honest).
Stack: Python 3.12 + static dashboard
Live: `http://127.0.0.1:8765/` via `/telemetry` or `python launch_dashboard.py`

## Open (disk is memory — chat is not)

| when | file |
|------|------|
| no goal / resume | `.grok/state.md` |
| after goal | `.grok/map.md` — one row → one source file |
| known pitfall | `.grok/learn.md` |
| ledger identities | `docs/ledger.md` |
| this feature | `.grok/work/<slug>.md` named in `state.md` |

First message already has a feature → skip `state.md`; open the map row.
Else read `state.md` once, then `goal?`.

## This repo

- **Three In ledgers — do not unify** unless the user unfreezes in the same session: Official API · Tree In (`user+harness+miss`) · System R1 (hist+ToolDef jump).
- Window identity: `System + R1 In = context_end`. Bill: `System + tree_in = paid_unc`.
- Compact glue: match phrases only in **head[:800]**.
- `CACHE_VER` in `session/calc_cache.py` (30). New accounting `.py` must be listed in `_CODE_FILES`.
- Feature gates: `token_telemetry/features.py` **and** `dashboard/js/features.js` stay in sync (History/Gantt/tok/s/Graph/Period I/O $/M off).
- Verify: `pytest test`. Reconstruct order is frozen.
- Never use WindowsApps `python`; prefer repo `.venv` / `py -3.12`.

## Quality (non-negotiable)

- **Root cause, not a patch.** If the fix is a special case, extra flag, second path, retry, or “just in case” guard — stop. Change the owner so that *class* of bug cannot happen.
- **One owner per fact.** Don’t re-sum ledgers in the UI. Readers use `breakdown.*` after merge; fallback only if the field is absent.
- **Shrink the surface.** No speculative stubs, unused params, commented-out code, or wrappers that only forward.
- **Fit `map.md`.** Accounting math lives in `hierarchy/` + `session/calc_cache.py` + `pricing/reconstruct/`. Graph sidecar stays in `token_telemetry/graph/*`.
- **Match the house.** Tests lock identities (`test_ledger_identities.py`), not live SIDs or `_helper` names.
- **Diff = the feature.** Don’t change R1/System accounting without an explicit unfreeze.
- **Prove it.** `pytest test` (and freeze check if goldens exist). `0` is a present value — never `or` / `||` it away.

## Session

- 1 feature / session. Green → `/end`.
- Explore: map row → named file → path + symbol + `head_limit`. Never scan `out/` or all sessions jsonl “to be sure”.

## Git

- `AGENTS.md` is **public** (harness injects it). Never put home directories, usernames, emails, tokens, machine-local paths, or session leftovers (Last/Next) in it. Those go in `.grok/` (gitignored).
- `/end`: overwrite `.grok/state.md` (local); commit the feature; include `AGENTS.md` only if it is still path-clean; **push** `origin HEAD`.
- Never commit `.grok/`, secrets, `out/_probe*`, `__pycache__/`, `.venv/`.
- Conventional commits, English, imperative. No empty commit, no force-push, no amend of pushed commits, no PR unless asked.
