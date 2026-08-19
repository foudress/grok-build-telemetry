"""On-disk calc cache for closed sessions (attr events + aggregate rows).

Invalidates when ``updates.jsonl`` or ``summary.json`` mtime/size change.
User reset wipes the folder + in-memory maps (callers).
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Optional

CACHE_VER = 1
CACHE_DIR = Path.home() / ".grok" / "token-telemetry" / "calc-cache"

_lock = threading.Lock()


def _stat_pair(path: Path) -> tuple[float, int]:
    try:
        st = path.stat()
        return float(st.st_mtime), int(st.st_size)
    except OSError:
        return 0.0, 0


def fingerprint(session_dir: Path) -> dict[str, Any]:
    um, us = _stat_pair(session_dir / "updates.jsonl")
    sm, ss = _stat_pair(session_dir / "summary.json")
    return {
        "v": CACHE_VER,
        "sid": session_dir.name,
        "u_mtime": um,
        "u_size": us,
        "s_mtime": sm,
        "s_size": ss,
    }


def _path_for(session_dir: Path) -> Path:
    # UUID names are unique; cwd-folder collisions are rare and still
    # guarded by fingerprint mtime/size.
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in session_dir.name)
    return CACHE_DIR / f"{safe}.json"


def _fp_match(blob: dict[str, Any], fp: dict[str, Any]) -> bool:
    return (
        blob.get("v") == CACHE_VER
        and blob.get("sid") == fp.get("sid")
        and blob.get("u_mtime") == fp.get("u_mtime")
        and blob.get("u_size") == fp.get("u_size")
        and blob.get("s_mtime") == fp.get("s_mtime")
        and blob.get("s_size") == fp.get("s_size")
    )


def load_calc(session_dir: Path) -> Optional[dict[str, Any]]:
    p = _path_for(session_dir)
    if not p.is_file():
        return None
    try:
        blob = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(blob, dict):
        return None
    fp = fingerprint(session_dir)
    if not _fp_match(blob, fp):
        return None
    return blob


def save_calc(session_dir: Path, **fields: Any) -> None:
    fp = fingerprint(session_dir)
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    dest = _path_for(session_dir)
    with _lock:
        prev: dict[str, Any] = {}
        if dest.is_file():
            try:
                old = json.loads(dest.read_text(encoding="utf-8"))
                if isinstance(old, dict) and _fp_match(old, fp):
                    prev = old
            except (OSError, json.JSONDecodeError):
                prev = {}
        blob = {**prev, **fp, **fields}
        fd, tmp = tempfile.mkstemp(prefix="ttc-", suffix=".json", dir=str(CACHE_DIR))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(blob, f, ensure_ascii=False, separators=(",", ":"))
            os.replace(tmp, dest)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def reset_all_calcs() -> int:
    """Wipe disk + in-memory period/aggregate caches."""
    from token_telemetry.session.aggregate import clear_file_mem
    from token_telemetry.session.period_attr import clear_attr_mem

    clear_attr_mem()
    clear_file_mem()
    return clear_calc_cache()


def clear_calc_cache() -> int:
    """Delete all on-disk calc files. Returns how many were removed."""
    n = 0
    if not CACHE_DIR.is_dir():
        return 0
    with _lock:
        for p in CACHE_DIR.glob("*.json"):
            try:
                p.unlink()
                n += 1
            except OSError:
                pass
    return n
