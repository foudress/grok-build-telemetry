"""Session bootstrap / chat_history loaders (R1 freeze zone — move only)."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from token_telemetry.tokenizer import (
    count_chars_as_tokens,
    count_tokens,
    tokenizer_mode,
)

from token_telemetry.hierarchy.text_metrics import (
    _msg_text,
    _preview,
    _scale_chars_to_tokens,
)


def _classify_bootstrap_message(role: str, text: str, syn: Any) -> Optional[str]:
    """Map a chat_history message to system-card / user buckets."""
    if role in ("system",):
        return "system"
    if role in ("reasoning", "assistant", "tool_result", "function_call"):
        return None
    if "<user_info>" in text:
        return "user_info"
    if syn == "system_reminder" or text.lstrip().startswith("<system-reminder>"):
        low = text.lower()
        if "skills are available" in low:
            return "reminders"
        if "mcp servers" in low or "mcp server" in low:
            return "mcp"
        return "reminders"
    if "<user_query>" in text or "<skill_information>" in text:
        return "user_prompt"
    return "other"

def load_chat_history_tool_results(
    session_dir: Optional[Path],
) -> dict[str, dict[str, Any]]:
    """
    Map tool_call_id → tokenized tool_result.content from chat_history.jsonl.

    This is the assistant-facing result body (closer to model In than rawOutput dump).
    """
    out: dict[str, dict[str, Any]] = {}
    if session_dir is None:
        return out
    ch_path = Path(session_dir) / "chat_history.jsonl"
    if not ch_path.is_file():
        return out
    try:
        with ch_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                role = str(o.get("type") or o.get("role") or "")
                if role != "tool_result":
                    continue
                tid = o.get("tool_call_id") or o.get("toolCallId")
                if not tid:
                    continue
                content = o.get("content")
                if isinstance(content, str):
                    text = content
                elif content is None:
                    text = ""
                else:
                    try:
                        text = json.dumps(content, ensure_ascii=False)
                    except (TypeError, ValueError):
                        text = str(content)
                chars = len(text)
                tok = count_tokens(text) if text else 0
                # Keep max if duplicate ids (shouldn't happen)
                prev = out.get(str(tid))
                if prev and int(prev.get("content_tokens") or 0) >= tok:
                    continue
                out[str(tid)] = {
                    "tool_call_id": str(tid),
                    "content_chars": chars,
                    "content_tokens": int(tok),
                    "preview": _preview(text, 80) if text else "",
                }
    except OSError:
        return out
    return out


def load_chat_history_reasonings(session_dir: Optional[Path]) -> list[dict[str, Any]]:
    """
    Ordered reasoning items from chat_history.jsonl (full JSON incl. encrypted_content).

    Each entry maps ~1:1 to a model step (reasoning before assistant/tools).
    """
    out: list[dict[str, Any]] = []
    if session_dir is None:
        return out
    ch_path = Path(session_dir) / "chat_history.jsonl"
    if not ch_path.is_file():
        return out
    try:
        with ch_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if (o.get("type") or o.get("role")) != "reasoning":
                    continue
                enc = o.get("encrypted_content") or ""
                enc_chars = len(enc) if isinstance(enc, str) else len(
                    json.dumps(enc, ensure_ascii=False)
                )
                summary = o.get("summary") or []
                sum_text = ""
                if isinstance(summary, list):
                    for s in summary:
                        if isinstance(s, dict):
                            sum_text += str(s.get("text") or "")
                        elif isinstance(s, str):
                            sum_text += s
                elif isinstance(summary, str):
                    sum_text = summary
                try:
                    full_chars = len(json.dumps(o, ensure_ascii=False))
                except (TypeError, ValueError):
                    full_chars = enc_chars + len(sum_text)
                sum_tok = count_tokens(sum_text) if sum_text else 0
                # Encrypted blob is opaque; BPE still better relative weight than //4
                enc_tok = (
                    count_tokens(enc)
                    if isinstance(enc, str) and enc
                    else count_chars_as_tokens(enc_chars)
                )
                out.append(
                    {
                        "full_chars": full_chars,
                        "encrypted_chars": enc_chars,
                        "encrypted_tokens": int(enc_tok),
                        "summary_chars": len(sum_text),
                        "summary_text": sum_text,
                        "summary_tokens": int(sum_tok),
                        "preview": _preview(sum_text, 80) if sum_text else "",
                    }
                )
    except OSError:
        return out
    return out


def parse_session_bootstrap(
    session_dir: Optional[Path],
    *,
    target_tokens: Optional[int] = None,
    hooks: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    """
    Build first-prompt bootstrap from chat_history.jsonl.

    Hooks are intentionally ignored: hook_execution payloads are not part of the
    model prompt / API input. The ``hooks`` arg is accepted for call-site
    compatibility only.

    Heuristic: tokenize each JSON message (xai-token-estimation bytes/4), then
    scale group totals so system-card + user_prompt == target_tokens (context_start).
    """
    empty = {
        "kind": "session_bootstrap",
        "parts": [],
        "system_tokens": 0,
        "user_tokens": 0,
        "total_tokens": 0,
        "user_preview": "",
        "user_chars": 0,
        "source": None,
    }
    if session_dir is None:
        return empty
    ch_path = Path(session_dir) / "chat_history.jsonl"
    if not ch_path.is_file():
        return empty

    buckets: dict[str, dict[str, Any]] = {}

    def add(
        kind: str,
        chars: int,
        preview: str = "",
        extra: Optional[dict] = None,
        *,
        tokens: Optional[int] = None,
    ) -> None:
        if kind not in buckets:
            buckets[kind] = {
                "kind": kind,
                "chars": 0,
                "tok_w": 0,
                "preview": preview,
                "messages": 0,
            }
            if extra:
                buckets[kind].update(extra)
        buckets[kind]["chars"] += max(0, int(chars))
        tw = int(tokens) if tokens is not None else count_chars_as_tokens(chars)
        buckets[kind]["tok_w"] = int(buckets[kind].get("tok_w") or 0) + max(0, tw)
        buckets[kind]["messages"] = int(buckets[kind].get("messages") or 0) + 1
        if preview and not buckets[kind].get("preview"):
            buckets[kind]["preview"] = preview

    user_preview = ""
    user_query_chars = 0
    skill_chars = 0

    try:
        with ch_path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                role = str(o.get("type") or o.get("role") or "")
                if role == "reasoning":
                    break
                text = _msg_text(o.get("content"))
                syn = o.get("synthetic_reason")
                kind = _classify_bootstrap_message(role, text, syn)
                if not kind:
                    continue
                full_json = json.dumps(o, ensure_ascii=False)
                full_chars = len(full_json)
                full_tok = count_tokens(full_json)
                prev = _preview(text, 80)
                if kind == "user_prompt":
                    # Split user_query vs skill_information inside the same JSON
                    uq = re.search(
                        r"<user_query>(.*?)</user_query>", text, re.DOTALL | re.IGNORECASE
                    )
                    sk = re.search(
                        r"<skill_information>(.*?)</skill_information>",
                        text,
                        re.DOTALL | re.IGNORECASE,
                    )
                    uq_c = len(uq.group(0)) if uq else 0
                    sk_c = len(sk.group(0)) if sk else 0
                    uq_t = count_tokens(uq.group(0)) if uq else 0
                    sk_t = count_tokens(sk.group(0)) if sk else 0
                    # Attribute full JSON tokens proportionally to tag token weights
                    tag_sum = uq_t + sk_t
                    if tag_sum > 0:
                        user_query_chars += int(round(full_chars * uq_t / tag_sum))
                        skill_chars += full_chars - int(round(full_chars * uq_t / tag_sum))
                    elif uq_c + sk_c > 0:
                        user_query_chars += int(round(full_chars * uq_c / (uq_c + sk_c)))
                        skill_chars += full_chars - int(
                            round(full_chars * uq_c / (uq_c + sk_c))
                        )
                    else:
                        user_query_chars += full_chars
                    if uq and not user_preview:
                        user_preview = _preview(uq.group(1).strip(), 80)
                    add(
                        "user_prompt",
                        full_chars,
                        user_preview or prev,
                        tokens=full_tok,
                    )
                else:
                    add(kind, full_chars, prev, tokens=full_tok)
    except OSError:
        return empty

    # Hooks are NOT part of the prompt — never add to system card tokens.
    _ = hooks  # call-site may still pass bootstrap hooks; ignore

    # System-card kinds (everything before the indexed user prompt)
    system_order = ["system", "user_info", "reminders", "mcp", "other"]
    sys_chars = sum(int(buckets[k]["chars"]) for k in system_order if k in buckets)
    user_chars = int(buckets.get("user_prompt", {}).get("chars") or 0)
    sys_w = sum(int(buckets[k].get("tok_w") or 0) for k in system_order if k in buckets)
    user_w = int(buckets.get("user_prompt", {}).get("tok_w") or 0)
    # Fallback to chars if tok_w missing
    if sys_w + user_w <= 0:
        sys_w, user_w = sys_chars, user_chars
    total_chars = sys_chars + user_chars
    if total_chars <= 0 and sys_w + user_w <= 0:
        return empty

    tgt = (
        int(target_tokens)
        if target_tokens and target_tokens > 0
        else max(1, int(sys_w + user_w) or count_chars_as_tokens(total_chars))
    )
    # Two-group split first (system vs user), then subdivide system parts
    # by tokenizer weights (not raw chars)
    group_tok = _scale_chars_to_tokens([max(1, sys_w), max(0, user_w) or 0], tgt)
    sys_tok_total, user_tok_total = group_tok[0], group_tok[1]

    sys_kinds = [
        k
        for k in system_order
        if k in buckets and (buckets[k]["chars"] > 0 or buckets[k].get("tok_w"))
    ]
    sys_weights = [
        int(buckets[k].get("tok_w") or buckets[k]["chars"] or 0) for k in sys_kinds
    ]
    sys_parts_tok = _scale_chars_to_tokens(sys_weights, sys_tok_total)

    labels = {
        "system": "System",
        "user_info": "User info",
        "reminders": "Reminders / skills catalog",
        "mcp": "MCP",
        "message": "Message",
        "other": "Other",
        "user_prompt": "User prompt",
        "tool_definitions": "Tool definitions",
    }
    parts: list[dict[str, Any]] = []
    for k, tok in zip(sys_kinds, sys_parts_tok):
        b = buckets[k]
        parts.append(
            {
                "kind": k,
                "label": labels.get(k, k),
                "chars": int(b["chars"]),
                "tokens": int(tok),
                # tokZ = raw tokenizer weight before pro-rata scale to stream start
                "tokenizer_tokens": int(
                    b.get("tok_w") or count_chars_as_tokens(int(b["chars"])) or tok
                ),
                "preview": b.get("preview") or "",
                "messages": b.get("messages"),
                "runs": b.get("runs"),
            }
        )

    # Optional detail inside user prompt (tokenized weights)
    user_detail = None
    if user_chars > 0 and (user_query_chars or skill_chars):
        uq_w = count_chars_as_tokens(user_query_chars) or max(1, user_query_chars)
        sk_w = count_chars_as_tokens(skill_chars) if skill_chars else 0
        ud = _scale_chars_to_tokens([max(1, uq_w), max(0, sk_w)], user_tok_total)
        user_detail = {
            "user_query_tokens": ud[0],
            "skill_information_tokens": ud[1] if len(ud) > 1 else 0,
            "user_query_chars": user_query_chars,
            "skill_information_chars": skill_chars,
        }

    return {
        "kind": "session_bootstrap",
        "parts": parts,
        "system_tokens": int(sys_tok_total),
        "user_tokens": int(user_tok_total),
        "total_tokens": int(tgt),
        "system_chars": int(sys_chars),
        "user_chars": int(user_chars),
        "user_preview": user_preview,
        "user_detail": user_detail,
        "source": "chat_history",
        "tokenizer": tokenizer_mode(),
        "note": (
            f"Tokenizer({tokenizer_mode()}) split: each chat_history JSON bracket "
            f"tokenized, pro-rata scaled to first stream context_start ({tgt}). "
            "System card = system+user_info+reminders+MCP "
            "(+ tool definitions added separately; hooks excluded — not in prompt); "
            "User prompt = prompt index 0 (user_query + skill_information)."
        ),
    }


# Host / MCP tool *schemas* are sent on the API tools channel — not as
# chat_history messages. Grok context budget reports them separately
# (e.g. "Tool definitions 8.2k · 25 tools"). Override via env if needed.
DEFAULT_TOOL_DEFINITION_TOKENS = 8200
DEFAULT_TOOL_DEFINITION_COUNT = 25


def resolve_tool_definitions(
    session_dir: Optional[Path] = None,
) -> dict[str, Any]:
    """
    Size of silent tool-definition payload (not in chat_history).

    Order:
      1. env GROK_TOOL_DEFINITION_TOKENS / GROK_TOOL_DEFINITION_COUNT
      2. session file tool_definitions.json {tokens, count}
      3. defaults from xAI context budget (8.2k / 25 tools)
    """
    import os

    tokens = DEFAULT_TOOL_DEFINITION_TOKENS
    count = DEFAULT_TOOL_DEFINITION_COUNT
    source = "default_xai_context_budget"

    env_t = os.environ.get("GROK_TOOL_DEFINITION_TOKENS")
    env_c = os.environ.get("GROK_TOOL_DEFINITION_COUNT")
    if env_t is not None:
        try:
            tokens = max(0, int(env_t))
            source = "env"
        except (TypeError, ValueError):
            pass
    if env_c is not None:
        try:
            count = max(0, int(env_c))
            if source != "env":
                source = "env"
        except (TypeError, ValueError):
            pass

    if session_dir is not None:
        path = Path(session_dir) / "tool_definitions.json"
        if path.is_file():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(raw, dict):
                    if raw.get("tokens") is not None:
                        tokens = max(0, int(raw["tokens"]))
                    if raw.get("count") is not None:
                        count = max(0, int(raw["count"]))
                    source = "session_tool_definitions.json"
            except (OSError, TypeError, ValueError, json.JSONDecodeError):
                pass

    return {
        "kind": "tool_definitions",
        "tokens": int(tokens),
        "count": int(count),
        "source": source,
        "note": (
            "API tools channel (host + MCP schemas). Absent from chat_history; "
            "counted in real prompt input and cache prefix from call 1."
        ),
    }


def inject_tool_definitions_into_bootstrap(
    boot: dict[str, Any],
    tool_defs: dict[str, Any],
) -> dict[str, Any]:
    """Append Tool definitions as a System-card part (absolute tokens, not re-scaled)."""
    tok = int(tool_defs.get("tokens") or 0)
    if tok <= 0:
        return boot
    count = int(tool_defs.get("count") or 0)
    parts = list(boot.get("parts") or [])
    # Idempotent: replace existing tool_definitions part if re-finalized
    parts = [p for p in parts if (p.get("kind") != "tool_definitions")]
    parts.append(
        {
            "kind": "tool_definitions",
            "label": "Tool definitions",
            "chars": 0,
            "tokens": tok,
            "tokenizer_tokens": tok,
            "messages": 0,
            "tool_count": count,
            "preview": (
                f"{count} tools · silent API channel (not in chat_history)"
                if count
                else "silent API channel (not in chat_history)"
            ),
            "source": tool_defs.get("source"),
        }
    )
    sys_tok = int(boot.get("system_tokens") or 0) + tok
    user_tok = int(boot.get("user_tokens") or 0)
    boot = dict(boot)
    boot["parts"] = parts
    boot["system_tokens"] = sys_tok
    boot["total_tokens"] = sys_tok + user_tok
    boot["tool_definitions_tokens"] = tok
    boot["tool_definitions_count"] = count
    boot["source"] = (boot.get("source") or "chat_history") + "+tool_definitions"
    boot["note"] = (
        f"{boot.get('note') or ''} "
        f"Tool definitions +{tok} tok ({count} tools, {tool_defs.get('source')}) "
        f"added to System; not scaled from history."
    ).strip()
    return boot
