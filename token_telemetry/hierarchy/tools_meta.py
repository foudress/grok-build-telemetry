"""Tool name, plan, arg/content metrics helpers."""

from __future__ import annotations

import json
from typing import Any, Optional

from token_telemetry.tokenizer import (
    count_chars_as_tokens,
    count_json_tokens,
    count_tokens,
)

from token_telemetry.hierarchy.text_metrics import _json_len, _preview


_KNOWN_TOOLS = frozenset(
    {
        "read_file",
        "grep",
        "write",
        "search_replace",
        "run_terminal_command",
        "list_dir",
        "web_search",
        "todo_write",
        "open_page",
        "web_fetch",
        "spawn_subagent",
        "get_command_or_subagent_output",
        "kill_command_or_subagent",
        "ask_user_question",
        "image_gen",
        "search_tool",
        "use_tool",
    }
)


def _tool_name(update: dict[str, Any], update_meta: dict[str, Any]) -> Optional[str]:
    xai = update_meta.get("x.ai/tool") if isinstance(update_meta, dict) else None
    if isinstance(xai, dict) and xai.get("name"):
        return str(xai["name"])
    title = update.get("title")
    if isinstance(title, str) and title:
        if title in _KNOWN_TOOLS:
            return title
        if title.startswith("Read `") or title.startswith("Read "):
            return "read_file"
        if title.startswith("Execute `") or title.startswith("Execute "):
            return "run_terminal_command"
        if title.startswith("Write `") or title.startswith("Write "):
            return "write"
        if title.startswith("Updating plan") or title.startswith("Todo"):
            return "todo_write"
        if title.startswith("Search") or "grep" in title.lower():
            return "grep"
        if title.startswith("List ") or title.startswith("List `"):
            return "list_dir"
    kind = update.get("kind")
    if isinstance(kind, str) and kind in ("read", "edit", "search", "execute"):
        return {
            "read": "read_file",
            "edit": "search_replace",
            "search": "grep",
            "execute": "run_terminal_command",
        }.get(kind, kind)
    return None


def _short_title(title: Optional[str], n: int = 72) -> Optional[str]:
    if not title:
        return None
    t = " ".join(str(title).split())
    if len(t) <= n:
        return t
    return t[: n - 1] + "…"


def _tool_seq_from_id(tid: Optional[str]) -> Optional[int]:
    """
    Last numeric segment of a toolCallId → per-round tool index.

    e.g. call-54ce03e3-20d3-4c79-97a0-e07ff5da8f6f-27 → 27
    """
    if not tid:
        return None
    tail = str(tid).rsplit("-", 1)[-1]
    if tail.isdigit():
        return int(tail)
    return None


def _is_plan_tool(name: Optional[str], title: Optional[str] = None) -> bool:
    """Strict: only todo_write / plan UI titles — never match grep etc."""
    n = (name or "").lower().strip()
    t = (title or "").lower().strip()
    if n in ("todo_write", "todo", "todowrite"):
        return True
    if t in ("todo_write", "updating plan") or t.startswith("updating plan"):
        return True
    return False


def _plan_steps_from_todos(todos: Any) -> list[dict[str, Any]]:
    """Normalize todo list → [{n, id, status, content}]."""
    out: list[dict[str, Any]] = []
    if not isinstance(todos, list):
        return out
    for i, item in enumerate(todos):
        if not isinstance(item, dict):
            continue
        raw_id = item.get("id")
        try:
            n = int(raw_id) if raw_id is not None and str(raw_id).isdigit() else i + 1
        except (TypeError, ValueError):
            n = i + 1
        st = str(item.get("status") or "pending").lower().replace(" ", "_")
        if st in ("complete", "done", "finished"):
            st = "completed"
        elif st in ("in-progress", "running", "active"):
            st = "in_progress"
        elif st in ("todo", "open", "not_started"):
            st = "pending"
        out.append(
            {
                "n": n,
                "id": str(raw_id) if raw_id is not None else str(n),
                "status": st,
                "content": str(item.get("content") or item.get("description") or "")[:80],
            }
        )
    return out


def _extract_plan_meta(
    *,
    name: Optional[str],
    title: Optional[str],
    raw_in: Any,
    raw_out: Any = None,
) -> Optional[dict[str, Any]]:
    """
    Plan (todo_write) display meta for LLM Out + Harness.

    - mode create: first full plan write (merge false / no merge)
    - mode modify: updating plan (merge true or title Updating plan)
    - steps: current todo statuses for harness colored numbers
    """
    if not _is_plan_tool(name, title):
        return None
    merge = None
    todos_in: Any = None
    if isinstance(raw_in, dict):
        if "merge" in raw_in:
            merge = bool(raw_in.get("merge"))
        todos_in = raw_in.get("todos")
    title_s = str(title or "")
    # create vs modify
    if merge is True or "updating plan" in title_s.lower():
        mode = "modify"
    elif merge is False:
        mode = "create"
    else:
        mode = "modify" if todos_in else "create"

    steps = _plan_steps_from_todos(todos_in)
    # Prefer completed result state when present (full plan snapshot)
    if isinstance(raw_out, dict):
        tu = raw_out.get("TodosUpdated") if raw_out.get("type") == "Todo" or "TodosUpdated" in raw_out else raw_out.get("TodosUpdated")
        if isinstance(tu, dict):
            steps_out = _plan_steps_from_todos(tu.get("todos"))
            if steps_out:
                steps = steps_out
            # state may hold full list
            state = tu.get("state")
            if isinstance(state, dict) and not steps_out:
                st_todos = state.get("todos") or state.get("items")
                steps_out = _plan_steps_from_todos(st_todos)
                if steps_out:
                    steps = steps_out

    n = len(steps) if steps else (
        len(todos_in) if isinstance(todos_in, list) else 0
    )
    return {
        "is_plan": True,
        "mode": mode,  # create | modify
        "step_count": int(n),
        "steps": steps,
        "merge": merge,
    }

def _arg_metrics(raw_in: Any) -> dict[str, Any]:
    """Size of tool *arguments* (emit side). Critical for search_replace old/new."""
    if raw_in is None:
        return {"arg_chars": 0, "arg_tokens_est": 0}
    if isinstance(raw_in, str):
        n = len(raw_in)
        return {"arg_chars": n, "arg_tokens_est": count_tokens(raw_in)}
    if not isinstance(raw_in, dict):
        try:
            s = json.dumps(raw_in, ensure_ascii=False)
        except (TypeError, ValueError):
            s = str(raw_in)
        n = len(s)
        return {"arg_chars": n, "arg_tokens_est": count_tokens(s)}

    # Full JSON arg object (keys + glue). Prefer that over bare text fields so
    # short tools still count type/name structure; take max with big text fields
    # (search_replace old/new) so we never undercount edits.
    try:
        json_s = json.dumps(raw_in, ensure_ascii=False)
        json_n = len(json_s)
        json_tok = count_tokens(json_s)
    except (TypeError, ValueError):
        json_s = str(raw_in)
        json_n = len(json_s)
        json_tok = count_tokens(json_s)
    text_n = 0
    text_tok = 0
    for key in (
        "old_string",
        "new_string",
        "contents",
        "content",
        "command",
        "query",
        "pattern",
        "prompt",
        "text",
        "diff",
    ):
        v = raw_in.get(key)
        if isinstance(v, str) and v:
            text_n += len(v)
            text_tok += count_tokens(v)
    n = max(json_n, text_n)
    return {"arg_chars": n, "arg_tokens_est": max(json_tok, text_tok)}

def _wire_content_text_parts(content: Any) -> list[str]:
    """
    Model-facing text fragments from stream tool_call_update.content.

    Skip UI-only `type: diff` blocks (oldText/newText). Those are the edit
    payload for the TUI, not the tool_result the model re-reads — using them
    as harness In weights inflated search_replace to multi-k tokZ.
    Prefer type:content text (errors, shell output, confirmations).
    """
    parts: list[str] = []
    if content is None:
        return parts
    if isinstance(content, str):
        if content.strip():
            parts.append(content)
        return parts
    if isinstance(content, dict):
        t = content.get("text") or content.get("content")
        if isinstance(t, str) and t.strip():
            parts.append(t)
        elif isinstance(t, dict):
            inner = t.get("text") or t.get("content")
            if isinstance(inner, str) and inner.strip():
                parts.append(inner)
        return parts
    if not isinstance(content, list):
        return parts
    for item in content:
        if not isinstance(item, dict):
            if isinstance(item, str) and item.strip():
                parts.append(item)
            continue
        kind = str(item.get("type") or "")
        # UI diff only — never count as tool result body
        if kind == "diff" or item.get("oldText") is not None or item.get("newText") is not None:
            continue
        if kind in ("content", "text", "") or "text" in item or "content" in item:
            t = item.get("text")
            if t is None:
                t = item.get("content")
            if isinstance(t, str) and t.strip():
                parts.append(t)
            elif isinstance(t, dict):
                inner = t.get("text") or t.get("content")
                if isinstance(inner, str) and inner.strip():
                    parts.append(inner)
    return parts


def _search_replace_prompt_output(raw_out: dict[str, Any]) -> Optional[str]:
    """
    Confirmation / error string the model sees for SearchReplace / Write.

    Prefer tool_output_for_prompt (matches chat_history tool_result.content).
    Never return old_string/new_string/edits (those are request-side mass).
    """
    ea = raw_out.get("EditsApplied")
    if isinstance(ea, dict):
        for key in ("tool_output_for_prompt", "tool_output_for_prompt_concise"):
            v = ea.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip()
        path = ea.get("absolute_path") or ea.get("file_path") or ea.get("path") or ""
        if isinstance(path, str) and path.strip():
            # Match chat_history phrasing when prompt fields missing
            if ea.get("old_string") in ("", None) and ea.get("new_string"):
                return f"The file {path} has been created."
            return f"The file {path} has been updated successfully."
        return "updated successfully"
    nm = raw_out.get("NoMatchesFound")
    if isinstance(nm, dict):
        msg = nm.get("message")
        if isinstance(msg, str) and msg.strip():
            return msg.strip()
        return "The string to replace was not found in the file."
    # Other edit outcomes (rare)
    for key in ("Error", "FileNotFound", "message"):
        v = raw_out.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip()
        if isinstance(v, dict):
            m = v.get("message") or v.get("error")
            if isinstance(m, str) and m.strip():
                return m.strip()
    return None


def _primary_raw_payload(raw_out: Any) -> Any:
    """
    Single primary body from tool rawOutput — never the full debug blob.

    Grok rawOutput often duplicates the same text (ReadFile: content +
    content_concise + raw_output; Bash: output + output_for_prompt + deltas).
    Counting json.dumps(rawOutput) therefore multiplies size ~2–6×.
    Prefer the field closest to what the model sees, once.
    """
    if raw_out is None:
        return None
    if isinstance(raw_out, str):
        return raw_out
    if not isinstance(raw_out, dict):
        return raw_out

    t = str(raw_out.get("type") or "")

    # search_replace / write-via-edit: short confirmation only (not EditsApplied blob)
    if (
        t == "SearchReplace"
        or raw_out.get("EditsApplied") is not None
        or raw_out.get("NoMatchesFound") is not None
    ):
        return _search_replace_prompt_output(raw_out)

    # read_file: FileContent.content (line-numbered) once
    fc = raw_out.get("FileContent")
    if t == "ReadFile" or isinstance(fc, dict):
        if isinstance(fc, dict):
            for key in ("content", "raw_output", "content_concise"):
                v = fc.get(key)
                if isinstance(v, str) and v:
                    return v
        fnf = raw_out.get("FileNotFound")
        if isinstance(fnf, str) and fnf:
            return fnf
        return None

    # shell: prompt-facing string wins over chunk lists / command echo
    if t == "Bash" or "output_for_prompt" in raw_out or (
        "output" in raw_out and "command" in raw_out
    ):
        ofp = raw_out.get("output_for_prompt")
        if isinstance(ofp, str) and ofp.strip():
            return ofp
        out = raw_out.get("output")
        if isinstance(out, str) and out:
            return out
        if isinstance(out, list) and out:
            return out
        return ofp if isinstance(ofp, str) else None

    # list_dir
    content_block = raw_out.get("Content")
    if t == "ListDir" or isinstance(content_block, dict):
        if isinstance(content_block, dict):
            c = content_block.get("content")
            if isinstance(c, str) and c:
                return c
        return None

    # todo write
    todos = raw_out.get("TodosUpdated")
    if t == "Todo" or isinstance(todos, dict):
        if isinstance(todos, dict):
            s = todos.get("summary_for_prompt")
            if isinstance(s, str) and s:
                return s
            # compact: summary missing → todos list once (not full state duplicate)
            if todos.get("todos") is not None:
                return todos.get("todos")
        return None

    # MCP tool result
    if t == "MCP" or raw_out.get("server_name") is not None:
        out = raw_out.get("output")
        if out is not None:
            return out
        return None

    # search_tool / generic content string
    c = raw_out.get("content")
    if isinstance(c, str) and c:
        return c

    # grep: structured matches; avoid double-count stdout + file_matches
    if t == "GrepSearch" or raw_out.get("file_matches") is not None:
        fm = raw_out.get("file_matches")
        if fm:
            return fm
        stdout = raw_out.get("stdout")
        if stdout is not None:
            return stdout
        return None

    # Generic fallback: first substantial known field (still not full dump)
    # Never pull old_string/new_string/edits (request-side / UI edit payload).
    for key in (
        "tool_output_for_prompt",
        "output_for_prompt",
        "summary_for_prompt",
        "content",
        "output",
        "stdout",
        "text",
        "result",
        "message",
    ):
        v = raw_out.get(key)
        if isinstance(v, str) and v.strip():
            return v
        if isinstance(v, (list, dict)) and v:
            return v
    return None


def _raw_meta_path_fields(raw_out: Any) -> dict[str, Any]:
    """Pull path/offset/limit from typed rawOutput when rawInput is empty."""
    out: dict[str, Any] = {}
    if not isinstance(raw_out, dict):
        return out
    fc = raw_out.get("FileContent")
    if isinstance(fc, dict):
        if fc.get("absolute_path"):
            out["path"] = fc.get("absolute_path")
        if fc.get("offset") is not None:
            out["offset"] = fc.get("offset")
        if fc.get("limit") is not None:
            out["limit"] = fc.get("limit")
    content_block = raw_out.get("Content")
    if isinstance(content_block, dict) and content_block.get("absolute_root_path"):
        out.setdefault("path", content_block.get("absolute_root_path"))
    return out


def _content_metrics(update: dict[str, Any]) -> dict[str, Any]:
    """
    Extract result size hints from tool_call_update payloads.

    Weight by the **model-facing tool_result body**, not:
      • UI `type:diff` (oldText/newText) on stream content
      • SearchReplace EditsApplied.old/new_string blobs
      • full rawOutput debug dumps (ReadFile ×3, Bash ×2–6)

    Prefer: chat_history tool_result.content (stamped later) >
            stream type:content text >
            rawOutput tool_output_for_prompt / primary field.
    """
    text_chars = 0
    lines = 0
    preview = None
    content = update.get("content")

    def absorb_text(s: str) -> None:
        nonlocal text_chars, lines, preview
        if not s:
            return
        text_chars += len(s)
        lines += s.count("\n") + (1 if s else 0)
        if preview is None:
            preview = _preview(s, 80)

    # Model-facing text only (skips UI diffs)
    wire_parts = _wire_content_text_parts(content)
    for part in wire_parts:
        absorb_text(part)
    content_text = "\n".join(wire_parts) if wire_parts else ""
    content_json_chars = len(content_text)

    raw_out = update.get("rawOutput")
    primary = _primary_raw_payload(raw_out)
    primary_chars = _json_len(primary) if primary is not None else 0
    # Debug-only: full raw dump size (never used for result_chars / pro-rata)
    raw_dump_chars = 0
    if raw_out is not None:
        raw_dump_chars = _json_len(raw_out)

    # If stream content empty (e.g. search_replace only sent UI diff), use
    # primary prompt-facing string (confirmation / error / file body).
    if text_chars == 0 and primary is not None:
        if isinstance(primary, str):
            absorb_text(primary)
        else:
            try:
                absorb_text(json.dumps(primary, ensure_ascii=False))
            except (TypeError, ValueError):
                absorb_text(str(primary))

    # Body for envelope / token weights: wire text, else primary — never raw diffs
    tid = update.get("toolCallId") or update.get("tool_call_id") or ""
    if content_text:
        body: Any = content_text
    elif primary is not None:
        body = primary
    else:
        body = ""
    try:
        envelope = {
            "type": "tool_result",
            "tool_call_id": tid,
            "content": body,
        }
        envelope_chars = len(json.dumps(envelope, ensure_ascii=False))
    except (TypeError, ValueError):
        envelope_chars = content_json_chars or primary_chars or text_chars

    # Wire size: envelope / bare text / primary. Never max with raw_dump or UI diff.
    chars = max(envelope_chars, content_json_chars, text_chars, primary_chars if body else 0)
    if chars > 0 and lines <= 0:
        lines = max(1, (text_chars.count("\n") + 1) if text_chars else 1)

    raw_in = update.get("rawInput") or {}
    path = None
    offset = None
    limit = None
    if isinstance(raw_in, dict):
        path = raw_in.get("target_file") or raw_in.get("path") or raw_in.get("file_path")
        offset = raw_in.get("offset")
        limit = raw_in.get("limit")
    um = update.get("_meta") or {}
    xai = um.get("x.ai/tool") if isinstance(um, dict) else None
    if isinstance(xai, dict):
        inp = xai.get("input") or {}
        if isinstance(inp, dict):
            path = path or inp.get("path") or inp.get("target_file") or inp.get("file_path")
            offset = offset if offset is not None else inp.get("offset")
            limit = limit if limit is not None else inp.get("limit")
            if not raw_in:
                raw_in = inp
    meta_path = _raw_meta_path_fields(raw_out)
    path = path or meta_path.get("path")
    if offset is None:
        offset = meta_path.get("offset")
    if limit is None:
        limit = meta_path.get("limit")
    # path from SearchReplace EditsApplied
    if not path and isinstance(raw_out, dict):
        ea = raw_out.get("EditsApplied")
        if isinstance(ea, dict):
            path = ea.get("absolute_path") or ea.get("file_path") or path

    # Tokenizer estimate from wire envelope (same body as chars)
    if chars and body != "":
        try:
            tokens_est = count_json_tokens(
                {
                    "type": "tool_result",
                    "tool_call_id": tid,
                    "content": body,
                }
            )
        except Exception:
            tokens_est = count_chars_as_tokens(chars)
    elif chars:
        tokens_est = count_chars_as_tokens(chars)
    else:
        tokens_est = 0
    # Status-only results ("ok", "updated") are tiny — keep est low; tt Δ wins in weights
    return {
        "result_chars": chars,
        "result_text_chars": text_chars,
        "result_lines": lines if chars else 0,
        "result_tokens_est": tokens_est,
        "result_preview": preview,
        "path": path,
        "offset": offset,
        "limit": limit,
        "raw_output_chars": primary_chars,
        "raw_output_dump_chars": raw_dump_chars,
        "envelope_chars": envelope_chars,
        **_arg_metrics(raw_in if isinstance(raw_in, (dict, str)) else None),
    }
