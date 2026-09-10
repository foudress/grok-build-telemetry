"""Moved from finalize.py — same function bodies."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from token_telemetry.pricing import pricing_model_scope, reconstruct_model_step_usage
from token_telemetry.tokenizer import (
    count_chars_as_tokens,
    count_tokens,
    count_user_prompt_tokens,
)
from token_telemetry.hierarchy.bootstrap import (
    _is_compact_continuation,
    inject_tool_definitions_into_bootstrap,
    load_chat_history_reasonings,
    load_chat_history_tool_requests,
    load_chat_history_tool_results,
    parse_session_bootstrap,
    resolve_tool_definitions,
)
from token_telemetry.hierarchy.compact_out import compact_round_inplace
from token_telemetry.hierarchy.tools_meta import _tool_seq_from_id
from token_telemetry.util import as_int as _as_tok

_DROP_SYS_PARTS = frozenset(
    {"message", "hooks", "tool_definitions", "tool_defs_message", "unknown"}
)

def _bootstrap_hist_tokens(boot: dict[str, Any]) -> int:
    """System-card history tokens (exclude tool_definitions / message / hooks)."""
    return sum(
        int(p.get("tokens") or 0)
        for p in (boot.get("parts") or [])
        if isinstance(p, dict) and p.get("kind") not in _DROP_SYS_PARTS
    )

def _is_first_round(hb: Any, r: dict[str, Any]) -> bool:
    """True for System-card R1 (index 1 or existing card)."""
    sys_p = r.get("system_prompt")
    if isinstance(sys_p, dict) and sys_p.get("kind") == "system_prompt":
        return True
    try:
        return int(r.get("index") or 0) == 1
    except (TypeError, ValueError):
        return False

def _is_cold_session_first(r: dict[str, Any]) -> bool:
    """True when reconstruct prior must stay None (cold R1, not a pruned list head)."""
    return r.get("cache_baseline_at_start") is None and _is_first_round(None, r)

def _official_input_tokens(r: dict[str, Any]) -> Optional[int]:
    usage = r.get("usage_raw")
    if not isinstance(usage, dict):
        return None
    for key in ("inputTokens", "input_tokens"):
        if usage.get(key) is not None:
            try:
                return max(0, int(usage[key]))
            except (TypeError, ValueError):
                return None
    return None

def _first_raw_stream_cs(steps: list[dict[str, Any]]) -> Optional[int]:
    """Un-bumped Call-1 stream start (raw snap before ToolDef remainder)."""
    for s in steps or []:
        if not isinstance(s, dict):
            continue
        for key in ("stream_context_raw", "stream_context_start", "context_start"):
            v = s.get(key)
            if isinstance(v, int) and v > 0:
                return int(v)
        return None
    return None

def stamp_r1_tooldef_jump(round_: dict[str, Any], kind: Any, tt: Any) -> None:
    """Record first LLM totalTokens and the first later increase (any update shape).

    Cold R1 only. Does not assume tool_call vs tool_call_update order — some
    sessions keep tt flat through the first completed tools, then jump on the
    next declare; others jump on the first pending tool_call.
    """
    if not isinstance(round_, dict) or not isinstance(tt, int) or tt < 0:
        return
    if round_.get("cache_baseline_at_start") is not None:
        return
    k = str(kind or "")
    if k in ("agent_thought_chunk", "agent_message_chunk"):
        if round_.get("first_llm_total_tokens") is None:
            round_["first_llm_total_tokens"] = int(tt)
        return
    if k not in ("tool_call", "tool_call_update"):
        return
    base = round_.get("first_llm_total_tokens")
    if not isinstance(base, int):
        return
    if round_.get("tooldef_high_total_tokens") is not None:
        return
    if int(tt) > int(base):
        round_["tooldef_high_total_tokens"] = int(tt)

def _first_tool_call_total_tokens(
    r: dict[str, Any], steps: Optional[list[dict[str, Any]]] = None
) -> Optional[int]:
    """totalTokens at first tool_call[0], not a later round peak (~26.6k)."""
    seq = steps if steps is not None else (r.get("model_steps") or [])
    snap = None
    fallback = None
    for s in seq or []:
        if not isinstance(s, dict):
            continue
        tagged = s.get("first_tool_call_total_tokens")
        if isinstance(tagged, int) and tagged > 0:
            return int(tagged)
        raw = s.get("stream_context_raw")
        if isinstance(raw, int) and raw > 0 and snap is None:
            snap = int(raw)
            continue
        if snap is not None:
            continue
        for key in ("total_tokens", "totalTokens", "context_start"):
            v = s.get(key)
            if isinstance(v, int) and v > 0 and fallback is None:
                fallback = int(v)
                break
    if snap is not None:
        return snap
    return fallback

def _first_llm_total_tokens(
    r: dict[str, Any], steps: Optional[list[dict[str, Any]]] = None
) -> Optional[int]:
    tagged = r.get("first_llm_total_tokens")
    if isinstance(tagged, int) and tagged > 0:
        return int(tagged)
    seq = steps if steps is not None else (r.get("model_steps") or [])
    for s in seq or []:
        if not isinstance(s, dict):
            continue
        for key in ("stream_context_raw", "context_start", "total_tokens", "totalTokens"):
            v = s.get(key)
            if isinstance(v, int) and v > 0:
                return int(v)
        break
    return None

def _scan_first_tt_increase(steps: list[dict[str, Any]], baseline: int) -> Optional[int]:
    """First totalTokens strictly above the first LLM snapshot (any tool shape)."""
    for s in steps or []:
        if not isinstance(s, dict):
            continue
        mea = s.get("model_emit_after")
        if isinstance(mea, int) and mea > baseline:
            return int(mea)
        for t in s.get("tools") or []:
            if not isinstance(t, dict):
                continue
            for key in ("declare_ctx", "context_after", "context_before"):
                v = t.get(key)
                if isinstance(v, int) and v > baseline:
                    return int(v)
        for key in ("context_end", "context_peak", "total_tokens", "totalTokens"):
            v = s.get(key)
            if isinstance(v, int) and v > baseline:
                return int(v)
    return None

def _tooldef_high_total_tokens(
    r: dict[str, Any], steps: Optional[list[dict[str, Any]]] = None
) -> Optional[int]:
    """High side of the ToolDef jump — never the round peak by default."""
    seq = steps if steps is not None else (r.get("model_steps") or [])
    tagged = r.get("tooldef_high_total_tokens")
    base = _first_llm_total_tokens(r, seq)
    if isinstance(tagged, int) and tagged > 0:
        if not isinstance(base, int) or tagged > base:
            return int(tagged)
    if isinstance(base, int) and base > 0:
        jumped = _scan_first_tt_increase(seq, base)
        if jumped is not None:
            return int(jumped)
        return int(base)
    return _first_tool_call_total_tokens(r, seq)

def _tooldef_jump_window(
    r: dict[str, Any], steps: Optional[list[dict[str, Any]]] = None
) -> Optional[int]:
    """High totalTokens of the ToolDef jump (not round context_end)."""
    return _tooldef_high_total_tokens(r, steps)

def _call1_out_tokens(step: Optional[dict[str, Any]]) -> int:
    """Call-1 Out in the jump window: thought + message + tool-request args."""
    if not isinstance(step, dict):
        return 0
    thought = _as_tok(step.get("thought_tokens") or step.get("thought_tokenizer_tokens"))
    if thought <= 0:
        thought = _as_tok(
            count_chars_as_tokens(
                _as_tok(step.get("thought_summary_chars") or step.get("thought_chars"))
            )
        )
    message = _as_tok(step.get("message_tokens") or step.get("message_tokenizer_tokens"))
    if message <= 0:
        message = _as_tok(count_chars_as_tokens(_as_tok(step.get("message_chars"))))
    args = 0
    for t in step.get("tools") or []:
        if not isinstance(t, dict):
            continue
        one = _as_tok(t.get("arg_tokens_est"))
        if one <= 0:
            one = _as_tok(count_chars_as_tokens(_as_tok(t.get("arg_chars"))))
        args += one
    return max(0, thought + message + args)

def _prejump_result_tokens(
    steps: list[dict[str, Any]], tt_llm: Optional[int], tt_high: Optional[int]
) -> int:
    """Tool results completed while totalTokens was still the LLM snapshot."""
    if not isinstance(tt_llm, int) or not isinstance(tt_high, int) or tt_high <= tt_llm:
        return 0
    tot = 0
    for s in steps or []:
        if not isinstance(s, dict):
            continue
        for t in s.get("tools") or []:
            if not isinstance(t, dict):
                continue
            dc = t.get("declare_ctx")
            if not isinstance(dc, int) or dc > tt_llm:
                continue
            after = t.get("context_after")
            if isinstance(after, int) and after > tt_llm:
                continue
            one = _as_tok(t.get("result_tokens_est"))
            if one <= 0:
                one = _as_tok(count_chars_as_tokens(_as_tok(t.get("result_chars"))))
            tot += one
    return tot

def _independent_tooldef_size(
    hb: Any, r: dict[str, Any]
) -> tuple[int, int]:
    """Measured ToolDef size from env, session file, or schema tokZ."""
    session_dir = getattr(hb, "_session_dir", None)
    resolved = resolve_tool_definitions(session_dir)
    count = int(resolved.get("count") or 0)
    src = str(resolved.get("source") or "")
    tok = 0
    try:
        tok = int(resolved.get("tokens") or 0)
    except (TypeError, ValueError):
        tok = 0
    if src in ("env", "session_tool_definitions.json") and tok > 0:
        return tok, count
    meta = r.get("tool_definitions") if isinstance(r.get("tool_definitions"), dict) else {}
    try:
        indep = int(meta.get("independent_tokens") or 0)
    except (TypeError, ValueError):
        indep = 0
    if indep > 0:
        try:
            count = int(meta.get("count") or count)
        except (TypeError, ValueError):
            pass
        return indep, count
    for key in ("schema_tokens", "tokenizer_tokens"):
        if meta.get(key) is None:
            continue
        try:
            schema = int(meta[key])
        except (TypeError, ValueError):
            continue
        if schema > 0:
            try:
                count = int(meta.get("count") or count)
            except (TypeError, ValueError):
                pass
            return schema, count
    return 0, count

def _estimate_tooldef_message_bucket(
    r: dict[str, Any],
    boot: dict[str, Any],
    steps: list[dict[str, Any]],
) -> int:
    """Tool Definitions In: jump high-side − hist − User − Call-1 Out (− pre-jump results).

    Window end is the first totalTokens increase after the first LLM snapshot
    (or that snapshot when tt never moved). Never round context_end.
    """
    hist = _bootstrap_hist_tokens(boot)
    user = _as_tok(boot.get("user_tokens"))
    real_steps = [s for s in (steps or []) if isinstance(s, dict)]
    tt_high = _tooldef_high_total_tokens(r, real_steps)
    if not isinstance(tt_high, int) or tt_high <= 0:
        return 0
    tt_llm = _first_llm_total_tokens(r, real_steps)
    s0 = real_steps[0] if real_steps else {}
    out1 = _call1_out_tokens(s0 if isinstance(s0, dict) else None)
    results = _prejump_result_tokens(real_steps, tt_llm, tt_high)
    remain = int(tt_high) - int(hist)
    for piece in (user, out1, results):
        if remain <= 0:
            break
        remain -= min(int(piece), remain)
    return max(0, int(remain))

def _r1_tree_in_tokens(r: dict[str, Any]) -> int:
    """User In (includes Compact Out when folded) + Σ LLM call In."""
    up = r.get("user_prompt") if isinstance(r.get("user_prompt"), dict) else {}
    user_in = int(
        (up or {}).get("tokens_in")
        or (up or {}).get("uncached_est")
        or 0
    )
    co = (up or {}).get("compact_out") if isinstance((up or {}).get("compact_out"), dict) else {}
    try:
        co_tok = int(co.get("tokens_in") or 0)
    except (TypeError, ValueError):
        co_tok = 0
    extra_co = 0 if (up or {}).get("compact_out_in_user_in") else int(co_tok)
    steps_m = [s for s in (r.get("model_steps") or []) if isinstance(s, dict)]
    sum_call = sum(
        int(s.get("tokens_in") or s.get("harness_in_tokens") or 0)
        for s in steps_m
    )
    return int(user_in) + int(extra_co) + int(sum_call)

def _remainder_rows(
    bucket: int, tool_tok: int, unk_tok: int, count: int, split: bool
) -> list[dict[str, Any]]:
    """System extra In is Tool Definitions (no Message leftover row)."""
    if bucket <= 0:
        return []
    size = int(tool_tok) if (split and int(tool_tok) > 0) else int(bucket)
    if size <= 0:
        size = int(bucket)
    if size > int(bucket):
        size = int(bucket)
    if size <= 0:
        return []
    return [
        {
            "kind": "tool_definitions",
            "label": "Tool Definitions",
            "tokens": int(size),
            "tokenizer_tokens": int(size),
            "chars": 0,
            "tool_count": count or None,
            "preview": (
                f"{count} tools · silent API channel (totalTokens jump)"
                if count
                else "silent API channel (totalTokens jump)"
            ),
            "messages": 0,
            "note": (
                "Tool Definitions = first totalTokens jump after the first LLM "
                "response minus System history, User In, and Call-1 Out "
                "(thought / message / tool request). Affiliated with System, "
                "not Round 1."
            ),
        }
    ]

def _peel_tooldef_from_r1(r: dict[str, Any], tool_tok: int) -> int:
    """Move ToolDef In off Call 1 / Round 1 (it belongs on System). Idempotent."""
    take_need = max(0, int(tool_tok))
    if take_need <= 0:
        return 0
    steps = [s for s in (r.get("model_steps") or []) if isinstance(s, dict)]
    if not steps:
        return 0
    s0 = steps[0]
    if s0.get("tokens_in_before_tooldef_peel") is None:
        s0["tokens_in_before_tooldef_peel"] = _as_tok(
            s0.get("tokens_in") or s0.get("harness_in_tokens")
        )
        if s0.get("harness_in_tokens") is not None:
            s0["harness_in_tokens_before_tooldef_peel"] = _as_tok(s0.get("harness_in_tokens"))
        if s0.get("cost_in_usd") is not None:
            s0["cost_in_usd_before_tooldef_peel"] = s0.get("cost_in_usd")
        if s0.get("harness_in_usd") is not None:
            s0["harness_in_usd_before_tooldef_peel"] = s0.get("harness_in_usd")
        if s0.get("model_emit_delta") is not None:
            s0["model_emit_delta_before_tooldef_peel"] = s0.get("model_emit_delta")
    cur = _as_tok(s0.get("tokens_in_before_tooldef_peel"))
    take = min(cur, take_need)
    if take <= 0:
        return 0
    new = cur - take
    s0["tokens_in"] = new
    if s0.get("harness_in_tokens") is not None or s0.get("harness_in_tokens_before_tooldef_peel") is not None:
        base_h = s0.get("harness_in_tokens_before_tooldef_peel")
        if base_h is None:
            base_h = s0.get("harness_in_tokens")
        s0["harness_in_tokens"] = max(0, _as_tok(base_h) - take)
    usd = s0.get("cost_in_usd_before_tooldef_peel", s0.get("cost_in_usd"))
    try:
        if usd is not None and cur > 0:
            s0["cost_in_usd"] = float(usd) * (new / cur)
    except (TypeError, ValueError):
        pass
    husd = s0.get("harness_in_usd_before_tooldef_peel", s0.get("harness_in_usd"))
    try:
        if husd is not None and cur > 0:
            s0["harness_in_usd"] = float(husd) * (new / cur)
    except (TypeError, ValueError):
        pass
    emit = _as_tok(s0.get("model_emit_delta_before_tooldef_peel", s0.get("model_emit_delta")))
    if emit > 0:
        s0["model_emit_delta"] = max(0, emit - min(emit, take))
    s0["tooldef_peeled_from_call"] = int(take)
    return int(take)

def _inject_system_message_residual(
    hb: Any, r: dict[str, Any], recon: dict[str, Any]
) -> None:
    """Stamp System Tool Definitions from the R1 totalTokens jump (else window remainder)."""
    sys_p = r.get("system_prompt")
    if not isinstance(sys_p, dict) or sys_p.get("kind") != "system_prompt":
        return

    parts = [
        p
        for p in (sys_p.get("parts") or [])
        if isinstance(p, dict) and p.get("kind") not in _DROP_SYS_PARTS
    ]
    # Keep scaled hist In — TokZ must not undo stream-start scaling.
    # Prefer `tokens` even when 0 (falsy-but-scaled); missing only falls to TokZ.
    known_hist = 0
    for p in parts:
        if p.get("tokens") is not None:
            try:
                known_hist += int(p.get("tokens") or 0)
                continue
            except (TypeError, ValueError):
                pass
        try:
            known_hist += int(p.get("tokenizer_tokens") or 0)
        except (TypeError, ValueError):
            pass
    r1_tree = _r1_tree_in_tokens(r)

    stream_formula = 0
    end = r.get("context_end")
    steps_m = [s for s in (r.get("model_steps") or []) if isinstance(s, dict)]
    boot_n = r.get("session_bootstrap") if isinstance(r.get("session_bootstrap"), dict) else {}
    measured = _estimate_tooldef_message_bucket(r, boot_n or {}, steps_m)
    off_in_floor = _official_input_tokens(r)
    stream_cs_floor = _first_raw_stream_cs(steps_m)
    has_off_floor = off_in_floor is not None and stream_cs_floor is not None
    tt_llm = _first_llm_total_tokens(r, steps_m)
    tt_high = _tooldef_high_total_tokens(r, steps_m)
    real_jump = (
        isinstance(tt_llm, int)
        and isinstance(tt_high, int)
        and int(tt_high) > int(tt_llm)
    )
    has_first_tt = isinstance(tt_high, int) and tt_high > 0
    if real_jump:
        stream_formula = int(measured)
    elif has_first_tt and measured > 0:
        stream_formula = int(measured)
    elif isinstance(end, int) and end > 0:
        stream_formula = max(0, int(end) - int(r1_tree) - int(known_hist))
    if (not real_jump) and len(steps_m) == 1 and has_off_floor:
        stream_formula = max(
            stream_formula, max(0, int(off_in_floor) - int(stream_cs_floor))
        )
    recon_residual = 0
    if isinstance(recon, dict):
        try:
            recon_residual = max(0, int(recon.get("bootstrap_residual_tokens") or 0))
        except (TypeError, ValueError):
            recon_residual = 0
    if real_jump or (has_first_tt and measured > 0):
        bucket = int(stream_formula)
    else:
        bucket = max(stream_formula, recon_residual)

    if bucket > 0:
        up_n = r.get("user_prompt") if isinstance(r.get("user_prompt"), dict) else {}
        boot_n = r.get("session_bootstrap") if isinstance(r.get("session_bootstrap"), dict) else {}
        try:
            user_only = int(
                (up_n or {}).get("tokens_in")
                or (up_n or {}).get("uncached_est")
                or (boot_n or {}).get("user_tokens")
                or 0
            )
        except (TypeError, ValueError):
            user_only = 0
        tree_for_need = max(int(r1_tree), int(user_only))
        needed = int(known_hist) + int(bucket) + int(tree_for_need)
        off_in_need = _official_input_tokens(r)
        stream_cs_need = _first_raw_stream_cs(r.get("model_steps") or [])
        if (
            off_in_need is not None
            and stream_cs_need is not None
            and int(bucket) == int(off_in_need) - int(stream_cs_need)
        ):
            needed = max(needed, int(off_in_need))
        if isinstance(end, int) and needed > int(end):
            r["context_end"] = needed
            start = r.get("context_start")
            if isinstance(start, int) and start != 0:
                r["context_delta"] = needed - int(start)
            else:
                r["context_delta"] = needed
            end = needed
    usage_n = r.get("usage_raw") if isinstance(r.get("usage_raw"), dict) else {}
    try:
        n_calls = int(usage_n.get("modelCalls") or usage_n.get("model_calls") or 0)
    except (TypeError, ValueError):
        n_calls = 0
    off_in_r1 = _official_input_tokens(r)
    if n_calls == 1 and off_in_r1 is not None:
        end_now = r.get("context_end")
        if not isinstance(end_now, int) or int(off_in_r1) > int(end_now):
            r["context_end"] = int(off_in_r1)
            start = r.get("context_start")
            if isinstance(start, int) and start != 0:
                r["context_delta"] = int(off_in_r1) - int(start)
            else:
                r["context_delta"] = int(off_in_r1)
            end = int(off_in_r1)

    indep, count = _independent_tooldef_size(hb, r)
    tool_meta = r.get("tool_definitions") if isinstance(r.get("tool_definitions"), dict) else {}
    try:
        count = int(tool_meta.get("count") or count or 0)
    except (TypeError, ValueError):
        count = int(count or 0)
    # Measured jump / window remainder wins; env file only fills when unmeasured.
    if bucket > 0:
        tool_tok = max(0, int(bucket))
    elif indep > 0:
        tool_tok = int(indep)
        bucket = int(tool_tok)
    else:
        tool_tok = 0
    leftover = 0
    extra = _remainder_rows(bucket, tool_tok, leftover, count, True)
    parts.extend(extra)
    shown_tool = int(tool_tok)
    if real_jump and shown_tool > 0:
        _peel_tooldef_from_r1(r, shown_tool)

    new_tok = int(known_hist) + int(bucket)
    sys_p["parts"] = parts
    sys_p["tokens_in"] = new_tok
    sys_p["logical_tokens"] = new_tok
    sys_p["uncached_est"] = new_tok
    sys_p["message_residual_tokens"] = int(bucket)
    sys_p["tool_definitions_tokens"] = int(shown_tool)
    sys_p["label"] = "System / tools / reminders / MCP / Tool Definitions"
    sys_p["note"] = (
        "History TokZ as In; Tool Definitions from the first totalTokens jump "
        "minus User In and Call-1 Out. That In is on System, peeled from Round 1."
    )

    boot = r.get("session_bootstrap")
    if isinstance(boot, dict):
        boot["system_tokens"] = int(new_tok)
        boot["message_residual_tokens"] = int(bucket)
        boot["tool_definitions_tokens"] = int(shown_tool)
        bparts = [
            p
            for p in (boot.get("parts") or [])
            if isinstance(p, dict) and p.get("kind") not in _DROP_SYS_PARTS
        ]
        bparts.extend(extra)
        boot["parts"] = bparts
    r["bootstrap_residual_tokens"] = int(bucket)
    if isinstance(r.get("step_usage"), dict):
        r["step_usage"]["bootstrap_residual_tokens"] = int(bucket)
