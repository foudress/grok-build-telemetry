"""On-disk calc cache for closed sessions (attr events + aggregate rows).

Invalidates when ``updates.jsonl`` / ``summary.json`` change **or** accounting
source files change (else old sessions stay frozen after a code edit).
``code_sig`` re-stats sources each call so a running dashboard notices edits.
Boot ``ensure_cache_fresh`` drops blobs from a previous CACHE_VER / code stamp.
User reset wipes the folder + in-memory maps; the live tree is rebuilt by
``SessionMonitor.rebuild_current``. D/W/M must also refetch (client dirty +
``cache_gen`` on /api/health).
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
from pathlib import Path
from typing import Any, Optional

CACHE_VER = 30
CACHE_DIR = Path.home() / ".grok" / "token-telemetry" / "calc-cache"
STAMP_NAME = "_stamp.json"

_lock = threading.Lock()
_PKG = Path(__file__).resolve().parent.parent
_CODE_FILES = (
    "util.py",
    "compact_glue.py",
    "session/calc_cache.py",
    "session/kind.py",
    "session/ids.py",
    "hierarchy/builder.py",
    "hierarchy/bootstrap.py",
    "hierarchy/tools_meta.py",
    "hierarchy/gen_rate.py",
    "hierarchy/finalize.py",
    "hierarchy/tooldef_r1.py",
    "hierarchy/step_tree.py",
    "hierarchy/thoughts_enc.py",
    "hierarchy/breakdown.py",
    "hierarchy/attach_price.py",
    "hierarchy/cache_miss.py",
    "hierarchy/user_continuity.py",
    "hierarchy/reread.py",
    "hierarchy/recap_compact.py",
    "pricing/reconstruct/__init__.py",
    "pricing/reconstruct/orchestrate.py",
    "pricing/reconstruct/in_cache.py",
    "pricing/reconstruct/output_enc.py",
    "pricing/reconstruct/segments.py",
    "pricing/reconstruct/weights.py",
    "session/period_attr.py",
    "session/aggregate.py",
    "session/windows.py",
    "session/scan.py",
    "session/peel_period.py",
    "session/monitor.py",
    "session/enrich.py",
    "session/wire.py",
    "session/children.py",
    "session/subagents.py",
    "session/usage.py",
    "session/cards.py",
)
_code_sig: Optional[str] = None
_code_probe: Optional[tuple[tuple[int, int], ...]] = None
_cache_gen: int = 0


def cache_gen() -> int:
    """Monotonic generation; bumps whenever on-disk calcs are wiped."""
    return _cache_gen


def cache_meta() -> dict[str, Any]:
    return {"cache_ver": CACHE_VER, "cache_gen": _cache_gen}


def code_sig() -> str:
    """mtime/size stamp of accounting sources (re-stats; join cached if same)."""
    global _code_sig, _code_probe
    probe = tuple(_stat_pair(_PKG / rel) for rel in _CODE_FILES)
    if _code_sig is not None and _code_probe == probe:
        return _code_sig
    _code_probe = probe
    _code_sig = "|".join(
        f"{rel}:{m}:{s}" for rel, (m, s) in zip(_CODE_FILES, probe)
    )
    return _code_sig


def _stat_pair(path: Path) -> tuple[int, int]:
    try:
        st = path.stat()
        # Milliseconds — JSON-stable (raw float mtimes often fail round-trip).
        return int(st.st_mtime * 1000), int(st.st_size)
    except OSError:
        return 0, 0


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
        "code": code_sig(),
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
        and blob.get("code") == fp.get("code")
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
    global _code_sig, _code_probe
    from token_telemetry.session.aggregate import clear_file_mem
    from token_telemetry.session.period_attr import clear_attr_mem

    _code_sig = None
    _code_probe = None
    clear_attr_mem()
    clear_file_mem()
    n = clear_calc_cache()
    _write_stamp()
    return n


def clear_calc_cache() -> int:
    """Delete session calc files (keep ``_stamp.json``). Returns how many were removed."""
    global _cache_gen
    n = 0
    if not CACHE_DIR.is_dir():
        _cache_gen += 1
        return 0
    with _lock:
        for p in CACHE_DIR.glob("*.json"):
            if p.name.startswith("_"):
                continue
            try:
                p.unlink()
                n += 1
            except OSError:
                pass
        _cache_gen += 1
    return n


def _stamp_path() -> Path:
    return CACHE_DIR / STAMP_NAME


def _write_stamp() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    blob = {"v": CACHE_VER, "code": code_sig(), "gen": _cache_gen}
    dest = _stamp_path()
    fd, tmp = tempfile.mkstemp(prefix="tts-", suffix=".json", dir=str(CACHE_DIR))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(blob, f, ensure_ascii=False, separators=(",", ":"))
        os.replace(tmp, dest)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _read_stamp() -> Optional[dict[str, Any]]:
    p = _stamp_path()
    if not p.is_file():
        return None
    try:
        blob = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return blob if isinstance(blob, dict) else None


def _stamp_gen(blob: Optional[dict[str, Any]]) -> int:
    if not blob:
        return 0
    try:
        return int(blob.get("gen") or 0)
    except (TypeError, ValueError):
        return 0


def ensure_cache_fresh() -> int:
    """Drop disk calcs when CACHE_VER or accounting sources changed since last boot."""
    global _cache_gen
    cur_code = code_sig()
    prev = _read_stamp()
    prev_gen = _stamp_gen(prev)
    if (
        prev is not None
        and prev.get("v") == CACHE_VER
        and prev.get("code") == cur_code
    ):
        _cache_gen = prev_gen
        return 0
    # Restore persisted gen so the next bump is visible to a still-open UI.
    _cache_gen = prev_gen
    n = clear_calc_cache()
    _write_stamp()
    return n
