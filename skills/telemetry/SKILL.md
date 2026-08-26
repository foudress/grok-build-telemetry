---
name: telemetry
description: "Open Grok Build Token Telemetry dashboard (kill :8765 if busy, clean relaunch). Use when the user runs /telemetry, asks to open/launch/start the telemetry dashboard, or ends a prompt with /telemetry."
user-invocable: true
argument-hint: ""
metadata:
  short-description: "Launch Token Telemetry dashboard"
---

# /telemetry

**Do this first, before any other work from the user message.** Minimal turn: run the launch command, confirm, then continue the rest of the user prompt (if any). Do **not** explore the repo, read install docs, or narrate a plan.

## Launch (exact — OS independent)

Resolve `ROOT`, then run **`launch_dashboard.py --detached`** with the host Python (prefer repo `.venv`).

```text
ROOT resolution order:
1) $GROK_TELEMETRY_ROOT
2) ~/.grok-telemetry/install.json → .root
3) ~/Grok/token-telemetry
4) ~/Grok/grok-build-telemetry

PY resolution (critical on Windows — **never** use `WindowsApps\…PythonSoftwareFoundation…`):
1. `$ROOT/.venv/Scripts/python.exe` or `$ROOT/.venv/bin/python` if present
2. else `py -3.12` / `py -3` (Windows) or `python3` (Unix)
3. `launch_dashboard.py` itself will create/repair `.venv` and skip Store stubs

Command:
  "$PY" "$ROOT/launch_dashboard.py" --detached
Optional if $GROK_SESSION_ID is set:
  "$PY" "$ROOT/launch_dashboard.py" --detached --session-id "$GROK_SESSION_ID"
```

Shell examples (pick the host OS):

**bash / zsh**

```bash
ROOT="${GROK_TELEMETRY_ROOT:-}"
if [ -z "$ROOT" ] && [ -f "$HOME/.grok-telemetry/install.json" ]; then
  ROOT=$(python3 -c "import json,pathlib; print(json.load(open(pathlib.Path.home()/'.grok-telemetry'/'install.json'))['root'])")
fi
for c in "$ROOT" "$HOME/Grok/token-telemetry" "$HOME/Grok/grok-build-telemetry"; do
  [ -n "$c" ] && [ -f "$c/launch_dashboard.py" ] && ROOT="$c" && break
done
PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY=$(command -v python3 || command -v python)
CMD=("$PY" "$ROOT/launch_dashboard.py" --detached)
[ -n "$GROK_SESSION_ID" ] && CMD+=(--session-id "$GROK_SESSION_ID")
"${CMD[@]}"
```

**PowerShell**

```powershell
$root = $env:GROK_TELEMETRY_ROOT
if (-not $root) {
  $m = Join-Path $env:USERPROFILE ".grok-telemetry\install.json"
  if (Test-Path $m) { $root = (Get-Content $m -Raw | ConvertFrom-Json).root }
}
foreach ($c in @($root, (Join-Path $env:USERPROFILE "Grok\token-telemetry"), (Join-Path $env:USERPROFILE "Grok\grok-build-telemetry"))) {
  if ($c -and (Test-Path (Join-Path $c "launch_dashboard.py"))) { $root = $c; break }
}
$py = Join-Path $root ".venv\Scripts\python.exe"
if (-not (Test-Path $py)) {
  # Prefer py launcher — NEVER fall back to WindowsApps Store stub
  $py = (& py -3.12 -c "import sys; print(sys.executable)" 2>$null)
  if (-not $py) { $py = (& py -3 -c "import sys; print(sys.executable)" 2>$null) }
}
if (-not $py) { throw "No usable Python. Install 3.10+ from python.org or run docs/install.md" }
$argList = @("$root\launch_dashboard.py", "--detached")
if ($env:GROK_SESSION_ID) { $argList += @("--session-id", $env:GROK_SESSION_ID) }
& $py @argList
```

The launcher **always kills** whatever is listening on port **8765**, then starts a clean detached instance (Windows: survives the agent Job Object), **pins `GROK_SESSION_ID` when set**, opens `http://127.0.0.1:8765/?session=<id>`, and does **not** rebuild every session on disk at startup.

## Reply

- **With user prompt after `/telemetry`:** one short line only (`dashboard: http://127.0.0.1:8765/`), then **immediately** do their request.
- **`/telemetry` alone:** one short confirmation line, same URL. Stop.

## Do not

- Skip the kill/relaunch (no “already running, reuse”)
- Run install / clone / pip from this skill
- Dump logs or long status
- Mention unrelated internal workflows
