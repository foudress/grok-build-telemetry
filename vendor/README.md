# Vendor assets

## `grok-2-tokenizer/`

Offline copy of a Grok-2-compatible Hugging Face tokenizer used for **pro-rata weights** (thought/message/tool splits). Billing totals still come from Grok’s official `turn_completed.usage`.

- Load path is local (`local_files_only`) so the dashboard works without network.
- If load fails, the app falls back to a bytes÷4 estimate.
- When redistributing this folder, follow the upstream tokenizer’s license and attribution requirements.
