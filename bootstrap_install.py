#!/usr/bin/env python3
"""
OS-independent post-clone install: venv + pip install -e . + /telemetry skill.

Run from repo root (or pass root as argv[1]):
  python bootstrap_install.py
  python3 bootstrap_install.py
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def _venv_python(root: Path) -> Path:
    if sys.platform == "win32":
        return root / ".venv" / "Scripts" / "python.exe"
    return root / ".venv" / "bin" / "python"


def _is_store_stub(py: Path) -> bool:
    s = str(py).replace("/", "\\").lower()
    return "windowsapps" in s or "pythonsoftwarefoundation" in s


def _discover_base_python() -> Path:
    """Avoid Windows Store stub; prefer py launcher / real installs."""
    if not _is_store_stub(Path(sys.executable)):
        try:
            out = subprocess.check_output(
                [
                    sys.executable,
                    "-c",
                    "import sys; raise SystemExit(0 if sys.version_info >= (3, 10) else 1)",
                ],
                timeout=20,
            )
            return Path(sys.executable)
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass

    if sys.platform == "win32":
        for args in (["py", "-3.12"], ["py", "-3.11"], ["py", "-3.10"], ["py", "-3"]):
            try:
                out = subprocess.check_output(
                    args + ["-c", "import sys; print(sys.executable)"],
                    text=True,
                    timeout=20,
                ).strip()
                p = Path(out)
                if p.is_file() and not _is_store_stub(p):
                    return p
            except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
                continue
        local = Path.home() / "AppData" / "Local" / "Programs" / "Python"
        for child in sorted(local.glob("Python3*/python.exe"), reverse=True):
            if not _is_store_stub(child):
                return child

    raise SystemExit(
        "No Python 3.10+ found (Windows Store Python stub is not usable).\n"
        "Install from https://www.python.org/downloads/ then re-run."
    )


def _run(cmd: list[str], *, cwd: Path) -> None:
    print("+", " ".join(cmd))
    subprocess.run(cmd, cwd=str(cwd), check=True)


def main() -> int:
    root = Path(sys.argv[1]).expanduser().resolve() if len(sys.argv) > 1 else Path(__file__).resolve().parent
    if not (root / "pyproject.toml").is_file():
        print(f"error: pyproject.toml not found under {root}", file=sys.stderr)
        return 1

    base = _discover_base_python()
    print(f"base python: {base}")
    vpy = _venv_python(root)
    if not vpy.is_file():
        _run([str(base), "-m", "venv", ".venv"], cwd=root)
    if not vpy.is_file():
        print(f"error: venv python missing: {vpy}", file=sys.stderr)
        return 1

    _run([str(vpy), "-m", "pip", "install", "-U", "pip"], cwd=root)
    _run([str(vpy), "-m", "pip", "install", "-e", "."], cwd=root)
    _run([str(vpy), "-m", "token_telemetry", "--help"], cwd=root)

    os.environ["GROK_TELEMETRY_ROOT"] = str(root)
    skills = root / "install_skills.py"
    _run([str(vpy), str(skills), str(root)], cwd=root)
    print("bootstrap_install: ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
