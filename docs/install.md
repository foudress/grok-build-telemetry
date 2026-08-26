# Token Telemetry — install (Grok Build agent)

**This file is the single install procedure (OS-independent).**  
A user can paste only:

```text
Follow https://github.com/foudress/grok-build-telemetry/blob/main/docs/install.md exactly.
```

You are a **Grok Build agent** on **Windows, macOS, or Linux**. Execute every step with the host shell. Prefer idempotent commands. After install, daily use is **`/telemetry`** (not this doc).

---

## Hard rules

1. Install **once** (clone + venv + pip + skill). Do **not** launch the dashboard unless the user asked in the same message.
2. Prefer **`python3`**; if missing, use **`python`** (must be **3.10+**).
3. Never invent a second “install skill”. Daily use = `/telemetry` only.
4. Do not print secrets.

---

## What “installed” means

| Path | Role |
|------|------|
| Repo root (default `~/Grok/token-telemetry`) | Source + launchers |
| `<root>/.venv/` | Python env with editable `token-telemetry` |
| `~/.grok/skills/telemetry/` | Slash skill `/telemetry` |
| `~/.grok-telemetry/install.json` | Records `root` for launch resolution |

(`~` = user home on all OS.)

---

## Procedure

### 0. Prerequisites

```text
python3 --version    # or: python --version  → 3.10+
git --version
```

Grok Build sessions are expected under `~/.grok/sessions/` (no extra setup).

### 1. Resolve Python command

```text
Use python3 if available, else python. Call it PY below.
```

### 2. Resolve / obtain repo

Prefer an existing clone. Check in order:

1. `$GROK_TELEMETRY_ROOT` if set and contains `pyproject.toml`
2. `~/Grok/token-telemetry`
3. `~/Grok/grok-build-telemetry`

If none found, clone:

```bash
mkdir -p "$HOME/Grok"
git clone https://github.com/foudress/grok-build-telemetry.git "$HOME/Grok/token-telemetry"
```

**Windows (PowerShell) equivalent if you are not in bash:**

```powershell
New-Item -ItemType Directory -Force -Path (Join-Path $env:USERPROFILE "Grok") | Out-Null
git clone https://github.com/foudress/grok-build-telemetry.git (Join-Path $env:USERPROFILE "Grok\token-telemetry")
```

Set `ROOT` to that directory and:

```bash
export GROK_TELEMETRY_ROOT="$ROOT"          # bash/zsh
# PowerShell: $env:GROK_TELEMETRY_ROOT = $ROOT
cd "$ROOT"
```

### 3. Bootstrap (venv + package + `/telemetry` skill)

From `$ROOT`, run **one** command (works on all OS):

```bash
"$PY" bootstrap_install.py
```

This creates `.venv` if needed, runs `pip install -e .`, verifies `python -m token_telemetry --help`, and installs the skill via `install_skills.py`.

### 4. Verify

```bash
"$PY" -c "from pathlib import Path; import json; p=Path.home()/'.grok'/'skills'/'telemetry'/'SKILL.md'; print(p, p.is_file()); print((Path.home()/'.grok-telemetry'/'install.json').read_text())"
```

### 5. Tell the user

Reply briefly:

- Installed at `$ROOT`
- Skill: **`/telemetry`** (restart Grok Build once if it does not appear)
- Optional smoke (any OS):  
  `"$PY" "$ROOT/launch_dashboard.py" --detached`

Do **not** chain a long tour of the dashboard UI.

---

## Related

- Daily: **`/telemetry`** → kill port **8765** → clean relaunch → `http://127.0.0.1:8765/`
- Manual: `python launch_dashboard.py` (foreground) or `--detached`
