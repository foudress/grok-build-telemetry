"""Round compaction / shallow copy for retained hierarchy state."""

from __future__ import annotations

from typing import Any

from token_telemetry.hierarchy.text_metrics import _PREVIEW_MAX, _clip_str


def compact_round_inplace(r: dict[str, Any]) -> None:
    """Drop heavy / redundant fields so retained rounds stay small."""
    # Collapse full usage blob to slim counters (enough for dashboard)
    usage = r.get("usage_raw")
    if isinstance(usage, dict) and usage:
        r["usage_raw"] = {
            k: usage.get(k)
            for k in (
                "inputTokens",
                "outputTokens",
                "reasoningTokens",
                "cachedReadTokens",
                "totalTokens",
                "costUsdTicks",
                "apiDurationMs",
                "modelCalls",
                "modelUsage",
            )
            if usage.get(k) is not None
        }
    for key in ("user_preview",):
        if key in r:
            r[key] = _clip_str(r.get(key), 120)
    up = r.get("user_prompt")
    if isinstance(up, dict):
        up["preview"] = _clip_str(up.get("preview"), 120)
        up.pop("note", None)
    notes = r.get("notes")
    if isinstance(notes, list) and len(notes) > 8:
        r["notes"] = notes[-8:]
    for step in r.get("model_steps") or []:
        if not isinstance(step, dict):
            continue
        for k in ("thought_preview", "message_preview"):
            if k in step:
                step[k] = _clip_str(step.get(k), _PREVIEW_MAX)
        # Internal bookkeeping not needed after finalize
        # Drop full text buffers once tokenizer weights are stamped (keep *tokens*)
        for k in (
            "_tool_cursor",
            "model_emit_arg_tokens",
            "model_emit_from_args",
            "thought_summary_text",
            "message_text",
        ):
            step.pop(k, None)
        for t in step.get("tools") or []:
            if not isinstance(t, dict):
                continue
            rp = t.get("result_preview")
            if rp:
                t["result_preview"] = _clip_str(rp, 60)
            else:
                t.pop("result_preview", None)
            p = t.get("path")
            if isinstance(p, str) and (len(p) > 64 or "/" in p or "\\" in p):
                parts = p.replace("\\", "/").split("/")
                t["path"] = "/".join(parts[-2:]) if len(parts) >= 2 else p[-64:]
        for ch in step.get("children") or []:
            if isinstance(ch, dict):
                _compact_child(ch)


def _compact_child(ch: dict[str, Any]) -> None:
    if not isinstance(ch, dict):
        return
    ch.pop("estimate_note", None)
    if ch.get("kind") == "tool_requests":
        declared = ch.get("declared_tools") or []
        # names only — drop per-tool path/offset bloat
        names: list[str] = []
        for d in declared:
            if isinstance(d, dict):
                names.append(str(d.get("name") or "tool"))
            else:
                names.append(str(d))
        # compress duplicates for storage
        counts: dict[str, int] = {}
        for n in names:
            counts[n] = counts.get(n, 0) + 1
        ch["declared_tools"] = [
            {"name": f"{n}×{c}" if c > 1 else n} for n, c in counts.items()
        ]
        ch.pop("estimate_note", None)
    if ch.get("kind") == "tool_request":
        ch["preview"] = _clip_str(ch.get("preview") or ch.get("title"), 60)
        ch.pop("estimate_note", None)
    if ch.get("kind") in ("thought", "reasoning", "message"):
        ch["preview"] = _clip_str(ch.get("preview"), 60)
    if ch.get("kind") == "tool":
        # keep ch_result_tokens for harness UI compare
        pass
    for sub in ch.get("children") or []:
        _compact_child(sub)

def _shallow_round_copy(src: dict[str, Any]) -> dict[str, Any]:
    """Copy round + steps + tools/children one level (enough for finalize)."""
    r = dict(src)
    steps_out: list[dict[str, Any]] = []
    for s in src.get("model_steps") or []:
        if not isinstance(s, dict):
            continue
        sc = dict(s)
        sc["tools"] = [dict(t) for t in (s.get("tools") or []) if isinstance(t, dict)]
        sc["children"] = [
            dict(c) for c in (s.get("children") or []) if isinstance(c, dict)
        ]
        steps_out.append(sc)
    r["model_steps"] = steps_out
    return r
