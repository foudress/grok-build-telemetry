"""Pure text / size helpers for hierarchy reconstruction."""

from __future__ import annotations

import json
from typing import Any

from token_telemetry.tokenizer import scale_weights_to_target


def _scale_chars_to_tokens(weights: list[int], target: int) -> list[int]:
    """Distribute target tokens proportional to weights (exact sum).

    Weights may be char lengths or pre-tokenized counts — we only need relative
    mass. Prefer scale_weights_to_target when available.
    """
    if not weights:
        return []
    try:
        return scale_weights_to_target([float(max(0, int(w))) for w in weights], int(target))
    except Exception:
        pass
    target = max(0, int(target))
    if target == 0:
        return [0] * len(weights)
    s = sum(max(0, int(w)) for w in weights)
    if s <= 0:
        base = target // len(weights)
        out = [base] * len(weights)
        for i in range(target - base * len(weights)):
            out[i] += 1
        return out
    floats = [max(0, int(w)) / s * target for w in weights]
    ints = [int(x) for x in floats]
    rem = target - sum(ints)
    order = sorted(
        range(len(floats)),
        key=lambda i: floats[i] - ints[i],
        reverse=True,
    )
    for i in order:
        if rem <= 0:
            break
        ints[i] += 1
        rem -= 1
    return ints


def _msg_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for c in content:
            if isinstance(c, dict):
                parts.append(str(c.get("text") or c.get("content") or ""))
            else:
                parts.append(str(c))
        return "".join(parts)
    if content is None:
        return ""
    try:
        return json.dumps(content, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(content)

def _text_of(update: dict[str, Any]) -> str:
    content = update.get("content") or {}
    if isinstance(content, dict):
        return content.get("text") or ""
    if isinstance(content, str):
        return content
    return ""


def _extract_recap_prompt_text(chat_history: Any) -> str:
    """Last user message that holds the recap system-reminder (fork prompt)."""
    if not isinstance(chat_history, list):
        return ""
    for m in reversed(chat_history):
        if not isinstance(m, dict):
            continue
        mtype = str(m.get("type") or m.get("role") or "").lower()
        if mtype not in ("user", "system"):
            continue
        content = m.get("content")
        if isinstance(content, list):
            parts: list[str] = []
            for x in content:
                if isinstance(x, dict):
                    t = x.get("text") or x.get("content")
                    if t:
                        parts.append(str(t))
                elif x is not None:
                    parts.append(str(x))
            text = "\n".join(parts)
        elif isinstance(content, str):
            text = content
        else:
            text = str(content or "")
        low = text.lower()
        if "recap" in low or "system-reminder" in low or "returning from idle" in low:
            return text
    return ""


def _preview(text: str, n: int = 100) -> str:
    t = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(t) <= n:
        return t
    return t[:n] + "…"

def _json_len(obj: Any) -> int:
    if obj is None:
        return 0
    if isinstance(obj, str):
        return len(obj)
    try:
        return len(json.dumps(obj, ensure_ascii=False))
    except (TypeError, ValueError):
        return len(str(obj))

def json_dumps_len(obj: Any) -> str:
    try:
        return json.dumps(obj, ensure_ascii=False)
    except (TypeError, ValueError):
        return str(obj)

# Keep only recent rounds in RAM (dashboard only needs a sliding window)
MAX_ROUNDS_RETAINED = 24
_PREVIEW_MAX = 80


def _clip_str(s: Any, n: int = _PREVIEW_MAX) -> Any:
    if not isinstance(s, str):
        return s
    return s if len(s) <= n else s[: n - 1] + "…"
