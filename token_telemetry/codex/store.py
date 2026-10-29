"""Read Codex rollout logs into a dashboard snapshot.

Codex writes one JSONL (or .jsonl.zst) per thread under ``$CODEX_HOME/sessions``.
Token facts live on ``token_usage_record``:

- ``usage`` is the delta of that API call. Sum these.
- ``turn_token_usage`` and ``thread_token_usage`` are cumulative. Never sum them.

``event_msg`` / ``token_count`` ``last_token_usage`` is the same kind of delta,
used only when a file has no ``token_usage_record``. Model and effort come from
``turn_context``. Tool names, commands, and message previews are labels on the
call. They are not a second ledger. This module does not import Grok hierarchy
or pricing.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import threading
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Callable, Iterator, Optional, TextIO

_USAGE_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "cache_write_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)

_UUID = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)

_METRIC = (
    "Sum of per-call usage deltas. "
    "cached_input_tokens is included in input_tokens. "
    "reasoning_output_tokens is included in output_tokens. "
    "No dollar price is stored in Codex logs."
)

_lock = threading.Lock()
# home path -> (fingerprint, full payload, slim body, etag)
_cache: dict[str, tuple[str, dict[str, Any], bytes, str]] = {}
# rollout path -> (mtime_ns, size, parsed file)
_file_cache: dict[str, tuple[int, int, dict[str, Any]]] = {}
# home -> (stat fingerprint, catalog rows)
_catalog_cache: dict[str, tuple[str, list[dict[str, Any]]]] = {}
# home -> ((fingerprint, focus id), merged snapshot)
_focus_cache: dict[str, tuple[tuple[str, str], dict[str, Any]]] = {}
# (home, fingerprint, focus id) while that parent's children are parsing
_focus_jobs: set[tuple[str, str, str]] = set()
_HEADER_CAP = 256 * 1024


def codex_home() -> Path:
    raw = (os.environ.get("CODEX_HOME") or "").strip()
    if raw:
        return Path(raw)
    return Path.home() / ".codex"


def clear_cache() -> None:
    with _lock:
        _cache.clear()
        _file_cache.clear()
        _catalog_cache.clear()
        _focus_cache.clear()
        _focus_jobs.clear()


def _parse_cached(path: Path) -> tuple[dict[str, Any], bool]:
    """Parsed file, and whether it came from the memory cache."""
    st = path.stat()
    key = str(path)
    hit = _file_cache.get(key)
    if hit and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
        return hit[2], True
    parsed = _parse_file(path)
    _file_cache[key] = (st.st_mtime_ns, st.st_size, parsed)
    return parsed, False


def _blank() -> dict[str, int]:
    return {k: 0 for k in _USAGE_KEYS}


def _add(dst: dict[str, int], src: dict[str, int]) -> None:
    for k in _USAGE_KEYS:
        dst[k] += src[k]


def _public(usage: dict[str, int]) -> dict[str, int]:
    out = {k: int(usage.get(k, 0)) for k in _USAGE_KEYS}
    out["uncached_input_tokens"] = max(0, out["input_tokens"] - out["cached_input_tokens"])
    return out


def _as_int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def _usage(obj: Any) -> Optional[dict[str, int]]:
    if not isinstance(obj, dict):
        return None
    return {k: _as_int(obj.get(k)) for k in _USAGE_KEYS}


def _source_label(src: Any) -> str:
    if isinstance(src, str):
        return src[:80]
    if isinstance(src, list):
        parts = [str(x) for x in src if isinstance(x, str)]
        return ",".join(parts)[:80]
    if isinstance(src, dict):
        sub = src.get("subagent")
        if isinstance(sub, dict):
            spawn = sub.get("thread_spawn")
            if isinstance(spawn, dict):
                nick = spawn.get("agent_nickname")
                if isinstance(nick, str) and nick:
                    return f"subagent:{nick}"[:80]
            return "subagent"
        if src:
            return "other"
    return ""


def _ids_in_name(name: str) -> list[str]:
    return _UUID.findall(name)


def _zstd_mod() -> Any:
    try:
        import zstandard
    except ImportError:
        return None
    return zstandard


def _iter_lines(path: Path) -> Iterator[str]:
    if path.name.endswith(".jsonl.zst"):
        mod = _zstd_mod()
        if mod is None:
            raise RuntimeError("zstd-unavailable")
        raw = path.open("rb")
        try:
            stream = mod.ZstdDecompressor().stream_reader(raw)
            text: TextIO = io.TextIOWrapper(stream, encoding="utf-8", errors="replace")
            try:
                for line in text:
                    yield line
            finally:
                text.close()
        finally:
            raw.close()
        return
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            yield line


def _titles(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    with path.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(obj, dict):
                continue
            tid = obj.get("id")
            name = obj.get("thread_name")
            if isinstance(tid, str) and isinstance(name, str) and name:
                out[tid] = name[:200]
    return out


_PREVIEW = 160


def _interesting(line: str) -> bool:
    return (
        "token_usage_record" in line
        or "turn_context" in line
        or "session_meta" in line
        or "token_count" in line
        or "response_item" in line
        or "item_completed" in line
    )


def _clip(text: Any, n: int = _PREVIEW) -> str:
    if not isinstance(text, str):
        return ""
    flat = " ".join(text.split())
    if len(flat) <= n:
        return flat
    return flat[: n - 1] + "…"


def _text_from_content(content: Any) -> str:
    if isinstance(content, str):
        return _clip(content, 240)
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for part in content:
        if isinstance(part, str):
            parts.append(part)
        elif isinstance(part, dict):
            raw = part.get("text") or part.get("content")
            if isinstance(raw, str):
                parts.append(raw)
    return _clip(" ".join(parts), 240)


def _arg_bits(raw: Any) -> tuple[str, str]:
    """Short command and path from a tool payload. No full file bodies."""
    obj: Any = raw
    if isinstance(raw, str):
        if raw[:1] not in "{[":
            return _clip(raw), ""
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            return _clip(raw), ""
    if isinstance(obj, list):
        return _clip(" ".join(str(x) for x in obj if isinstance(x, (str, int)))), ""
    if not isinstance(obj, dict):
        return "", ""
    command = ""
    path = ""
    for key in ("command", "cmd", "input"):
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            command = _clip(val)
            break
        if isinstance(val, list) and val:
            command = _clip(" ".join(str(x) for x in val if isinstance(x, (str, int))))
            break
    for key in ("path", "file", "file_path", "cwd"):
        val = obj.get(key)
        if isinstance(val, str) and val.strip():
            path = _clip(val, 200)
            break
    if command or path:
        return command, path
    bits: list[str] = []
    for key, val in obj.items():
        if key in ("content", "output", "stdout", "stderr"):
            continue
        if isinstance(val, bool) or val is None:
            continue
        if isinstance(val, (int, float)):
            bits.append(f"{key}={val}")
        elif isinstance(val, str) and val and len(val) <= 40:
            bits.append(f"{key}={val}")
    return _clip(" ".join(bits)), ""


def _file_from_read(command: str) -> str:
    """File argument of a cat / Get-Content style read. Empty when it is not one."""
    match = _READ_FILE.search(command or "")
    if match is None:
        return ""
    return _clip(match.group(1) or match.group(2) or match.group(3) or "", 200)


def _turn_of(payload: dict[str, Any]) -> str:
    turn = payload.get("turn_id")
    if isinstance(turn, str) and turn:
        return turn
    meta = payload.get("internal_chat_message_metadata_passthrough")
    if isinstance(meta, dict):
        turn = meta.get("turn_id")
        if isinstance(turn, str):
            return turn
    return ""


_TOOL_REF = re.compile(r"tools\.([A-Za-z0-9_]+)")
_CMD_ARG = re.compile(r"cmd\s*:\s*\"((?:\\.|[^\"])*)\"")
_FILE_ARG = re.compile(r"(?:Update|Add|Delete) File:\s*(\S+)")
_READ_FILE = re.compile(
    r"(?:^|[^A-Za-z0-9_])(?:Get-Content|gc|cat|head|tail|type)\s+"
    r"(?:-[A-Za-z]+\s+)*(?:\"([^\"]+)\"|'([^']+)'|(\S+))",
    re.IGNORECASE,
)


def _event(
    kind: str,
    *,
    name: str = "",
    call_id: str = "",
    preview: str = "",
    command: str = "",
    path: str = "",
    chars: int = 0,
) -> dict[str, str]:
    return {
        "kind": kind,
        "name": name,
        "call_id": call_id,
        "preview": preview,
        "command": command,
        "path": path,
        "chars": str(max(0, int(chars))),
    }


def _content_len(content: Any) -> int:
    if isinstance(content, str):
        return len(content)
    if not isinstance(content, list):
        return 0
    n = 0
    for part in content:
        if isinstance(part, str):
            n += len(part)
        elif isinstance(part, dict):
            raw = part.get("text") or part.get("content")
            if isinstance(raw, str):
                n += len(raw)
    return n


def _purpose(tool: str, blob: str) -> str:
    """Map a Codex tool call onto the same names the Grok tree shows."""
    name = (tool or "").strip()
    low = blob.lower()
    key = name.lower()
    if key in ("exec", "exec_command", "write_stdin", "shell"):
        if re.search(r"(^|[^a-z])(rg|grep|findstr|select-string)([^a-z]|$)", low):
            return "grep"
        if re.search(r"(^|[^a-z])(get-content|cat|head|tail)([^a-z]|$)", low):
            return "read_file"
        if "begin patch" in low or "update file:" in low or "add file:" in low:
            return "search_replace"
        return "run_terminal_command"
    if key in ("web__run", "web_search"):
        if "search_query" in low or "search:" in low:
            return "web_search"
        if "open" in low:
            return "open_page"
        return "web_search"
    return {
        "read_text": "read_file",
        "read_json": "read_file",
        "view_image": "read_file",
        "spawn_agent": "spawn_subagent",
        "apply_patch": "search_replace",
        "js": "mcp",
    }.get(key, name or "tool")


def _tool_events(name: str, raw: Any, call_id: str) -> list[dict[str, str]]:
    text = raw if isinstance(raw, str) else json.dumps(raw, ensure_ascii=False) if isinstance(raw, (dict, list)) else ""
    hits = list(_TOOL_REF.finditer(text))
    rows: list[tuple[str, str, int]] = []
    if hits:
        for i, match in enumerate(hits):
            end = hits[i + 1].start() if i + 1 < len(hits) else len(text)
            blob = text[match.start():end]
            rows.append((match.group(1), blob, len(blob)))
    elif text:
        rows.append((name or "tool", text, len(text)))
    events = []
    for tool, blob, chars in rows:
        purpose = _purpose(tool, blob)
        cmd_m = _CMD_ARG.search(blob)
        file_m = _FILE_ARG.search(blob)
        command = ""
        path = ""
        if cmd_m:
            command = _clip(cmd_m.group(1).replace("\\n", " "))
        elif purpose == "search_replace":
            command = _clip(blob)
        else:
            command, path = _arg_bits(blob if blob[:1] in "{[" else raw)
        if file_m:
            path = _clip(file_m.group(1), 200)
        if purpose == "read_file" and not path:
            path = _file_from_read(command or blob)
        if purpose == "read_file" and path:
            # Same row as Grok: the file, not the shell line that opened it.
            preview = path
            command = ""
        else:
            preview = command or path
        events.append(
            _event(
                "tool_request",
                name=purpose,
                call_id=call_id,
                preview=preview,
                command=command,
                path=path,
                chars=chars,
            )
        )
    return events


def _take_response_item(
    payload: dict[str, Any],
    llm_buf: list[dict[str, str]],
    closed: Optional[dict[str, Any]],
    remember_user,
    has_llm,
) -> None:
    pt = payload.get("type")
    if not isinstance(pt, str):
        return
    turn = _turn_of(payload)
    if pt == "message":
        role = payload.get("role") if isinstance(payload.get("role"), str) else ""
        text = _text_from_content(payload.get("content"))
        if role == "user":
            remember_user(turn, text, event=False)
        elif role == "assistant" and text:
            chars = _content_len(payload.get("content"))
            if llm_buf and llm_buf[-1].get("kind") == "message" and not llm_buf[-1].get("preview"):
                llm_buf[-1]["preview"] = text
                llm_buf[-1]["chars"] = str(chars)
            elif not (llm_buf and llm_buf[-1].get("kind") == "message" and llm_buf[-1].get("preview") == text):
                llm_buf.append(_event("message", preview=text, chars=chars))
        return
    if pt == "reasoning":
        summary = _text_from_content(payload.get("summary"))
        enc = payload.get("encrypted_content")
        chars = len(enc) if isinstance(enc, str) else _content_len(payload.get("summary"))
        if has_llm("reasoning", "thought"):
            if summary:
                for ev in llm_buf:
                    if ev.get("kind") in ("reasoning", "thought") and not ev.get("preview"):
                        ev["preview"] = summary
                        ev["kind"] = "thought"
                        if chars and ev.get("chars") in ("", "0"):
                            ev["chars"] = str(chars)
                        break
            return
        if summary:
            llm_buf.append(_event("thought", preview=summary, chars=chars))
        else:
            llm_buf.append(_event("reasoning", chars=chars))
        return
    if pt.endswith("_output") and "call" in pt:
        if closed is None or closed.get("harness"):
            return
        call_id = payload.get("call_id") if isinstance(payload.get("call_id"), str) else ""
        name = "tool"
        for ev in closed.get("detail") or []:
            if ev.get("call_id") == call_id and ev.get("name"):
                name = ev["name"]
                break
        text = payload.get("output")
        preview = _text_from_content(text) if not isinstance(text, str) else _clip(text)
        chars = len(text) if isinstance(text, str) else _content_len(text)
        closed["harness"].append(
            _event("tool", name=name, call_id=call_id, preview=preview, command=preview, chars=chars)
        )
        return
    if "tool_call" in pt or pt == "function_call":
        name = payload.get("name") if isinstance(payload.get("name"), str) else ""
        if not name:
            name = pt
        call_id = payload.get("call_id") if isinstance(payload.get("call_id"), str) else ""
        raw = payload.get("input")
        if raw is None:
            raw = payload.get("arguments")
        llm_buf.extend(_tool_events(name, raw, call_id))


def _take_item_completed(
    payload: dict[str, Any],
    llm_buf: list[dict[str, str]],
    closed: Optional[dict[str, Any]],
    remember_user,
    has_llm,
) -> None:
    item = payload.get("item") if isinstance(payload.get("item"), dict) else {}
    it = item.get("type")
    if not isinstance(it, str):
        return
    turn = _turn_of(payload)
    if it == "UserMessage":
        remember_user(turn, _text_from_content(item.get("content")), event=True)
        return
    if it == "ContextCompaction":
        if closed is not None:
            closed["compact_after"] = True
        return
    if it == "Reasoning":
        summary = _text_from_content(item.get("summary_text") or item.get("raw_content"))
        if has_llm("reasoning", "thought"):
            return
        if summary:
            llm_buf.append(_event("thought", preview=summary))
        else:
            llm_buf.append(_event("reasoning"))
        return
    if it in ("AgentMessage", "Message"):
        return
    if it.endswith("Call") or it.endswith("Request"):
        return
    if closed is None:
        return
    name = it
    command = ""
    path = ""
    preview = ""
    chars = 0
    if it == "CommandExecution":
        cmd = item.get("command")
        joined = ""
        if isinstance(cmd, list):
            joined = " ".join(str(x) for x in cmd if isinstance(x, (str, int)))
        elif isinstance(cmd, str):
            joined = cmd
        command = _clip(joined)
        name = _purpose("exec_command", joined)
        cwd = item.get("cwd") if isinstance(item.get("cwd"), str) else ""
        path = _clip(cwd, 200)
        preview = command
        if name == "read_file":
            file_path = _file_from_read(joined)
            if file_path:
                path = file_path
                command = ""
                preview = file_path
        out = item.get("aggregated_output")
        chars = len(joined) + (len(out) if isinstance(out, str) else 0)
    elif it == "FileChange":
        changes = item.get("changes") if isinstance(item.get("changes"), dict) else {}
        action = ""
        if changes:
            full = next(iter(changes))
            path = _clip(str(full), 200)
            change = changes.get(full)
            action = change.get("type") if isinstance(change, dict) else ""
            preview = _clip(f"{action} {Path(str(full)).name}".strip())
            for change in changes.values():
                if isinstance(change, dict) and isinstance(change.get("content"), str):
                    chars += len(change["content"])
        name = "write" if str(action).lower() in ("add", "create", "write") else "search_replace"
    elif it == "Extension":
        kind = item.get("kind") if isinstance(item.get("kind"), str) else "extension"
        query = item.get("query") if isinstance(item.get("query"), str) else ""
        action = item.get("action") if isinstance(item.get("action"), dict) else {}
        url = action.get("url") if isinstance(action.get("url"), str) else ""
        name = _purpose(kind.replace(".", "_"), f"{query} {url}")
        preview = _clip(query or url)
        path = _clip(url, 200)
        chars = len(query) + len(url)
    else:
        kind = item.get("kind") if isinstance(item.get("kind"), str) else ""
        name = kind or it
        query = item.get("query") if isinstance(item.get("query"), str) else ""
        preview = _clip(query)
        chars = len(query)
    closed["harness"].append(
        _event("tool", name=name, preview=preview, command=command, path=path, chars=chars)
    )


def _parse_file(path: Path) -> dict[str, Any]:
    """One rollout. ``mode`` is ``usage`` or ``token_count``."""
    meta_id = ""
    parent_id = ""
    cwd = ""
    originator = ""
    cli_version = ""
    source = ""
    started = ""
    window = 0
    current_model = ""
    current_effort = ""
    current_turn = ""
    by_turn: dict[str, tuple[str, str]] = {}
    calls: list[dict[str, Any]] = []
    fallback: list[dict[str, Any]] = []
    llm_buf: list[dict[str, str]] = []
    closed: Optional[dict[str, Any]] = None
    turn_user: dict[str, str] = {}
    turn_user_event: set[str] = set()

    def _remember_user(turn: str, text: str, *, event: bool) -> None:
        shown = _clip(text, 240)
        if not shown:
            return
        key = turn or current_turn
        if not key:
            return
        if event:
            turn_user[key] = shown
            turn_user_event.add(key)
        elif key not in turn_user_event and key not in turn_user:
            turn_user[key] = shown

    def _seal(call: dict[str, Any]) -> None:
        nonlocal llm_buf, closed
        turn = call["turn_id"] or current_turn
        call["turn_id"] = turn
        call["detail"] = llm_buf
        call["harness"] = []
        call["user_preview"] = turn_user.get(turn, "")
        call["compact_after"] = False
        llm_buf = []
        closed = call

    def _has_llm(*kinds: str) -> bool:
        return any(ev.get("kind") in kinds for ev in llm_buf)

    for line in _iter_lines(path):
        if not _interesting(line):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        kind = obj.get("type")
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        ts = obj.get("timestamp") if isinstance(obj.get("timestamp"), str) else ""

        if kind == "session_meta":
            mid = payload.get("id")
            if isinstance(mid, str) and mid:
                meta_id = mid
            parent = payload.get("parent_thread_id")
            if isinstance(parent, str):
                parent_id = parent
            if isinstance(payload.get("cwd"), str):
                cwd = payload["cwd"]
            if isinstance(payload.get("originator"), str):
                originator = payload["originator"]
            if isinstance(payload.get("cli_version"), str):
                cli_version = payload["cli_version"]
            source = _source_label(payload.get("source")) or source
            stamp = payload.get("timestamp")
            if isinstance(stamp, str) and stamp:
                started = stamp
            elif ts and not started:
                started = ts
            continue

        if kind == "turn_context":
            model = payload.get("model")
            effort = payload.get("effort")
            if isinstance(model, str) and model:
                current_model = model
            if isinstance(effort, str) and effort:
                current_effort = effort
            turn_id = payload.get("turn_id")
            if isinstance(turn_id, str) and turn_id:
                current_turn = turn_id
                by_turn[turn_id] = (current_model, current_effort)
            continue

        if kind == "token_usage_record":
            usage = _usage(payload.get("usage"))
            if usage is None:
                continue
            turn_id = payload.get("turn_id") if isinstance(payload.get("turn_id"), str) else ""
            if turn_id:
                current_turn = turn_id
            model, effort = by_turn.get(turn_id, (current_model, current_effort))
            response_id = payload.get("response_id")
            call = {
                "t": ts,
                "turn_id": turn_id,
                "response_id": response_id if isinstance(response_id, str) else "",
                "model": model,
                "effort": effort,
                "usage": usage,
            }
            _seal(call)
            calls.append(call)
            continue

        if kind == "response_item":
            _take_response_item(payload, llm_buf, closed, _remember_user, _has_llm)
            continue

        if kind == "event_msg" and payload.get("type") == "item_completed":
            _take_item_completed(payload, llm_buf, closed, _remember_user, _has_llm)
            continue

        if kind == "event_msg" and payload.get("type") == "token_count":
            info = payload.get("info") if isinstance(payload.get("info"), dict) else {}
            win = info.get("model_context_window")
            if isinstance(win, (int, float)) and not isinstance(win, bool) and win > 0:
                window = int(win)
            if calls:
                continue
            usage = _usage(info.get("last_token_usage"))
            if usage is None:
                continue
            call = {
                "t": ts,
                "turn_id": current_turn,
                "response_id": "",
                "model": current_model,
                "effort": current_effort,
                "usage": usage,
            }
            _seal(call)
            fallback.append(call)

    ids = _ids_in_name(path.name)
    thread_id = meta_id or (ids[-1] if ids else path.name)
    return {
        "id": thread_id,
        "parent_id": parent_id if parent_id and parent_id != thread_id else "",
        "cwd": cwd,
        "originator": originator,
        "cli_version": cli_version,
        "source": source,
        "started": started,
        "window": window,
        "calls": calls if calls else fallback,
        "from_usage_record": bool(calls),
    }


def _project_name(cwd: str) -> str:
    if not cwd:
        return ""
    return Path(cwd).name


def _fingerprint(home: Path) -> str:
    parts: list[str] = []
    sessions = home / "sessions"
    index = home / "session_index.jsonl"
    if index.is_file():
        st = index.stat()
        parts.append(f"index:{st.st_mtime_ns}:{st.st_size}")
    if sessions.is_dir():
        for path in sorted(sessions.rglob("*")):
            if not path.is_file():
                continue
            if path.name.endswith(".jsonl") or path.name.endswith(".jsonl.zst"):
                st = path.stat()
                parts.append(f"{path.name}:{st.st_mtime_ns}:{st.st_size}")
    parts.append("zstd" if _zstd_mod() is not None else "nozstd")
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()


def _empty(note: str, *, zst_skipped: int = 0) -> dict[str, Any]:
    return {
        "ok": True,
        "source": "codex",
        "metric": _METRIC,
        "note": note,
        "scan_ms": 0,
        "files": {"jsonl": 0, "zst": 0, "zst_skipped": zst_skipped, "read_errors": 0},
        "totals": _public(_blank()),
        "by_day": [],
        "by_model": [],
        "threads": [],
    }


def newest_day_paths(home: Path) -> list[Path]:
    """Rollouts in the latest day folder only.

    The live session is there. Walking every older day is the historical
    backfill, not the first paint.
    """
    day = Path(home) / "sessions"
    if not day.is_dir():
        return []
    try:
        for _ in range(3):
            kids = [p for p in day.iterdir() if p.is_dir()]
            if not kids:
                break
            day = max(kids, key=lambda p: p.name)
        out = []
        for path in day.iterdir():
            name = path.name
            if path.is_file() and (name.endswith(".jsonl") or name.endswith(".jsonl.zst")):
                out.append(path)
        return out
    except OSError:
        return []


def build_snapshot(
    home: Path,
    *,
    paths: Optional[list[Path]] = None,
    pause_s: float = 0.0,
    on_progress: Optional[Callable[..., None]] = None,
) -> dict[str, Any]:
    started = time.perf_counter()
    home = Path(home)
    if not home.is_dir():
        snap = _empty("Codex home not found")
        snap["scan_ms"] = 0
        return snap

    titles = _titles(home / "session_index.jsonl")
    sessions = home / "sessions"
    full_scan = paths is None
    if paths is None:
        paths = []
        if sessions.is_dir():
            for path in sessions.rglob("*"):
                if path.is_file() and (
                    path.name.endswith(".jsonl") or path.name.endswith(".jsonl.zst")
                ):
                    paths.append(path)
    else:
        paths = list(paths)
    paths.sort(key=lambda p: p.name)

    def _tick(done: int, total: int, cold: int) -> None:
        if on_progress is None:
            return
        on_progress(done, total, cold)

    file_total = len(paths)
    if file_total <= 0:
        _tick(1, 1, 0)
    else:
        # cold stays 0 until a file is actually read. A cache hit must not
        # look like a full history rebuild.
        _tick(0, file_total, 0)

    zstd_ok = _zstd_mod() is not None
    n_jsonl = 0
    n_zst = 0
    n_skip = 0
    n_err = 0
    parsed: list[dict[str, Any]] = []
    for done, path in enumerate(paths, start=1):
        skipped = False
        if path.name.endswith(".jsonl.zst"):
            n_zst += 1
            if not zstd_ok:
                n_skip += 1
                skipped = True
        else:
            n_jsonl += 1
        cold = 0
        if not skipped:
            try:
                item, hit = _parse_cached(path)
                parsed.append(item)
                cold = 0 if hit else 1
            except Exception:
                n_err += 1
                cold = 1
            # A full history parse used to hold the interpreter until every
            # rollout was read, so the Grok page could not load either.
            if pause_s:
                time.sleep(pause_s)
        if file_total:
            _tick(done, file_total, cold)
    if full_scan:
        live = {str(path) for path in paths}
        root_prefix = (str(home.resolve()) if home.exists() else str(home)) + os.sep
        for key in list(_file_cache):
            if key.startswith(root_prefix) and key not in live:
                _file_cache.pop(key, None)

    threads: dict[str, dict[str, Any]] = {}
    seen_response: dict[str, set[str]] = defaultdict(set)

    for item in parsed:
        tid = item["id"]
        th = threads.get(tid)
        if th is None:
            th = {
                "id": tid,
                "title": titles.get(tid) or tid,
                "cwd": item["cwd"],
                "project": _project_name(item["cwd"]),
                "originator": item["originator"],
                "cli_version": item["cli_version"],
                "source": item["source"],
                "parent_id": item["parent_id"],
                "started": item["started"],
                "updated": item["started"],
                "context_window": item["window"],
                "calls": [],
                "_usage": _blank(),
            }
            threads[tid] = th
        else:
            if item["cwd"] and not th["cwd"]:
                th["cwd"] = item["cwd"]
                th["project"] = _project_name(item["cwd"])
            if item["originator"] and not th["originator"]:
                th["originator"] = item["originator"]
            if item["cli_version"] and not th["cli_version"]:
                th["cli_version"] = item["cli_version"]
            if item["source"] and not th["source"]:
                th["source"] = item["source"]
            if item["parent_id"] and not th["parent_id"]:
                th["parent_id"] = item["parent_id"]
            if item["started"] and (not th["started"] or item["started"] < th["started"]):
                th["started"] = item["started"]
            if item["window"]:
                th["context_window"] = item["window"]
        if titles.get(tid):
            th["title"] = titles[tid]

        seen = seen_response[tid]
        for call in item["calls"]:
            rid = call["response_id"]
            if rid:
                if rid in seen:
                    continue
                seen.add(rid)
            th["calls"].append(call)
            _add(th["_usage"], call["usage"])
            if call["t"] and (not th["updated"] or call["t"] > th["updated"]):
                th["updated"] = call["t"]

    ids = set(threads)
    children: dict[str, list[str]] = defaultdict(list)
    for th in threads.values():
        parent = th["parent_id"]
        if parent in ids and parent != th["id"]:
            children[parent].append(th["id"])
        elif parent not in ids:
            th["parent_missing"] = bool(parent)
        else:
            th["parent_missing"] = False
        th.setdefault("parent_missing", False)

    def rolled(tid: str, stack: set[str]) -> tuple[dict[str, int], int]:
        if tid in stack:
            return _blank(), 0
        stack.add(tid)
        total = dict(threads[tid]["_usage"])
        n = len(threads[tid]["calls"])
        for cid in children.get(tid, []):
            sub, sub_n = rolled(cid, stack)
            _add(total, sub)
            n += sub_n
        stack.remove(tid)
        return total, n

    by_day: dict[str, dict[str, int]] = defaultdict(_blank)
    day_calls: dict[str, int] = defaultdict(int)
    by_model: dict[tuple[str, str], dict[str, int]] = defaultdict(_blank)
    model_calls: dict[tuple[str, str], int] = defaultdict(int)
    totals = _blank()
    n_calls = 0

    out_threads: list[dict[str, Any]] = []
    for tid, th in threads.items():
        calls_out = []
        for call in th["calls"]:
            n_calls += 1
            usage = call["usage"]
            _add(totals, usage)
            day = (call["t"] or "")[:10] or "unknown"
            _add(by_day[day], usage)
            day_calls[day] += 1
            key = (call["model"] or "unknown", call["effort"] or "")
            _add(by_model[key], usage)
            model_calls[key] += 1
            calls_out.append(
                {
                    "t": call["t"],
                    "turn_id": call["turn_id"],
                    "model": call["model"],
                    "effort": call["effort"],
                    "detail": list(call.get("detail") or []),
                    "harness": list(call.get("harness") or []),
                    "user_preview": call.get("user_preview") or "",
                    "compact_after": bool(call.get("compact_after")),
                    **_public(usage),
                }
            )
        calls_out.sort(key=lambda c: c["t"] or "")
        kids = sorted(children.get(tid, []), key=lambda cid: threads[cid]["updated"] or "")
        last_model = ""
        last_effort = ""
        for call in reversed(calls_out):
            if call["model"]:
                last_model = call["model"]
                last_effort = call["effort"]
                break
        with_children, n_with_children = rolled(tid, set())
        out_threads.append(
            {
                "id": tid,
                "title": th["title"],
                "cwd": th["cwd"],
                "project": th["project"],
                "originator": th["originator"],
                "cli_version": th["cli_version"],
                "source": th["source"],
                "parent_id": th["parent_id"],
                "parent_missing": th["parent_missing"],
                "is_root": th["parent_id"] not in ids,
                "started": th["started"],
                "updated": th["updated"],
                "context_window": th["context_window"],
                "model": last_model,
                "effort": last_effort,
                "n_calls": len(calls_out),
                "n_calls_with_children": n_with_children,
                "child_ids": kids,
                "usage": _public(th["_usage"]),
                "usage_with_children": _public(with_children),
                "calls": calls_out,
            }
        )

    out_threads.sort(key=lambda t: (t["updated"] or "", t["id"]), reverse=True)

    days = []
    for day in sorted(by_day):
        days.append({"day": day, "calls": day_calls[day], **_public(by_day[day])})
    models = []
    for key in sorted(by_model, key=lambda k: by_model[k]["total_tokens"], reverse=True):
        model, effort = key
        models.append(
            {
                "model": model,
                "effort": effort,
                "calls": model_calls[key],
                **_public(by_model[key]),
            }
        )

    note = ""
    if n_skip:
        note = f"{n_skip} compressed rollouts skipped (zstandard not installed)"
    elif n_err:
        note = f"{n_err} rollout files could not be read"

    return {
        "ok": True,
        "source": "codex",
        "metric": _METRIC,
        "note": note,
        "scan_ms": int((time.perf_counter() - started) * 1000),
        "files": {
            "jsonl": n_jsonl,
            "zst": n_zst,
            "zst_skipped": n_skip,
            "read_errors": n_err,
        },
        "totals": _public(totals),
        "by_day": days,
        "by_model": models,
        "threads": out_threads,
        "n_calls": n_calls,
        "n_threads": len(out_threads),
    }


def _slim(payload: dict[str, Any]) -> dict[str, Any]:
    """State payload for the page. Per-call rows stay on the thread route."""
    threads = []
    for thread in payload.get("threads") or []:
        if isinstance(thread, dict):
            threads.append({k: v for k, v in thread.items() if k != "calls"})
    return {**payload, "threads": threads}


def _load(
    home: Optional[Path],
    *,
    pause_s: float = 0.0,
    on_progress: Optional[Callable[..., None]] = None,
) -> tuple[dict[str, Any], bytes, str]:
    root = Path(home) if home is not None else codex_home()
    key = str(root.resolve()) if root.exists() else str(root)
    fp = _fingerprint(root) if root.is_dir() else "missing"
    with _lock:
        hit = _cache.get(key)
        if hit and hit[0] == fp:
            if on_progress is not None:
                files = hit[1].get("files") if isinstance(hit[1], dict) else {}
                n = int((files or {}).get("jsonl") or 0) + int((files or {}).get("zst") or 0)
                on_progress(n or 1, n or 1, 0)
            return hit[1], hit[2], hit[3]
    payload = build_snapshot(root, pause_s=pause_s, on_progress=on_progress)
    body = json.dumps(_slim(payload), ensure_ascii=False, separators=(",", ":")).encode(
        "utf-8"
    )
    etag = '"' + hashlib.sha1(body).hexdigest()[:16] + '"'
    with _lock:
        _cache[key] = (fp, payload, body, etag)
    return payload, body, etag


def load_snapshot(
    home: Optional[Path] = None,
    *,
    pause_s: float = 0.0,
    on_progress: Optional[Callable[..., None]] = None,
) -> dict[str, Any]:
    """Full snapshot, including per-call rows. Cached on the rollout fingerprint.

    ``pause_s`` yields between files so a history backfill does not freeze
    the other dashboard (Grok) while it reads. ``on_progress(done, total, cold)``
    fires once per rollout so Daily/Weekly/Monthly can show n/n while scanning.
    """
    payload, _body, _etag = _load(home, pause_s=pause_s, on_progress=on_progress)
    return payload


def _header_text(path: Path) -> str:
    """Only the start of a rollout. session_meta is the first record."""
    if path.name.endswith(".jsonl.zst"):
        mod = _zstd_mod()
        if mod is None:
            return ""
        raw = path.open("rb")
        try:
            stream = mod.ZstdDecompressor().stream_reader(raw)
            return stream.read(_HEADER_CAP).decode("utf-8", errors="replace")
        except Exception:
            return ""
        finally:
            raw.close()
    try:
        with path.open("rb") as fh:
            return fh.read(_HEADER_CAP).decode("utf-8", errors="replace")
    except OSError:
        return ""


def _header(path: Path) -> dict[str, str]:
    found: dict[str, str] = {"id": "", "parent_id": "", "cwd": "", "source": "", "started": ""}
    text = _header_text(path)
    for line in text.splitlines():
        if "session_meta" not in line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict) or obj.get("type") != "session_meta":
            continue
        payload = obj.get("payload") if isinstance(obj.get("payload"), dict) else {}
        mid = payload.get("id")
        if isinstance(mid, str):
            found["id"] = mid
        parent = payload.get("parent_thread_id")
        if isinstance(parent, str):
            found["parent_id"] = parent
        if isinstance(payload.get("cwd"), str):
            found["cwd"] = payload["cwd"]
        found["source"] = _source_label(payload.get("source"))
        stamp = payload.get("timestamp")
        if isinstance(stamp, str):
            found["started"] = stamp
        break
    if not found["id"]:
        ids = _ids_in_name(path.name)
        found["id"] = ids[0] if ids else path.name
    return found


def _rollout_paths(home: Path) -> list[Path]:
    sessions = home / "sessions"
    if not sessions.is_dir():
        return []
    out: list[Path] = []
    try:
        for path in sessions.rglob("*"):
            if path.is_file() and (
                path.name.endswith(".jsonl") or path.name.endswith(".jsonl.zst")
            ):
                out.append(path)
    except OSError:
        return out
    return out


def catalog_rows(home: Optional[Path] = None) -> list[dict[str, Any]]:
    """One row per thread, from the file header and session index.

    Guardian reviews are child rollouts (`parent_thread_id` set). Their
    parent often lives on an earlier day. The picker lists roots, and a
    child's mtime counts as activity on that parent.
    """
    root = Path(home) if home is not None else codex_home()
    if not root.is_dir():
        return []
    _fp, rows = _catalog(root)
    return rows


def _catalog(home: Path) -> tuple[str, list[dict[str, Any]]]:
    specs: list[tuple[Path, os.stat_result]] = []
    parts: list[str] = []
    for path in _rollout_paths(home):
        try:
            st = path.stat()
        except OSError:
            continue
        specs.append((path, st))
        parts.append(f"{path}:{st.st_mtime_ns}:{st.st_size}")
    fp = hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()
    key = str(home.resolve()) if home.exists() else str(home)
    with _lock:
        hit = _catalog_cache.get(key)
        if hit and hit[0] == fp:
            return fp, hit[1]
    titles = _titles(home / "session_index.jsonl")
    by_id: dict[str, dict[str, Any]] = {}
    for path, st in specs:
        header = _header(path)
        tid = header["id"]
        row = by_id.get(tid)
        if row is None:
            row = {
                "id": tid,
                "title": titles.get(tid) or tid,
                "cwd": header["cwd"],
                "parent_id": header["parent_id"],
                "source": header["source"],
                "started": header["started"],
                "updated": "",
                "calls": [],
                "is_root": True,
                "_mtime": 0.0,
                "_paths": [],
            }
            by_id[tid] = row
        row["_paths"].append(str(path))
        if st.st_mtime > float(row["_mtime"]):
            row["_mtime"] = float(st.st_mtime)
        if header["parent_id"] and not row["parent_id"]:
            row["parent_id"] = header["parent_id"]
        if header["cwd"] and not row["cwd"]:
            row["cwd"] = header["cwd"]
        if titles.get(tid):
            row["title"] = titles[tid]
    ids = set(by_id)
    extra = {tid: 0.0 for tid in ids}
    for row in by_id.values():
        parent = str(row.get("parent_id") or "")
        if parent in ids and parent != row["id"]:
            extra[parent] = max(extra[parent], float(row["_mtime"]))
            row["is_root"] = False
        else:
            row["is_root"] = True
    for row in by_id.values():
        when = max(float(row["_mtime"]), extra.get(row["id"], 0.0))
        row["_activity"] = when
        row["updated"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(when))
    rows = list(by_id.values())
    with _lock:
        _catalog_cache[key] = (fp, rows)
    return fp, rows


def _public_row(row: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in row.items() if not str(k).startswith("_")}


def _kick_children(
    root: Path,
    home_key: str,
    cache_key: tuple[str, str],
    chosen: str,
    child_paths: list[Path],
) -> None:
    """Parse the open parent's children after the parent is already on screen.

    The session request returns the parent first. The next poll picks up
    the filled sub-agent tabs and their share of the session total.
    """
    job = (home_key, cache_key[0], cache_key[1])
    with _lock:
        if job in _focus_jobs:
            return
        _focus_jobs.add(job)

    def run() -> None:
        try:
            parsed = build_snapshot(root, paths=child_paths) if child_paths else {"threads": []}
            parsed_by = {
                str(thread.get("id")): thread
                for thread in (parsed.get("threads") or [])
                if isinstance(thread, dict) and thread.get("id")
            }
            with _lock:
                hit = _focus_cache.get(home_key)
                if not hit or hit[0] != cache_key:
                    return
                base = hit[1]
            threads: list[dict[str, Any]] = []
            for thread in base.get("threads") or []:
                if not isinstance(thread, dict):
                    continue
                tid = str(thread.get("id") or "")
                if tid in parsed_by and tid != chosen:
                    threads.append(parsed_by[tid])
                else:
                    threads.append(thread)
            snap = dict(base)
            snap["threads"] = threads
            snap["children_loaded"] = True
            with _lock:
                hit = _focus_cache.get(home_key)
                if hit and hit[0] == cache_key:
                    _focus_cache[home_key] = (cache_key, snap)
        except Exception:
            with _lock:
                hit = _focus_cache.get(home_key)
                if hit and hit[0] == cache_key:
                    failed = dict(hit[1])
                    failed["children_loaded"] = True
                    _focus_cache[home_key] = (cache_key, failed)
        finally:
            with _lock:
                _focus_jobs.discard(job)

    threading.Thread(target=run, name="codex-subagents", daemon=True).start()


def focused_snapshot(
    home: Optional[Path] = None,
    focus_id: Optional[str] = None,
) -> dict[str, Any]:
    """Roots for the picker, and the open root parsed.

    A newer child file does not become the session. Its parent does.
    Child rollouts are listed immediately and parsed after this returns,
    so the first paint is not waiting on every sub-agent file.
    Other days are not parsed.
    """
    root = Path(home) if home is not None else codex_home()
    if not root.is_dir():
        return build_snapshot(root, paths=[])
    fp, rows = _catalog(root)
    ids = {str(r["id"]) for r in rows}
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
        roots = [r for r in rows if r.get("is_root")]
        roots.sort(key=lambda r: float(r.get("_activity") or 0), reverse=True)
        if roots:
            chosen = str(roots[0]["id"])
    home_key = str(root.resolve()) if root.exists() else str(root)
    cache_key = (fp, chosen)
    child_paths: list[Path] = []
    for row in rows:
        if chosen and str(row.get("parent_id") or "") == chosen:
            child_paths.extend(Path(p) for p in row.get("_paths") or [])
    with _lock:
        hit = _focus_cache.get(home_key)
    if hit and hit[0] == cache_key:
        if not hit[1].get("children_loaded") and child_paths:
            _kick_children(root, home_key, cache_key, chosen, child_paths)
        return hit[1]
    paths: list[Path] = []
    activity = ""
    title = ""
    for row in rows:
        if row["id"] == chosen:
            activity = str(row.get("updated") or "")
            title = str(row.get("title") or "")
            paths.extend(Path(p) for p in row.get("_paths") or [])
    parsed = build_snapshot(root, paths=paths) if paths else {"threads": []}
    parsed_by = {
        str(thread.get("id")): thread
        for thread in (parsed.get("threads") or [])
        if isinstance(thread, dict) and thread.get("id")
    }
    merged: list[dict[str, Any]] = []
    for row in rows:
        if row["id"] == chosen and row["id"] in parsed_by:
            full = parsed_by[row["id"]]
            if activity and (not full.get("updated") or activity > str(full.get("updated"))):
                full["updated"] = activity
            if title:
                full["title"] = title
            merged.append(full)
        elif chosen and str(row.get("parent_id") or "") == chosen:
            merged.append(_public_row(row))
        elif row.get("is_root"):
            merged.append(_public_row(row))
    snap = {
        "ok": True,
        "source": "codex",
        "metric": _METRIC,
        "note": "",
        "scan_ms": int(parsed.get("scan_ms") or 0) if isinstance(parsed, dict) else 0,
        "files": parsed.get("files") if isinstance(parsed, dict) else {},
        "totals": parsed.get("totals") if isinstance(parsed, dict) else _public(_blank()),
        "by_day": [],
        "by_model": [],
        "threads": merged,
        "n_calls": int(parsed.get("n_calls") or 0) if isinstance(parsed, dict) else 0,
        "n_threads": len(merged),
        "children_loaded": not child_paths,
    }
    with _lock:
        _focus_cache[home_key] = (cache_key, snap)
    if child_paths:
        _kick_children(root, home_key, cache_key, chosen, child_paths)
    return snap


def focus_snapshot(home: Optional[Path] = None) -> dict[str, Any]:
    """Newest day only. Enough to paint the live session."""
    root = Path(home) if home is not None else codex_home()
    return build_snapshot(root, paths=newest_day_paths(root))


def cached_thread_ids(home: Optional[Path] = None) -> Optional[set[str]]:
    """Ids already in the snapshot cache. None if this home was never built."""
    root = Path(home) if home is not None else codex_home()
    key = str(root.resolve()) if root.exists() else str(root)
    with _lock:
        hit = _cache.get(key)
    if not hit:
        return None
    return {
        str(thread.get("id"))
        for thread in (hit[1].get("threads") or [])
        if isinstance(thread, dict) and thread.get("id")
    }


def snapshot_bytes(home: Optional[Path] = None) -> tuple[bytes, str]:
    _payload, body, etag = _load(home)
    return body, etag


def thread_detail(thread_id: str, home: Optional[Path] = None) -> dict[str, Any]:
    payload, _body, _etag = _load(home)
    for thread in payload.get("threads") or []:
        if thread.get("id") == thread_id:
            return {
                "ok": True,
                "source": "codex",
                "id": thread_id,
                "calls": thread.get("calls") or [],
            }
    return {"ok": False, "source": "codex", "error": "thread not found", "calls": []}
