#!/usr/bin/env python3
"""Copy /telemetry skill into ~/.grok/skills and write ~/.grok-telemetry/install.json."""

from __future__ import annotations

import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path


def main() -> int:
    repo = Path(os.environ.get("GROK_TELEMETRY_ROOT") or Path(__file__).resolve().parent)
    if len(sys.argv) > 1 and sys.argv[1].strip():
        repo = Path(sys.argv[1]).expanduser().resolve()
    if not (repo / "pyproject.toml").is_file():
        print(f"error: not a token-telemetry root: {repo}", file=sys.stderr)
        return 1

    src_skills = repo / "skills"
    if not src_skills.is_dir():
        print(f"error: skills folder missing: {src_skills}", file=sys.stderr)
        return 1

    dest_root = Path.home() / ".grok" / "skills"
    dest_root.mkdir(parents=True, exist_ok=True)

    copied: list[str] = []
    for name in ("telemetry",):
        src = src_skills / name
        if not src.is_dir():
            print(f"warning: skill missing: {src}", file=sys.stderr)
            continue
        dest = dest_root / name
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(src, dest)
        copied.append(name)
        print(f"Installed skill: {name} -> {dest}")

    if not copied:
        print("error: no telemetry skills copied", file=sys.stderr)
        return 1

    marker_dir = Path.home() / ".grok-telemetry"
    marker_dir.mkdir(parents=True, exist_ok=True)
    marker = {
        "root": str(repo),
        "installed_at": datetime.now(timezone.utc).isoformat(),
        "skills": copied,
    }
    (marker_dir / "install.json").write_text(
        json.dumps(marker, indent=2) + "\n", encoding="utf-8"
    )
    print(f"GROK_TELEMETRY_ROOT={repo}")
    print("Restart Grok Build or reload skills if /telemetry does not appear.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
