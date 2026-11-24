"""Read Antigravity conversation databases into a Codex-shaped snapshot.

Each ``conversations/<id>.db`` is one cascade. Token counts live on
``gen_metadata.data`` (one row per model call). The steps that call lists
are the planner reply and the tool results. A user-input step starts a
round, the same way a Codex turn does. Later calls in that round are
harness re-entry.

Cache read is separate from uncached input. Thinking is inside output.
A repeated response id is the same call.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from bisect import bisect_left, bisect_right
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import unquote, urlparse

from token_telemetry.antigravity.prices import priced_model
from token_telemetry.antigravity.proto import (
    parse_aux,
    parse_generation,
    parse_planner,
    parse_step,
    parse_user_text,
)
from token_telemetry.tokenizer import count_tokens

_USER = 14
_PLANNER = 15
_CHECKPOINT = 23
_AUX = 101

_METRIC = (
    "One gen_metadata row is one model call. "
    "Calls after the same user input share one round. "
    "input_tokens is uncached input plus cache read. "
    "cached_input_tokens is cache read and is not inside the uncached count. "
    "reasoning_output_tokens is included in output_tokens. "
    "Tool steps are harness results of the call that requested them. "
    "No dollar price is stored."
)

_lock = threading.Lock()
_cache: dict[str, tuple[str, dict[str, Any]]] = {}
_file_cache: dict[Path, tuple[tuple, dict[str, Any]]] = {}
_focus_cache: dict[Path, tuple[tuple[str, Optional[str]], dict[str, Any]]] = {}
_summary_cache: dict[Path, tuple[tuple, dict[str, dict[str, Any]]]] = {}


def antigravity_home() -> Path:
    raw = (os.environ.get("ANTIGRAVITY_HOME") or "").strip()
    if raw:
        return Path(raw)
    return Path.home() / ".gemini" / "antigravity"


def clear_cache() -> None:
    with _lock:
        _cache.clear()
        _file_cache.clear()
        _focus_cache.clear()
        _summary_cache.clear()


def _connect(path: Path) -> sqlite3.Connection:
    uri = path.resolve().as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True, timeout=2.0)
    con.execute("PRAGMA query_only = ON")
    return con


def _iso(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def _cwd(raw: str) -> str:
    text = (raw or "").strip()
    if not text:
        return ""
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        data = text
    item = data[0] if isinstance(data, list) and data else data
    if not isinstance(item, str):
        return ""
    if item.startswith("file:"):
        path = unquote(urlparse(item).path)
        if len(path) >= 3 and path[0] == "/" and path[2] == ":":
            path = path[1:]
        return path
    return item


def _summaries(root: Path) -> dict[str, dict[str, Any]]:
    path = root / "conversation_summaries.db"
    stamp = _db_stamp(path)
    if not stamp:
        return {}
    cached = _summary_cache.get(path)
    if cached and cached[0] == stamp:
        return cached[1]
    try:
        con = _connect(path)
    except sqlite3.Error:
        return {}
    try:
        rows = con.execute(
            "SELECT conversation_id, title, last_modified_time, workspace_uris, "
            "parent_conversation_id, agent_name FROM conversation_summaries"
        ).fetchall()
    except sqlite3.Error:
        return {}
    finally:
        con.close()
    out: dict[str, dict[str, Any]] = {}
    for cid, title, modified, workspaces, parent, agent in rows:
        if not isinstance(cid, str) or not cid:
            continue
        out[cid] = {
            "title": title if isinstance(title, str) else "",
            "modified": modified if isinstance(modified, str) else "",
            "cwd": _cwd(workspaces if isinstance(workspaces, str) else ""),
            "parent_id": parent if isinstance(parent, str) else "",
            "agent": agent if isinstance(agent, str) else "",
        }
    _summary_cache[path] = (stamp, out)
    return out


def _conversation_paths(root: Path) -> list[Path]:
    folder = root / "conversations"
    if not folder.is_dir():
        return []
    try:
        paths = [
            p for p in folder.iterdir()
            if p.is_file() and p.suffix == ".db" and p.stat().st_size > 0
        ]
    except OSError:
        return []
    paths.sort(key=lambda p: p.name)
    return paths


def _count(usage: dict[str, Any], key: str) -> int:
    val = usage.get(key)
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        return 0
    return int(val)


def _arg_label(raw: bytes) -> tuple[str, str, int]:
    """Short command and path from a tool argument blob. No file bodies."""
    if not raw:
        return "", "", 0
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return "", "", len(raw)
    obj: Any = text
    if text[:1] in "{[":
        try:
            obj = json.loads(text)
        except json.JSONDecodeError:
            obj = text
    command = ""
    path = ""
    if isinstance(obj, dict):
        for key in ("command", "cmd", "command_line", "commandline", "input"):
            val = obj.get(key)
            if isinstance(val, str) and val.strip():
                command = " ".join(val.split())[:200]
                break
        for key in ("path", "file", "file_path", "target_file", "cwd"):
            val = obj.get(key)
            if isinstance(val, str) and val.strip():
                path = val.strip()[:200]
                break
        if not command and not path:
            for key in ("query", "url", "pattern"):
                val = obj.get(key)
                if isinstance(val, str) and val.strip():
                    command = " ".join(val.split())[:200]
                    break
    elif isinstance(obj, str) and obj.strip():
        command = " ".join(obj.split())[:200]
    return command, path, len(text)


def _tool_row(tool: dict[str, Any], *, kind: str) -> dict[str, Any]:
    command, path, arg_chars = _arg_label(tool.get("args") if isinstance(tool.get("args"), (bytes, bytearray)) else b"")
    result = tool.get("result_chars")
    result_chars = int(result) if isinstance(result, int) and not isinstance(result, bool) else 0
    preview = command or path
    return {
        "kind": kind,
        "name": str(tool.get("name") or "tool"),
        "call_id": str(tool.get("call_id") or ""),
        "preview": preview,
        "command": command,
        "path": path,
        "chars": result_chars if kind == "tool" and result_chars else max(arg_chars, 1),
    }


def _call_from_usage(
    usage: dict[str, Any],
    *,
    model: str,
    epoch: Optional[float],
    turn_id: str,
    detail: list[dict[str, Any]],
    harness: list[dict[str, Any]],
    user_preview: str,
    gen_idx: int,
    compact_after: bool,
) -> dict[str, Any]:
    uncached = _count(usage, "uncached")
    cached = _count(usage, "cached")
    write = _count(usage, "cache_write")
    output = _count(usage, "output")
    reason = _count(usage, "reasoning")
    prompt = uncached + cached
    response = str(usage.get("response_id") or "")
    call: dict[str, Any] = {
        "t": _iso(epoch) if epoch is not None else "",
        "model": model,
        "effort": "",
        "turn_id": turn_id,
        "response_id": response,
        "input_tokens": prompt,
        "cached_input_tokens": cached,
        "uncached_input_tokens": uncached,
        "cache_write_input_tokens": write,
        "output_tokens": output,
        "reasoning_output_tokens": reason,
        "total_tokens": prompt + write + output,
        "detail": detail,
        "harness": harness,
        "user_preview": user_preview,
        "_idx": gen_idx,
        "_epoch": epoch if epoch is not None else 0.0,
    }
    if compact_after:
        call["compact_after"] = True
    return call


def _parent_id(raw: str, cid: str, known: set[str]) -> str:
    parent = raw.strip()
    if not parent or parent == cid or parent not in known:
        return ""
    return parent


def _load_steps(con: sqlite3.Connection) -> dict[int, dict[str, Any]]:
    rows = con.execute(
        "SELECT idx, step_type, metadata, step_payload FROM steps ORDER BY idx"
    )
    out: dict[int, dict[str, Any]] = {}
    for idx, step_type, meta, payload in rows:
        meta_b = bytes(meta) if isinstance(meta, (bytes, bytearray)) else b""
        payload_b = bytes(payload) if isinstance(payload, (bytes, bytearray)) else b""
        parsed = parse_step(meta_b) if meta_b else {"epoch": None, "tool": None}
        kind = int(step_type) if isinstance(step_type, int) and not isinstance(step_type, bool) else 0
        out[int(idx)] = {
            "type": kind,
            "epoch": parsed.get("epoch"),
            "tool": parsed.get("tool"),
            "payload": payload_b,
        }
    return out


def _turn_id(user_steps: list[int], planner: Optional[int]) -> str:
    pos = bisect_left(user_steps, planner) if planner is not None else 0
    return str(user_steps[pos - 1]) if pos else "start"


def _detail_and_harness(
    step_ids: list[int],
    steps: dict[int, dict[str, Any]],
    orphans: list[int],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], str, bool]:
    """LLM rows, harness rows, user-visible reply, and a mid-round checkpoint."""
    planner = None
    for step_id in step_ids:
        row = steps.get(step_id)
        if row and row["type"] == _PLANNER:
            planner = row
            break
    view = parse_planner(planner["payload"]) if planner else {
        "response": "",
        "response_chars": 0,
        "thinking": "",
        "thinking_chars": 0,
        "tools": [],
    }
    detail: list[dict[str, Any]] = []
    thinking = str(view.get("thinking") or "")
    if thinking:
        detail.append({
            "kind": "thought",
            "preview": thinking,
            "chars": int(view.get("thinking_chars") or 0),
        })
    response = str(view.get("response") or "")
    response_chars = int(view.get("response_chars") or 0)
    message_preview = response
    message_chars = response_chars
    harness: list[dict[str, Any]] = []
    compact = False
    tools = view.get("tools") if isinstance(view.get("tools"), list) else []
    for tool in tools:
        if isinstance(tool, dict) and tool.get("name"):
            detail.append(_tool_row(tool, kind="tool_request"))
    seen_results: set[int] = set()
    for step_id in step_ids:
        row = steps.get(step_id)
        if row is None or row["type"] in (_PLANNER, _USER):
            continue
        seen_results.add(step_id)
        if row["type"] == _CHECKPOINT:
            compact = True
            continue
        tool = row.get("tool")
        if isinstance(tool, dict):
            harness.append(_tool_row(tool, kind="tool"))
    for step_id in orphans:
        if step_id in seen_results:
            continue
        row = steps.get(step_id)
        if row is None or row["type"] in (_PLANNER, _USER):
            continue
        if row["type"] == _CHECKPOINT:
            compact = True
            continue
        if row["type"] == _AUX:
            aux = parse_aux(row["payload"])
            kind = str(aux.get("kind") or "step")
            preview = str(aux.get("preview") or "")
            chars = int(aux.get("chars") or 0)
            if kind == "agent_message" and preview and not message_preview:
                message_preview = preview
                message_chars = chars
                continue
            harness.append({
                "kind": "tool",
                "name": kind,
                "call_id": "",
                "preview": preview,
                "command": "",
                "path": "",
                "chars": chars or 1,
            })
            continue
        tool = row.get("tool")
        if isinstance(tool, dict):
            harness.append(_tool_row(tool, kind="tool"))
    if message_preview:
        detail.append({
            "kind": "message",
            "preview": message_preview,
            "chars": message_chars or 1,
        })
    elif not tools:
        detail.append({"kind": "message", "chars": 1})
    return detail, harness, message_preview, compact


def _parse_db(
    path: Path,
    summary: Optional[dict[str, Any]],
    known: set[str],
) -> dict[str, Any]:
    cid = path.stem
    info = summary or {}
    parent = _parent_id(str(info.get("parent_id") or ""), cid, known)
    title = str(info.get("title") or "").strip() or str(info.get("agent") or "").strip()
    if not title:
        title = "subagent" if parent else cid[:8]
    try:
        st = path.stat()
        mtime = st.st_mtime
    except OSError:
        mtime = 0.0
    calls: list[dict[str, Any]] = []
    try:
        con = _connect(path)
    except sqlite3.Error as exc:
        return _thread(cid, title, info, parent, mtime, calls, error=type(exc).__name__)
    try:
        con.execute("BEGIN")
        gens = list(con.execute("SELECT idx, data FROM gen_metadata ORDER BY idx"))
        steps = _load_steps(con)
    except sqlite3.Error as exc:
        return _thread(cid, title, info, parent, mtime, calls, error=type(exc).__name__)
    finally:
        con.close()

    parsed: list[dict[str, Any]] = []
    covered: set[int] = set()
    for idx, data in gens:
        if not isinstance(data, (bytes, bytearray)) or not data:
            continue
        gen = parse_generation(bytes(data))
        if gen is None or not isinstance(gen.get("usage"), dict):
            continue
        indices = [int(n) for n in gen.get("steps") or [] if isinstance(n, int)]
        planner = next((n for n in indices if steps.get(n, {}).get("type") == _PLANNER), None)
        if planner is None and indices:
            planner = indices[0]
        parsed.append({
            "idx": int(idx),
            "gen": gen,
            "indices": indices,
            "planner": planner,
        })
        covered.update(indices)
    parsed.sort(key=lambda row: (
        row["planner"] if isinstance(row["planner"], int) else float("inf"),
        row["idx"],
    ))
    user_steps = sorted(idx for idx, row in steps.items() if row["type"] == _USER)
    bounds = [row["planner"] for row in parsed if isinstance(row["planner"], int)]
    orphans: dict[int, list[int]] = {i: [] for i in range(len(parsed))}
    for step_id, row in steps.items():
        if step_id in covered or row["type"] == _USER:
            continue
        owner = bisect_right(bounds, step_id) - 1
        if owner >= 0:
            orphans[owner].append(step_id)
    previews: dict[str, str] = {}
    for step_id in user_steps:
        previews[str(step_id)] = parse_user_text(steps[step_id]["payload"])

    seen: dict[str, dict[str, Any]] = {}
    order: list[str] = []
    for pos, row in enumerate(parsed):
        gen = row["gen"]
        usage = gen["usage"]
        planner = row["planner"] if isinstance(row["planner"], int) else None
        turn = _turn_id(user_steps, planner)
        epoch = None
        if planner is not None:
            stamp = steps.get(planner, {}).get("epoch")
            if isinstance(stamp, float):
                epoch = stamp
        if epoch is None:
            for step_id in row["indices"]:
                stamp = steps.get(step_id, {}).get("epoch")
                if isinstance(stamp, float):
                    epoch = stamp
                    break
        if epoch is None and mtime:
            epoch = mtime
        detail, harness, _reply, compact = _detail_and_harness(
            row["indices"], steps, orphans.get(pos, [])
        )
        call = _call_from_usage(
            usage,
            model=str(gen.get("model") or ""),
            epoch=epoch,
            turn_id=turn,
            detail=detail,
            harness=harness,
            user_preview=previews.get(turn, ""),
            gen_idx=planner if planner is not None else int(row["idx"]),
            compact_after=compact,
        )
        model_id = gen.get("model_id")
        if priced_model(model_id):
            call["billing_model"] = model_id
            label_words = str(gen.get("model") or "").lower().replace("-", " ").split()
            if "priority" in label_words:
                call["service_tier"] = "priority"
            elif "fast" in label_words:
                call["service_tier"] = "fast"
        key = str(call.get("response_id") or "") or f"gen-{row['idx']}"
        prev = seen.get(key)
        if prev is None:
            seen[key] = call
            order.append(key)
            continue
        # A repeated response id is the same call, not a second bill.
        if int(call["total_tokens"]) > int(prev["total_tokens"]):
            if not call.get("detail"):
                call["detail"] = prev.get("detail") or []
            if not call.get("harness"):
                call["harness"] = prev.get("harness") or []
            if not call.get("t"):
                call["t"] = prev.get("t") or ""
                call["_epoch"] = prev.get("_epoch") or 0.0
            if not call.get("user_preview"):
                call["user_preview"] = prev.get("user_preview") or ""
            seen[key] = call
    calls = [seen[key] for key in order]
    calls.sort(key=lambda c: (float(c.get("_epoch") or 0), int(c.get("_idx") or 0)))
    if calls and user_steps and calls[0]["turn_id"] == str(user_steps[0]):
        first = calls[0]
        first_text = parse_user_text(steps[user_steps[0]]["payload"], limit=None)
        if first_text:
            user_tokens = count_tokens(first_text)
            total_input = first["input_tokens"]
            if user_tokens <= total_input:
                first["system_estimate"] = {
                    "tokens": total_input - user_tokens,
                    "label": "First input − user text",
                    "note": "First call total input, including cache, minus a local token count of the full user text. The remainder can include tool definitions, attachments, and other context.",
                    "input_tokens": total_input,
                    "user_tokens": user_tokens,
                }
    for call in calls:
        call.pop("_epoch", None)
        call.pop("_idx", None)
        call.pop("response_id", None)
    return _thread(cid, title, info, parent, mtime, calls, error="")


def _thread(
    cid: str,
    title: str,
    info: dict[str, Any],
    parent: str,
    mtime: float,
    calls: list[dict[str, Any]],
    *,
    error: str,
) -> dict[str, Any]:
    epochs = []
    model = ""
    for call in calls:
        if call.get("model"):
            model = str(call["model"])
        text = call.get("t")
        if isinstance(text, str) and text:
            epochs.append(text)
    started = epochs[0] if epochs else (_iso(mtime) if mtime else "")
    updated = epochs[-1] if epochs else started
    modified = str(info.get("modified") or "")
    if modified and modified > updated:
        updated = modified if "T" in modified else modified.replace(" ", "T") + "+00:00"
    return {
        "id": cid,
        "title": title,
        "cwd": str(info.get("cwd") or ""),
        "model": model,
        "started": started,
        "updated": updated,
        "parent_id": parent,
        "is_root": not parent,
        "context_window": 0,
        "calls": calls,
        "error": error,
    }


def _db_stamp(path: Path) -> tuple[tuple[str, int, int], ...]:
    stamps = []
    for suffix in ("", "-wal"):
        try:
            st = path.with_name(path.name + suffix).stat()
        except OSError:
            continue
        stamps.append((suffix, st.st_mtime_ns, st.st_size))
    return tuple(stamps)


def _parsed_file(
    path: Path,
    summary: Optional[dict[str, Any]],
    known: set[str],
) -> dict[str, Any]:
    stamp = _db_stamp(path)
    if not stamp:
        return _parse_db(path, summary, known)
    parent = _parent_id(str((summary or {}).get("parent_id") or ""), path.stem, known)
    key = (stamp, summary, parent)
    hit = _file_cache.get(path)
    if hit and hit[0] == key:
        return hit[1]
    parsed = _parse_db(path, summary, known)
    if not parsed.get("error"):
        _file_cache[path] = (key, parsed)
    return parsed


def _fingerprint(root: Path) -> str:
    paths = [root / "conversation_summaries.db", *_conversation_paths(root)]
    parts = [f"{path.name}:{_db_stamp(path)}" for path in paths]
    return hashlib.sha1("\n".join(parts).encode()).hexdigest()


def source_revision(home: Optional[Path] = None) -> str:
    root = Path(home) if home is not None else antigravity_home()
    return _fingerprint(root) if root.is_dir() else "missing"


def catalog_rows(home: Optional[Path] = None) -> list[dict[str, Any]]:
    root = Path(home) if home is not None else antigravity_home()
    summaries = _summaries(root)
    paths = _conversation_paths(root)
    known = {path.stem for path in paths}
    rows = []
    for path in paths:
        info = summaries.get(path.stem) or {}
        parent = _parent_id(str(info.get("parent_id") or ""), path.stem, known)
        mtime = max((ns for _, ns, _ in _db_stamp(path)), default=0) / 1e9
        title = str(info.get("title") or "").strip() or path.stem[:8]
        rows.append({
            "id": path.stem,
            "title": title,
            "parent_id": parent,
            "is_root": not parent,
            "updated": _iso(mtime) if mtime else "",
            "_activity": mtime,
        })
    return rows


def _public_header(row: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": row["id"],
        "title": row.get("title") or row["id"],
        "cwd": "",
        "model": "",
        "started": row.get("updated") or "",
        "updated": row.get("updated") or "",
        "parent_id": row.get("parent_id") or "",
        "is_root": bool(row.get("is_root")),
        "context_window": 0,
        "calls": [],
    }


def build_snapshot(
    root: Path,
    *,
    paths: Optional[list[Path]] = None,
    on_progress: Optional[Callable[..., None]] = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    summaries = _summaries(root)
    known = {path.stem for path in _conversation_paths(root)}
    files = paths if paths is not None else _conversation_paths(root)
    threads = []
    errors = 0
    n_calls = 0
    total = len(files) or 1
    for i, path in enumerate(files, start=1):
        try:
            thread = _parsed_file(path, summaries.get(path.stem), known)
        except Exception:
            errors += 1
            continue
        if thread.get("error"):
            errors += 1
        n_calls += len(thread.get("calls") or [])
        threads.append(thread)
        if on_progress is not None:
            on_progress(i, total, 0)
    note = f"{errors} conversation files could not be read" if errors else ""
    return {
        "ok": True,
        "source": "antigravity",
        "metric": _METRIC,
        "note": note,
        "scan_ms": int((time.perf_counter() - started) * 1000),
        "files": {"db": len(files), "read_errors": errors},
        "threads": threads,
        "n_calls": n_calls,
        "n_threads": len(threads),
    }


def load_snapshot(
    home: Optional[Path] = None,
    *,
    on_progress: Optional[Callable[..., None]] = None,
) -> dict[str, Any]:
    root = Path(home) if home is not None else antigravity_home()
    key = str(root.resolve()) if root.exists() else str(root)
    fp = source_revision(root)
    with _lock:
        hit = _cache.get(key)
    if hit and hit[0] == fp:
        if on_progress is not None:
            n = int((hit[1].get("files") or {}).get("db") or 0)
            on_progress(n or 1, n or 1, 0)
        return hit[1]
    payload = build_snapshot(root, on_progress=on_progress)
    payload["source_revision"] = fp
    if not payload["files"]["read_errors"]:
        with _lock:
            _cache[key] = (fp, payload)
    return payload


def focused_snapshot(
    home: Optional[Path] = None,
    focus_id: Optional[str] = None,
) -> dict[str, Any]:
    """Picker rows for every root, with calls for the open root and its children."""
    root = Path(home) if home is not None else antigravity_home()
    key = (source_revision(root), focus_id)
    with _lock:
        hit = _focus_cache.get(root)
    if hit and hit[0] == key:
        return hit[1]
    rows = catalog_rows(root)
    ids = {str(row["id"]) for row in rows}
    chosen = ""
    if focus_id and focus_id in ids:
        chosen = focus_id
        seen: set[str] = set()
        while chosen not in seen:
            seen.add(chosen)
            parent = ""
            for row in rows:
                if row["id"] == chosen:
                    parent = str(row.get("parent_id") or "")
                    break
            if parent not in ids or parent == chosen:
                break
            chosen = parent
    else:
        roots = [row for row in rows if row.get("is_root")]
        roots.sort(key=lambda row: float(row.get("_activity") or 0), reverse=True)
        if roots:
            chosen = str(roots[0]["id"])
    wanted = {chosen} if chosen else set()
    for row in rows:
        if chosen and str(row.get("parent_id") or "") == chosen:
            wanted.add(str(row["id"]))
    paths = [root / "conversations" / f"{cid}.db" for cid in sorted(wanted)]
    paths = [p for p in paths if p.is_file()]
    parsed = build_snapshot(root, paths=paths) if paths else {"threads": []}
    by_id = {
        str(thread.get("id")): thread
        for thread in (parsed.get("threads") or [])
        if isinstance(thread, dict) and thread.get("id")
    }
    merged: list[dict[str, Any]] = []
    for row in rows:
        rid = str(row["id"])
        if rid == chosen and rid in by_id:
            merged.append(by_id[rid])
        elif chosen and str(row.get("parent_id") or "") == chosen and rid in by_id:
            merged.append(by_id[rid])
        elif row.get("is_root"):
            merged.append(_public_header(row))
    snapshot = {
        "ok": True,
        "source": "antigravity",
        "metric": _METRIC,
        "note": parsed.get("note") if isinstance(parsed, dict) else "",
        "scan_ms": int(parsed.get("scan_ms") or 0) if isinstance(parsed, dict) else 0,
        "files": parsed.get("files") if isinstance(parsed, dict) else {},
        "threads": merged,
        "n_calls": int(parsed.get("n_calls") or 0) if isinstance(parsed, dict) else 0,
        "n_threads": len(merged),
    }
    if not (snapshot["files"] or {}).get("read_errors"):
        with _lock:
            _focus_cache[root] = (key, snapshot)
    return snapshot


def newest_mtime(home: Optional[Path] = None) -> float:
    root = Path(home) if home is not None else antigravity_home()
    newest = 0.0
    for path in _conversation_paths(root):
        try:
            newest = max(newest, path.stat().st_mtime)
        except OSError:
            continue
    return newest
