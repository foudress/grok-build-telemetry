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

def _is_kept_model_step(step: Any) -> bool:
    """True for a real LLM call (same keep rule as empty-step drop)."""
    if not isinstance(step, dict):
        return False
    try:
        delta = abs(int(step.get("context_delta") or 0))
    except (TypeError, ValueError):
        delta = 0
    try:
        emit = int(step.get("model_emit_delta") or 0)
    except (TypeError, ValueError):
        emit = 0
    return bool(
        step.get("thought_chunks")
        or step.get("message_chunks")
        or step.get("tools")
        or delta > 0
        or emit > 0
    )

def _finalize_step(hb: Any, step: dict[str, Any]) -> None:
    from token_telemetry.hierarchy.thoughts_enc import _stamp_stream_window

    s0 = step.get("context_start")
    s1 = step.get("context_end")
    _stamp_stream_window(step)
    if isinstance(s0, int) and isinstance(s1, int):
        # Compact / totalTokens noise can briefly drop end below start —
        # never show a negative window growth on the call line.
        step["context_delta"] = max(0, s1 - s0)

    raw_tools = list(step.get("tools") or [])
    cleaned: list[dict[str, Any]] = []
    for t in raw_tools:
        name = t.get("name") or "tool"
        tt_obs = int(t.get("tt_delta_observed") or t.get("context_delta") or 0)
        if name == "tool" and tt_obs == 0 and not t.get("title") and not t.get("result_chars"):
            continue
        tid = t.get("tool_call_id")
        cleaned.append(
            {
                "kind": "tool",
                "tool_call_id": tid,
                "tool_seq": t.get("tool_seq")
                if t.get("tool_seq") is not None
                else _tool_seq_from_id(tid if isinstance(tid, str) else None),
                "name": name,
                "title": t.get("title"),
                "status": t.get("status"),
                "path": t.get("path"),
                "offset": t.get("offset"),
                "limit": t.get("limit"),
                "result_chars": int(t.get("result_chars") or 0),
                "result_lines": int(t.get("result_lines") or 0),
                "result_tokens_est": int(t.get("result_tokens_est") or 0),
                "result_preview": t.get("result_preview"),
                "ch_result_chars": int(t.get("ch_result_chars") or 0),
                "ch_result_tokens": int(t.get("ch_result_tokens") or 0),
                "arg_chars": int(t.get("arg_chars") or 0),
                "arg_tokens_est": int(t.get("arg_tokens_est") or 0),
                "ch_arg_chars": int(t.get("ch_arg_chars") or 0),
                "ch_arg_tokens": int(t.get("ch_arg_tokens") or 0),
                "tt_delta_observed": max(0, tt_obs),
                "context_delta": max(0, tt_obs),  # may be redistributed below
                "context_before": t.get("context_before"),
                "context_after": t.get("context_after"),
                "declare_ctx": t.get("declare_ctx"),
                "plan": t.get("plan"),
                "is_plan": bool(t.get("is_plan") or t.get("plan")),
                "subagent_id": t.get("subagent_id"),
                "subagent_ids": list(t.get("subagent_ids") or []) or None,
                "subagent_type": t.get("subagent_type"),
                "subagent_description": t.get("subagent_description"),
                "resume_from": t.get("resume_from"),
            }
        )
    # Stamp chat_history tool_result + tool_calls[].arguments (authoritative)
    hb._stamp_tool_chat_results(cleaned)
    hb._stamp_tool_chat_args(cleaned)
    for t in cleaned:
        # also keep on step.tools for later reprice
        tid = t.get("tool_call_id")
        if not tid:
            continue
        for raw_t in raw_tools:
            if raw_t.get("tool_call_id") == tid:
                raw_t["ch_result_chars"] = t.get("ch_result_chars")
                raw_t["ch_result_tokens"] = t.get("ch_result_tokens")
                raw_t["result_chars"] = t.get("result_chars")
                raw_t["result_tokens_est"] = t.get("result_tokens_est")
                raw_t["arg_chars"] = t.get("arg_chars")
                raw_t["arg_tokens_est"] = t.get("arg_tokens_est")
                raw_t["ch_arg_chars"] = t.get("ch_arg_chars")
                raw_t["ch_arg_tokens"] = t.get("ch_arg_tokens")
                if t.get("weight_source"):
                    raw_t["weight_source"] = t.get("weight_source")
                if t.get("arg_weight_source"):
                    raw_t["arg_weight_source"] = t.get("arg_weight_source")
                if t.get("subagent_id"):
                    raw_t["subagent_id"] = t.get("subagent_id")
                if t.get("subagent_ids"):
                    raw_t["subagent_ids"] = list(t.get("subagent_ids") or [])
                if t.get("resume_from") and not raw_t.get("resume_from"):
                    raw_t["resume_from"] = t.get("resume_from")
                break

    emit_d = int(step.get("model_emit_delta") or 0)
    # Fallback emit from tool arg sizes (search_replace old/new often huge)
    # Note: arg sizes are for LLM→Harness Out only — never harness In weights.
    arg_emit = sum(int(t.get("arg_tokens_est") or 0) for t in cleaned)
    if emit_d <= 0 and arg_emit > 0:
        emit_d = arg_emit
        step["model_emit_delta"] = emit_d
        step["model_emit_from_args"] = True
    elif emit_d > 0 and arg_emit > emit_d * 2:
        # Counter under-reported tool-call stream; prefer arg estimate for pricing weights
        step["model_emit_arg_tokens"] = arg_emit

    late = max(0, int(step.get("late_context_delta") or 0))
    tt_tools_sum = sum(int(t.get("tt_delta_observed") or 0) for t in cleaned)
    # Tokenizer result weights (history when stamped; else stream content body)
    content_sum = sum(int(t.get("result_tokens_est") or 0) for t in cleaned)

    # --- One tool-In estimator: tokenizer result weights + late into tools ---
    ambiguous = len(cleaned) > 1
    attribution = "token_counter"
    late_absorbed = 0

    def _raw_tool_weight(t: dict[str, Any]) -> int:
        # ch_result_tokens preferred; stamp already copied into result_tokens_est
        content = max(
            0,
            int(t.get("ch_result_tokens") or 0)
            or int(t.get("result_tokens_est") or 0),
        )
        tt = max(0, int(t.get("tt_delta_observed") or 0))
        # Status-only / empty payload but counter moved → tt is the signal
        if content < 48 and tt > max(content * 3, 32):
            return tt
        if content > 0:
            return content
        return tt

    def _weights_for(tools: list[dict[str, Any]]) -> list[int]:
        """
        Split harness pool by tokenizer result tokens (not args).

        Prefer ch_result_tokens / result_tokens_est. Fallback to tt only
        for status-only tools ("ok" / tiny payload) that still moved the
        counter (search_replace, etc.). Never use arg_chars/arg_tokens.
        """
        ws = [_raw_tool_weight(t) for t in tools]
        if sum(ws) <= 0:
            return [1] * len(tools)
        return [max(1, w) if w > 0 else 0 for w in ws]

    def _allocate_pool(tools: list[dict[str, Any]], pool: int, attr: str) -> None:
        if not tools or pool <= 0:
            for t in tools:
                t["context_delta"] = 0
                t["attribution"] = attr
            return
        weights = _weights_for(tools)
        wsum = sum(weights) or 1
        allocated = 0
        last_i = max((i for i, w in enumerate(weights) if w > 0), default=len(tools) - 1)
        for i, t in enumerate(tools):
            if weights[i] <= 0:
                t["context_delta"] = 0
                t["attribution"] = attr
                continue
            if i == last_i:
                d = max(0, pool - allocated)
            else:
                d = int(round(pool * weights[i] / wsum))
                allocated += d
            t["context_delta"] = max(0, d)
            t["attribution"] = attr

    if cleaned:
        signal = sum(_raw_tool_weight(t) for t in cleaned)
        pool = int(signal)
        if late > 0:
            pool += late
            late_absorbed = late
            attribution = "weighted_incl_late"
        elif signal > 0:
            attribution = "weighted_split"
        _allocate_pool(cleaned, max(0, pool), attribution)
    elif late > 0:
        # No tools: fold late into step (never a residual UI node)
        late_absorbed = late

    harness_pool = sum(int(t.get("context_delta") or 0) for t in cleaned)

    # Serial chain display after final deltas (each tool only its own slice)
    cursor = step.get("tools_phase_start")
    if cursor is None:
        cursor = s0
    for t in cleaned:
        d = int(t.get("context_delta") or 0)
        if isinstance(cursor, int):
            t["context_before"] = cursor
            t["context_after"] = cursor + d
            cursor = t["context_after"]
        t["kind"] = "tool"

    step["tools"] = cleaned
    step["harness_pool_tokens"] = harness_pool
    step["harness_attribution"] = attribution
    step["harness_ambiguous"] = ambiguous
    step["late_absorbed"] = late_absorbed
    # Never surface late residual in UI — always 0 after redistribute
    step["late_context_delta_display"] = 0

    # Tokenizer weights for Thought / Message (full buffer when available)
    th_text = step.get("thought_summary_text") or ""
    if isinstance(th_text, str) and th_text:
        step["thought_summary_tokens"] = count_tokens(th_text)
        step["thought_summary_chars"] = len(th_text)
        step["thought_chars"] = len(th_text)
    elif not step.get("thought_summary_tokens"):
        step["thought_summary_tokens"] = count_chars_as_tokens(
            int(step.get("thought_summary_chars") or step.get("thought_chars") or 0)
        )
    msg_text = step.get("message_text") or ""
    if isinstance(msg_text, str) and msg_text:
        step["message_tokens"] = count_tokens(msg_text)
        step["message_chars"] = len(msg_text)
    elif not step.get("message_tokens"):
        step["message_tokens"] = count_chars_as_tokens(
            int(step.get("message_chars") or 0)
        )

    # Composition of context growth within this call (provisional tokZ).
    # Pricing overwrites with billed pure-Out / reason / harness shares.
    thought_tok_est = max(0, int(step.get("thought_summary_tokens") or 0))
    message_tok_est = max(0, int(step.get("message_tokens") or 0))
    harness_results = sum(int(t.get("context_delta") or 0) for t in cleaned)
    step["composition"] = {
        "thought_out": thought_tok_est,
        "model_emit": emit_d,
        "message_out": message_tok_est,
        "harness_results": harness_results,
        "late_residual": 0,
        "total": (
            thought_tok_est
            + emit_d
            + message_tok_est
            + harness_results
        ),
    }

    # --- Phase tree: LLM then harness ---
    # UI order (fixed): Thought → Reasoning[enc] → Message → Tool request[id]…
    # Thought/Message/ToolReq = exact TokZ; Enc = residual of full off_out.
    llm_children: list[dict[str, Any]] = []
    enc_c = int(step.get("thought_encrypted_chars") or 0)
    sum_c = int(
        step.get("thought_summary_chars") or step.get("thought_chars") or 0
    )
    sum_tok = int(step.get("thought_summary_tokens") or thought_tok_est or 0)

    # 1) Thought
    if sum_c > 0 or step.get("thought_chunks") or step.get("thought_preview") or sum_tok:
        llm_children.append(
            {
                "kind": "thought",
                "label": "Thought",
                "chunks": step.get("thought_chunks") or 0,
                "chars": sum_c,
                "summary_chars": sum_c,
                "summary_tokens": sum_tok,
                "tokenizer_tokens": sum_tok,
                "preview": step.get("thought_preview"),
                "context_delta": 0,
                "estimate_note": (
                    "summary_text — tokenizer definitive; inside reasoningTokens"
                ),
            }
        )

    # 2) Reasoning [encrypted] — residual filled by pricing; show when we
    # have enc chars or a thought/tools reason activity.
    # Always stamp encrypted TokZ (never leave only chars → UI chars//4 lies).
    enc_tok = int(step.get("thought_encrypted_tokens") or 0)
    if enc_tok <= 0 and enc_c > 0:
        enc_tok = count_chars_as_tokens(enc_c)
    if enc_c > 0 or enc_tok > 0 or sum_tok > 0 or emit_d > 0 or cleaned:
        enc_node: dict[str, Any] = {
            "kind": "reasoning",
            "label": "Reasoning",
            "chars": enc_c,
            "encrypted_chars": enc_c,
            "encrypted_tokens": int(enc_tok),
            "context_delta": 0,
            "estimate_note": (
                "encrypted_content TokZ (tokenizer); residual Out by pricing"
            ),
        }
        if enc_tok > 0:
            enc_node["tokenizer_tokens"] = int(enc_tok)
        llm_children.append(enc_node)

    # 3) Message (pure Out)
    if step.get("message_chunks") or message_tok_est:
        llm_children.append(
            {
                "kind": "message",
                "chunks": step.get("message_chunks") or [],
                "chars": step.get("message_chars") or 0,
                "message_tokens": int(step.get("message_tokens") or 0),
                "tokenizer_tokens": int(step.get("message_tokens") or 0),
                "preview": step.get("message_preview"),
                "context_delta": 0,
                "estimate_note": "assistant.content — pure Out pro-rata",
            }
        )

    # 4) Tool request [id] — one line per tool (RawInput tokenizer definitive)
    for t in cleaned:
        arg_tok = int(t.get("arg_tokens_est") or 0)
        arg_ch = int(t.get("arg_chars") or 0)
        if arg_tok <= 0 and arg_ch <= 0 and not t.get("name"):
            continue
        plan = t.get("plan") if isinstance(t.get("plan"), dict) else None
        is_plan = bool(t.get("is_plan") or plan)
        llm_children.append(
            {
                "kind": "tool_request",
                "label": "plan request" if is_plan else "tool request",
                "name": t.get("name"),
                "title": t.get("title"),
                "tool_call_id": t.get("tool_call_id"),
                "tool_seq": t.get("tool_seq"),
                "path": t.get("path"),
                "offset": t.get("offset"),
                "limit": t.get("limit"),
                "arg_chars": arg_ch,
                "arg_tokens_est": arg_tok,
                "tokenizer_tokens": arg_tok,
                "chars": arg_ch,
                "context_delta": 0,
                "plan": plan,
                "is_plan": is_plan,
                "estimate_note": (
                    "plan request RawInput — tokenizer definitive; "
                    "inside reasoningTokens"
                    if is_plan
                    else (
                        "tool request RawInput — tokenizer definitive; "
                        "inside reasoningTokens"
                    )
                ),
            }
        )

    # Out→In provisional (pricing overwrites with full billed Out TokF):
    # Thought + Reasoning + Message + ToolReq — all TokF after price.
    # Pre-price: thought+toolreq+message TokZ (enc residual not yet known).
    llm_to_ctx = thought_tok_est + emit_d + message_tok_est
    step["llm_to_ctx_tokens"] = llm_to_ctx

    # Tool name summary for LLM Out → In line (read_file x2 · grep x3)
    name_counts: dict[str, int] = {}
    for t in cleaned:
        nm = str(t.get("name") or "tool").strip() or "tool"
        name_counts[nm] = int(name_counts.get(nm) or 0) + 1
    tool_summary_parts: list[str] = []
    for nm, cnt in name_counts.items():
        tool_summary_parts.append(f"{nm} x{cnt}" if cnt > 1 else nm)
    tool_summary = " · ".join(tool_summary_parts)

    harness_children: list[dict[str, Any]] = []
    # First line: LLM Out [N] of this call → next-call In (provisional tok; pricing overwrites)
    call_idx = step.get("index")
    if call_idx is None:
        call_idx = step.get("step_index")
    try:
        call_n = int(call_idx) if call_idx is not None else 0
    except (TypeError, ValueError):
        call_n = 0
    if llm_to_ctx > 0 or cleaned:
        harness_children.append(
            {
                "kind": "llm_to_in",
                "label": f"LLM Out [{call_n}]",
                "call_index": call_n,
                "tool_summary": tool_summary,
                "tool_names": list(name_counts.keys()),
                "tool_name_counts": dict(name_counts),
                # Provisional: tokenizer-side Out composition; pricing sets billed Out
                "tokens_in": int(llm_to_ctx),
                "context_delta": int(llm_to_ctx),
                "tokens_out_source": int(llm_to_ctx),
                "tokenizer_tokens": int(llm_to_ctx),
                "estimate_note": (
                    "LLM Out of this call re-enters next prompt as uncached In "
                    "(with tool results). Separated so tools are not scaled to absorb Out."
                ),
            }
        )
    harness_children.extend(cleaned)
    # late residual is always redistributed into tools — never a harness child
    # Hooks that ran during this model step (track everywhere).
    # Mid-call hooks can feed the next LLM; stop/end hooks go to the user
    # and must not inflate harness_pool / next-call In / Cached.
    hook_tokens = 0
    hook_tokens_to_llm = 0
    for h in step.get("hooks") or []:
        if not isinstance(h, dict):
            continue
        h_chars = int(h.get("chars") or 0)
        h_tok = int(
            h.get("tokens_est") or max(0, count_chars_as_tokens(h_chars))
        )
        ev = str(h.get("event_name") or "hook").lower()
        to_user = ev in ("stop", "session_stop", "agent_stop")
        hook_tokens += h_tok
        if not to_user:
            hook_tokens_to_llm += h_tok
        harness_children.append(
            {
                "kind": "hook",
                "event_name": h.get("event_name") or "hook",
                "run_names": h.get("run_names") or [],
                "chars": h_chars,
                # tokens kept for display; pricing zeros In if to_user / last call
                "tokens_in": h_tok,
                "context_delta": 0 if to_user else h_tok,
                "to_user": to_user,
                "elapsed_ms": h.get("elapsed_ms"),
                "estimate_note": (
                    "hook → user (not returned to LLM)"
                    if to_user
                    else "hook_execution payload (JSON chars/4) → may feed next LLM"
                ),
            }
        )
    if hook_tokens > 0:
        # Only hooks that can feed the next model call enlarge harness pool
        harness_pool = int(harness_pool) + hook_tokens_to_llm
        step["harness_pool_tokens"] = harness_pool
        step["hook_tokens"] = hook_tokens
        step["hook_tokens_to_llm"] = hook_tokens_to_llm

    # Tools/hooks/late only in stream harness pool; LLM Out tracked separately
    # (re-enters as In but is not stream tool growth).
    harness_delta = sum(
        int(c.get("context_delta") or 0)
        for c in harness_children
        if c.get("kind") not in ("llm_to_in", "compact_out_in")
    )
    harness_in_with_out = harness_delta + int(llm_to_ctx)
    children: list[dict[str, Any]] = []
    if llm_children:
        children.append(
            {
                "kind": "phase_llm",
                "label": "LLM",
                # Out phase; growth→next In mirrored under harness as llm_to_in
                "context_delta": emit_d,
                "children": llm_children,
            }
        )
    if harness_children:
        children.append(
            {
                "kind": "phase_harness",
                "label": "Harness",
                "context_delta": harness_in_with_out,
                "ambiguous": ambiguous,
                "attribution": attribution,
                "tool_count": len(cleaned),
                "result_chars_total": sum(
                    int(t.get("result_chars") or 0) for t in cleaned
                ),
                "result_tokens_est_total": content_sum,
                "tt_observed_total": tt_tools_sum,
                "late_absorbed": late_absorbed,
                "llm_out_in_tokens": int(llm_to_ctx),
                "children": harness_children,
            }
        )

    step["children"] = children
    # Window parts: Out composition + tool/hook stream growth (Out not double-counted)
    parts = llm_to_ctx + harness_delta
    step["context_parts_sum"] = parts
    if isinstance(s0, int) and isinstance(s1, int):
        step["context_unaccounted"] = (s1 - s0) - parts
