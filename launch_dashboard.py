#!/usr/bin/env python3
"""
OS-independent dashboard launcher: kill port → clean start.

  python launch_dashboard.py
  python launch_dashboard.py --detached
  python launch_dashboard.py --port 8765 --session-id <uuid> --no-open
"""

from __future__ import annotations

import argparse
import json
import os
import signal
import socket
import subprocess
import sys
import time
import webbrowser
from pathlib import Path


# A turn still being written counts as active. Older logs do not.
_ACTIVE_S = 10 * 60


def choose_source(*, grok_active: bool, codex_active: bool) -> str:
    """Grok wins when both are in progress. Codex only when it is the one running."""
    if grok_active or not codex_active:
        return "grok"
    return "codex"


def _mtime(path: Path) -> float:
    try:
        return path.stat().st_mtime if path.is_file() else 0.0
    except OSError:
        return 0.0


def _grok_updates(sessions_root: Path, session_id: str) -> float:
    if not session_id or not sessions_root.is_dir():
        return 0.0
    direct = _mtime(sessions_root / session_id / "updates.jsonl")
    if direct:
        return direct
    try:
        children = list(sessions_root.iterdir())
    except OSError:
        return 0.0
    newest = 0.0
    for child in children:
        newest = max(newest, _mtime(child / session_id / "updates.jsonl"))
    return newest


def _codex_newest(codex_home: Path) -> float:
    """Newest rollout under the latest day folder. Enough to see a live turn."""
    day = codex_home / "sessions"
    if not day.is_dir():
        return 0.0
    try:
        for _ in range(3):
            kids = [p for p in day.iterdir() if p.is_dir()]
            if not kids:
                break
            day = max(kids, key=lambda p: p.name)
        newest = 0.0
        for path in day.iterdir():
            name = path.name
            if path.is_file() and (name.endswith(".jsonl") or name.endswith(".jsonl.zst")):
                newest = max(newest, _mtime(path))
        return newest
    except OSError:
        return 0.0


def _read_active_ids(path: Path) -> list[str]:
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    out = []
    for row in data:
        if isinstance(row, dict) and row.get("session_id"):
            out.append(str(row["session_id"]))
        elif isinstance(row, str) and row:
            out.append(row)
    return out


def _newest_grok(sessions_root: Path) -> tuple[float, str]:
    """Latest updates.jsonl at session depth. Returns (mtime, session id)."""
    best = 0.0
    sid = ""
    if not sessions_root.is_dir():
        return best, sid
    try:
        children = list(sessions_root.iterdir())
    except OSError:
        return best, sid
    for child in children:
        if not child.is_dir():
            continue
        direct = child / "updates.jsonl"
        candidates = [direct] if direct.is_file() else []
        if not candidates:
            try:
                subs = list(child.iterdir())
            except OSError:
                continue
            for sub in subs:
                path = sub / "updates.jsonl"
                if path.is_file():
                    candidates.append(path)
        for path in candidates:
            if _is_subagent(path.parent):
                continue
            mt = _mtime(path)
            if mt >= best:
                best = mt
                sid = path.parent.name
    return best, sid


def _is_subagent(session_dir: Path) -> bool:
    summary = session_dir / "summary.json"
    if not summary.is_file():
        return False
    try:
        data = json.loads(summary.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError):
        return False
    kind = data.get("session_kind") if isinstance(data, dict) else None
    return isinstance(kind, str) and kind.strip().lower() in ("subagent", "subagent_resume")


def launch_choice(
    *,
    session_id: str = "",
    active_ids: list[str] | None = None,
    sessions_root: Path | None = None,
    codex_home: Path | None = None,
    active_file: Path | None = None,
    now: float | None = None,
) -> tuple[str, str]:
    """(source, grok session to open).

    Active means a log written in the last 10 minutes. A stale id list is
    not a running session. Both running → grok, and the URL pins that live
    Grok session. Only Codex running → codex.
    """
    now_s = time.time() if now is None else now
    root = sessions_root if sessions_root is not None else Path.home() / ".grok" / "sessions"
    home = codex_home if codex_home is not None else Path.home() / ".codex"
    if active_ids is None:
        listing = active_file if active_file is not None else Path.home() / ".grok" / "active_sessions.json"
        ids = _read_active_ids(listing)
    else:
        ids = list(active_ids)
    grok_mtime, grok_sid = _newest_grok(root)
    if session_id and session_id not in ids:
        ids.append(session_id)
    for sid in ids:
        mt = _grok_updates(root, sid)
        if mt >= grok_mtime:
            grok_mtime, grok_sid = mt, sid
    codex_mtime = _codex_newest(home)
    # An explicit Grok session is the one the user just launched from.
    grok_active = bool(session_id) or (
        grok_mtime > 0 and now_s - grok_mtime <= _ACTIVE_S
    )
    codex_active = codex_mtime > 0 and now_s - codex_mtime <= _ACTIVE_S
    source = choose_source(grok_active=grok_active, codex_active=codex_active)
    if source != "grok":
        return source, ""
    return source, session_id or grok_sid


def launch_source(
    *,
    session_id: str = "",
    active_ids: list[str] | None = None,
    sessions_root: Path | None = None,
    codex_home: Path | None = None,
    active_file: Path | None = None,
    now: float | None = None,
) -> str:
    """Which dashboard to open. Both active → grok. Only Codex running → codex."""
    source, _sid = launch_choice(
        session_id=session_id,
        active_ids=active_ids,
        sessions_root=sessions_root,
        codex_home=codex_home,
        active_file=active_file,
        now=now,
    )
    return source


def dashboard_url(port: int, source: str, session_id: str = "") -> str:
    base = f"http://127.0.0.1:{port}/"
    if source == "codex":
        return base + "?source=codex"
    if session_id:
        return base + "?source=grok&session=" + session_id
    return base + "?source=grok"


def resolve_root() -> Path:
    env = os.environ.get("GROK_TELEMETRY_ROOT")
    if env:
        p = Path(env).expanduser()
        if (p / "pyproject.toml").is_file():
            return p.resolve()

    marker = Path.home() / ".grok-telemetry" / "install.json"
    if marker.is_file():
        try:
            data = json.loads(marker.read_text(encoding="utf-8"))
            root = Path(str(data.get("root") or "")).expanduser()
            if (root / "pyproject.toml").is_file():
                return root.resolve()
        except (OSError, json.JSONDecodeError, TypeError):
            pass

    here = Path(__file__).resolve().parent
    if (here / "pyproject.toml").is_file():
        return here

    for rel in ("Grok/token-telemetry", "Grok/grok-build-telemetry"):
        c = Path.home() / rel
        if (c / "pyproject.toml").is_file():
            return c.resolve()

    raise SystemExit(
        "Token Telemetry root not found. Set GROK_TELEMETRY_ROOT or run docs/install.md."
    )


def _venv_python_path(root: Path) -> Path:
    if sys.platform == "win32":
        return root / ".venv" / "Scripts" / "python.exe"
    return root / ".venv" / "bin" / "python"


def _is_store_stub(py: Path) -> bool:
    s = str(py).replace("/", "\\").lower()
    return "windowsapps" in s or "pythonsoftwarefoundation" in s


def _python_ok(py: Path) -> bool:
    """True if py is a real 3.10+ interpreter (not the Windows Store stub)."""
    if not py.is_file() and not py.exists():
        return False
    if _is_store_stub(py):
        return False
    try:
        out = subprocess.check_output(
            [str(py), "-c", "import sys; print(f'{sys.version_info[0]}.{sys.version_info[1]}')"],
            text=True,
            errors="replace",
            timeout=20,
        ).strip()
        major, minor = (int(x) for x in out.split(".", 1))
        return (major, minor) >= (3, 10)
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
        return False


def _has_transformers(py: Path) -> bool:
    try:
        subprocess.run(
            [str(py), "-c", "import transformers"],
            check=True,
            capture_output=True,
            timeout=60,
        )
        return True
    except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
        return False


def _discover_base_python() -> Path:
    """Pick a system Python to create/use when .venv is missing."""
    candidates: list[Path] = []

    if sys.platform == "win32":
        try:
            out = subprocess.check_output(
                ["py", "-0p"], text=True, errors="replace", timeout=15
            )
            for line in out.splitlines():
                # -V:3.12 *  C:\...\python.exe
                parts = line.strip().split()
                if not parts:
                    continue
                path = Path(parts[-1])
                if path.suffix.lower() == ".exe":
                    candidates.append(path)
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass
        local = Path.home() / "AppData" / "Local" / "Programs" / "Python"
        if local.is_dir():
            for child in sorted(local.glob("Python3*/python.exe"), reverse=True):
                candidates.append(child)

    # Current interpreter last (may be the Store stub — filtered below)
    candidates.append(Path(sys.executable))

    # Prefer higher versions; unique preserve order after sort by version probe
    seen: set[str] = set()
    usable: list[tuple[tuple[int, int], Path]] = []
    for py in candidates:
        key = str(py.resolve()) if py.exists() else str(py)
        if key in seen:
            continue
        seen.add(key)
        if not _python_ok(py):
            continue
        try:
            out = subprocess.check_output(
                [
                    str(py),
                    "-c",
                    "import sys; print(sys.version_info[0] * 100 + sys.version_info[1])",
                ],
                text=True,
                timeout=20,
            ).strip()
            usable.append((-int(out), py))  # higher version first after sort
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired, ValueError):
            continue

    if not usable:
        raise SystemExit(
            "No Python 3.10+ found (Windows Store stub alone is not enough).\n"
            "Install Python 3.10+ from https://www.python.org/downloads/ then re-run."
        )
    usable.sort()
    return usable[0][1]


def ensure_runtime_python(root: Path) -> Path:
    """
    Prefer repo .venv with transformers. Create/repair it if needed so
    `/telemetry` does not pick the WindowsApps Python 3.9 stub.
    """
    vpy = _venv_python_path(root)
    if vpy.is_file() and _python_ok(vpy):
        if _has_transformers(vpy):
            return vpy
        print(f"repairing venv deps: {vpy}")
        subprocess.run([str(vpy), "-m", "pip", "install", "-U", "pip"], cwd=str(root), check=False)
        subprocess.run([str(vpy), "-m", "pip", "install", "-e", "."], cwd=str(root), check=True)
        return vpy

    base = _discover_base_python()
    # If no venv yet but base already has the package + transformers, use it
    if not vpy.is_file() and _has_transformers(base):
        try:
            subprocess.run(
                [str(base), "-c", "import token_telemetry"],
                check=True,
                capture_output=True,
                timeout=30,
                cwd=str(root),
            )
            return base
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired):
            pass

    print(f"creating .venv with {base}")
    subprocess.run([str(base), "-m", "venv", ".venv"], cwd=str(root), check=True)
    if not vpy.is_file():
        raise SystemExit(f"venv python missing after create: {vpy}")
    subprocess.run([str(vpy), "-m", "pip", "install", "-U", "pip"], cwd=str(root), check=True)
    subprocess.run([str(vpy), "-m", "pip", "install", "-e", "."], cwd=str(root), check=True)
    return vpy


def venv_python(root: Path) -> Path:
    return ensure_runtime_python(root)


def pids_on_port(port: int) -> list[int]:
    pids: set[int] = set()
    if sys.platform == "win32":
        try:
            out = subprocess.check_output(
                ["netstat", "-ano", "-p", "tcp"], text=True, errors="replace"
            )
        except (OSError, subprocess.CalledProcessError):
            return []
        for line in out.splitlines():
            if "LISTENING" not in line:
                continue
            parts = line.split()
            if len(parts) < 5 or not parts[0].upper().startswith("TCP"):
                continue
            local = parts[1]
            # 127.0.0.1:8765 / 0.0.0.0:8765 / [::]:8765
            if not (local.endswith(f":{port}") or local.endswith(f"]:{port}")):
                continue
            try:
                pid = int(parts[-1])
            except ValueError:
                continue
            if pid > 0:
                pids.add(pid)
    else:
        for cmd in (
            ["lsof", f"-tiTCP:{port}", "-sTCP:LISTEN"],
            ["lsof", f"-ti:{port}"],
        ):
            try:
                out = subprocess.check_output(cmd, text=True, errors="replace")
            except (OSError, subprocess.CalledProcessError):
                continue
            for part in out.split():
                try:
                    pid = int(part)
                except ValueError:
                    continue
                if pid > 0:
                    pids.add(pid)
            if pids:
                break
        if not pids:
            try:
                out = subprocess.check_output(
                    ["fuser", f"{port}/tcp"],
                    text=True,
                    errors="replace",
                    stderr=subprocess.DEVNULL,
                )
                for part in out.replace(":", " ").split():
                    try:
                        pid = int(part)
                    except ValueError:
                        continue
                    if pid > 0:
                        pids.add(pid)
            except (OSError, subprocess.CalledProcessError):
                pass
    return sorted(pids)


def kill_port(port: int) -> None:
    for pid in pids_on_port(port):
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/PID", str(pid), "/F"],
                    check=False,
                    capture_output=True,
                )
            else:
                os.kill(pid, signal.SIGTERM)
            print(f"killed PID {pid} (port {port})")
        except (OSError, ProcessLookupError) as e:
            print(f"warning: could not kill PID {pid}: {e}", file=sys.stderr)
    if pids_on_port(port):
        time.sleep(0.4)
        for pid in pids_on_port(port):
            try:
                if sys.platform == "win32":
                    subprocess.run(
                        ["taskkill", "/PID", str(pid), "/F", "/T"],
                        check=False,
                        capture_output=True,
                    )
                else:
                    os.kill(pid, signal.SIGKILL)
            except (OSError, ProcessLookupError):
                pass
        time.sleep(0.3)


def wait_url(url: str, seconds: float = 20.0) -> bool:
    deadline = time.time() + seconds
    # Prefer stdlib only
    from urllib.error import URLError
    from urllib.request import urlopen

    while time.time() < deadline:
        try:
            with urlopen(url, timeout=2) as r:  # noqa: S310 — local dashboard
                if 200 <= getattr(r, "status", 200) < 500:
                    return True
        except (URLError, OSError, TimeoutError):
            pass
        time.sleep(0.3)
    return False


def port_free_check(port: int) -> None:
    # Best-effort bind probe (does not replace kill)
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", port))
    except OSError:
        pass
    finally:
        s.close()


def _write_windows_launch_vbs(
    cmd: list[str], root: Path, out_log: Path, err_log: Path, vbs_path: Path
) -> None:
    """Hidden WScript launcher; opened via explorer.exe to escape Job Objects."""
    # cmd /c cd /d ROOT && <cmd> >> out 2>> err
    inner = " ".join(
        [
            "cd /d",
            subprocess.list2cmdline([str(root)]),
            "&&",
            subprocess.list2cmdline(cmd),
            ">>",
            subprocess.list2cmdline([str(out_log)]),
            "2>>",
            subprocess.list2cmdline([str(err_log)]),
        ]
    )
    run = "cmd /c " + inner.replace('"', '""')
    vbs_path.write_text(
        'Set sh = CreateObject("WScript.Shell")\r\n'
        f'sh.Run "{run}", 0, False\r\n',
        encoding="ascii",
        errors="replace",
    )


def _spawn_detached(cmd: list[str], root: Path, out_log: Path, err_log: Path) -> int:
    """Start dashboard so it survives after this launcher exits.

    Returns a best-effort PID (0 if not yet known). On Windows, Grok Build runs
    the skill shell inside a Job Object with KILL_ON_JOB_CLOSE. Direct CreateProcess
    / ``start /B`` / Start-Process stay in that job and die when ``/telemetry``
    returns — even with CREATE_BREAKAWAY_FROM_JOB on some hosts. Launching a
    tiny VBS through ``explorer.exe`` creates the process outside the job.
    """
    if sys.platform == "win32":
        vbs = out_log.parent / "_dashboard_launch.vbs"
        _write_windows_launch_vbs(cmd, root, out_log, err_log, vbs)
        # explorer.exe is outside the agent job; its child inherits that.
        subprocess.Popen(
            ["explorer.exe", str(vbs)],
            close_fds=True,
            creationflags=0x08000000,  # CREATE_NO_WINDOW (explorer itself)
        )
        return 0

    proc = subprocess.Popen(
        cmd,
        cwd=str(root),
        stdin=subprocess.DEVNULL,
        stdout=open(out_log, "a", encoding="utf-8"),  # noqa: SIM115 — kept for child life
        stderr=open(err_log, "a", encoding="utf-8"),  # noqa: SIM115
        start_new_session=True,
        close_fds=True,
    )
    return int(proc.pid)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Launch Grok Token Telemetry dashboard")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--session-id", default="")
    ap.add_argument("--detached", action="store_true")
    ap.add_argument("--no-open", action="store_true")
    ap.add_argument("--no-kill", action="store_true")
    args = ap.parse_args(argv)

    root = resolve_root()
    py = venv_python(root)
    # Prefer CLI flag; fall back to the Grok Build session that ran /telemetry.
    session_id = (args.session_id or "").strip() or (
        os.environ.get("GROK_SESSION_ID") or ""
    ).strip()
    source, live_sid = launch_choice(session_id=session_id)
    open_sid = session_id or live_sid
    url = dashboard_url(args.port, source, open_sid)
    health_url = f"http://127.0.0.1:{args.port}/api/health"

    if not args.no_kill:
        kill_port(args.port)
        port_free_check(args.port)

    cmd = [str(py), "-u", "-m", "token_telemetry", "--port", str(args.port)]
    if source == "grok" and open_sid:
        cmd += ["--session-id", open_sid]
    # Always let this launcher own browser open when detached; avoid double-open
    if args.detached or args.no_open:
        cmd.append("--no-open")

    print(f"Starting Grok Token Telemetry on {url}")
    print(f"  root:   {root}")
    print(f"  python: {py}")
    if session_id:
        print(f"  session: {session_id} (pinned)")

    # Preflight: Grok-2 weights need transformers (else bytes4 fallback)
    try:
        subprocess.run(
            [str(py), "-c", "import transformers"],
            check=True,
            capture_output=True,
            cwd=str(root),
        )
    except subprocess.CalledProcessError:
        print(
            "warning: 'transformers' missing in this Python — tokenizer will be bytes4.\n"
            f"  fix: \"{py}\" -m pip install -e \"{root}\"",
            file=sys.stderr,
        )

    if not args.detached:
        return subprocess.call(cmd, cwd=str(root))

    log_dir = Path.home() / ".grok-telemetry"
    log_dir.mkdir(parents=True, exist_ok=True)
    out_log = log_dir / "dashboard.out.log"
    err_log = log_dir / "dashboard.err.log"

    spawned_pid = _spawn_detached(cmd, root, out_log, err_log)

    # Prefer /api/health (cheap). First transformers import / bind can be slow.
    ok = wait_url(health_url, seconds=45.0) or wait_url(url, seconds=10.0)
    if not ok:
        print(
            f"warning: dashboard did not respond on {url} — see {err_log}",
            file=sys.stderr,
        )
        return 1
    listen_pids = pids_on_port(args.port)
    pid = listen_pids[0] if listen_pids else spawned_pid
    if not args.no_open:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    print(f"dashboard up  pid={pid}  {url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
