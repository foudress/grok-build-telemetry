"""Protobuf wire reader for Antigravity conversation blobs.

Token counts, model name, step time, and short labels. Previews are clipped.
Full tool results and file bodies stay in the blob.
"""

from __future__ import annotations

from typing import Iterator, Optional


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    n = 0
    shift = 0
    while i < len(buf) and shift <= 63:
        b = buf[i]
        i += 1
        n |= (b & 0x7F) << shift
        if not b & 0x80:
            return n, i
        shift += 7
    raise ValueError("bad varint")


def iter_fields(buf: bytes) -> Iterator[tuple[int, int, object]]:
    """(field number, wire type, value). Value is int or bytes."""
    i = 0
    try:
        while i < len(buf):
            key, i = _varint(buf, i)
            number, wire = key >> 3, key & 7
            if number == 0 or number > 50_000:
                return
            if wire == 0:
                val, i = _varint(buf, i)
            elif wire == 2:
                length, i = _varint(buf, i)
                if length < 0 or i + length > len(buf):
                    return
                val = buf[i : i + length]
                i += length
            elif wire == 1:
                if i + 8 > len(buf):
                    return
                val = buf[i : i + 8]
                i += 8
            elif wire == 5:
                if i + 4 > len(buf):
                    return
                val = buf[i : i + 4]
                i += 4
            else:
                return
            yield number, wire, val
    except ValueError:
        return


def _text(blob: object) -> str:
    if not isinstance(blob, (bytes, bytearray)) or not blob:
        return ""
    if any(b < 32 or b > 126 for b in blob):
        return ""
    return blob.decode("ascii")


def _varint_map(buf: bytes) -> dict[int, int]:
    out: dict[int, int] = {}
    for number, wire, val in iter_fields(buf):
        if wire == 0 and isinstance(val, int) and number not in out:
            out[number] = val
    return out


def _sub(buf: bytes, number: int) -> Optional[bytes]:
    for fn, wire, val in iter_fields(buf):
        if fn == number and wire == 2 and isinstance(val, (bytes, bytearray)):
            return bytes(val)
    return None


def _string_field(buf: bytes, number: int) -> str:
    for fn, wire, val in iter_fields(buf):
        if fn == number and wire == 2:
            text = _text(val)
            if text:
                return text
    return ""


def timestamp_epoch(buf: bytes) -> Optional[float]:
    """google.protobuf.Timestamp: field 1 seconds, field 2 nanos."""
    vals = _varint_map(buf)
    seconds = vals.get(1)
    if seconds is None or seconds <= 0:
        return None
    nanos = vals.get(2, 0)
    return float(seconds) + (nanos / 1_000_000_000.0)


def parse_usage(buf: bytes) -> Optional[dict[str, object]]:
    """ModelUsageStats.

    Field 2 is uncached input. Field 5 is cache read and is not inside field 2.
    Field 3 is total output. Field 9 (thinking) + field 10 (visible) equals
    field 3 on the logs this reader was checked against. Field 4 is cache write.
    """
    vals = _varint_map(buf)
    uncached = vals.get(2, 0)
    output = vals.get(3, 0)
    cache_write = vals.get(4, 0)
    cached = vals.get(5, 0)
    thinking = vals.get(9, 0)
    visible = vals.get(10, 0)
    if thinking or visible:
        if output != thinking + visible:
            output = max(output, thinking + visible)
    if thinking > output:
        thinking = output
    if not any((uncached, output, cache_write, cached, thinking)):
        return None
    return {
        "uncached": uncached,
        "cached": cached,
        "cache_write": cache_write,
        "output": output,
        "reasoning": thinking,
        "response_id": _string_field(buf, 11),
    }


def _packed_varints(raw: bytes) -> Optional[list[int]]:
    out: list[int] = []
    i = 0
    while i < len(raw):
        n = 0
        shift = 0
        while i < len(raw) and shift <= 63:
            b = raw[i]
            i += 1
            n |= (b & 0x7F) << shift
            if not b & 0x80:
                break
            shift += 7
        else:
            return None
        if n >= 10_000_000:
            return None
        out.append(n)
    return out


def step_indices(buf: bytes) -> list[int]:
    """Generator field 2: the trajectory steps this model call produced."""
    out: list[int] = []
    for number, wire, val in iter_fields(buf):
        if number != 2:
            continue
        if wire == 0 and isinstance(val, int) and 0 <= val < 10_000_000:
            out.append(val)
        elif wire == 2 and isinstance(val, (bytes, bytearray)):
            packed = _packed_varints(bytes(val))
            if packed:
                out.extend(packed)
    return out


def parse_generation(buf: bytes) -> Optional[dict[str, object]]:
    """One gen_metadata.data row. Keep the provider ID alongside the UI label."""
    chat = _sub(buf, 1)
    if not chat:
        return None
    usage_blob = _sub(chat, 4)
    usage = parse_usage(usage_blob) if usage_blob else None
    if usage is None:
        return None
    label = _string_field(chat, 21)
    model_id = _string_field(chat, 19)
    return {
        "model": label or model_id,
        "model_id": model_id,
        "usage": usage,
        "steps": step_indices(buf),
    }


def _clip(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    if len(flat) <= limit:
        return flat
    return flat[: limit - 1].rstrip() + "…"


def _utf8(blob: object, limit: Optional[int] = 240) -> tuple[str, int]:
    """Clipped preview and the full character count. Empty when it is not text."""
    if not isinstance(blob, (bytes, bytearray)) or not blob:
        return "", 0
    raw = bytes(blob)
    if b"\x00" in raw:
        return "", 0
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return "", 0
    if not text.strip():
        return "", 0
    return (text if limit is None else _clip(text, limit)), len(text)


def _field_text(buf: bytes, number: int, limit: Optional[int] = 240) -> tuple[str, int]:
    for fn, wire, val in iter_fields(buf):
        if fn == number and wire == 2:
            preview, size = _utf8(val, limit)
            if preview:
                return preview, size
    return "", 0


def _tool_call(buf: bytes) -> dict[str, object]:
    name, _ = _field_text(buf, 2, 80)
    call_id, _ = _field_text(buf, 1, 80)
    args = _sub(buf, 3) or b""
    result = 0
    for fn, wire, val in iter_fields(buf):
        if fn == 7 and wire == 2 and isinstance(val, (bytes, bytearray)):
            result += len(val)
    return {
        "name": name,
        "call_id": call_id,
        "args": args,
        "result_chars": result,
    }


def parse_step(buf: bytes) -> dict[str, object]:
    """Step metadata: time and the tool call, when this step is one."""
    epoch = None
    created = _sub(buf, 1)
    if created:
        epoch = timestamp_epoch(created)
    tool_blob = _sub(buf, 4)
    tool = _tool_call(tool_blob) if tool_blob else None
    if tool and not tool.get("name"):
        tool = None
    return {"epoch": epoch, "tool": tool}


def parse_user_text(payload: bytes, *, limit: Optional[int] = 240) -> str:
    """User step payload. The query is the user-input body, field 1 or 2."""
    body = _sub(payload, 19)
    if not body:
        return ""
    for number in (1, 2):
        preview, _size = _field_text(body, number, limit)
        if preview:
            return preview
    return ""


def parse_planner(payload: bytes) -> dict[str, object]:
    """Planner step: clipped reply, clipped thinking, and the tool calls it made."""
    body = _sub(payload, 20) or b""
    response, response_chars = _field_text(body, 1, 240)
    if not response:
        response, response_chars = _field_text(body, 8, 240)
    thinking, thinking_chars = _field_text(body, 3, 240)
    tools: list[dict[str, object]] = []
    for number, wire, val in iter_fields(body):
        if number == 7 and wire == 2 and isinstance(val, (bytes, bytearray)):
            tools.append(_tool_call(bytes(val)))
    return {
        "response": response,
        "response_chars": response_chars,
        "thinking": thinking,
        "thinking_chars": thinking_chars,
        "tools": tools,
    }


def parse_aux(payload: bytes) -> dict[str, object]:
    """A harness step that is not a model call. Kind is a short label."""
    body = _sub(payload, 114) or b""
    kind, _ = _field_text(body, 3, 40)
    if not kind or any(ch.isspace() for ch in kind):
        kind = "step"
    preview, size = _field_text(body, 1, 240)
    return {"kind": kind, "preview": preview, "chars": size}
