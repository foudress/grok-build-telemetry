# Security

token-telemetry is a **local** companion. It reads Grok Build session files under `~/.grok/sessions/` and serves a dashboard bound to `127.0.0.1` by default.

## Hardening notes

- Prefer `--host 127.0.0.1` (default). Binding `0.0.0.0` exposes session metadata on your LAN.
- Do not commit real `updates.jsonl` / chat transcripts with secrets into the repo or fixtures.
- Session JSON may contain prompts, tool args, and paths from your machine — treat `out/` and fixtures as sensitive.
