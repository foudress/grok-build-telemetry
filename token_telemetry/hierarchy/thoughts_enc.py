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

from token_telemetry.hierarchy.step_tree import _is_kept_model_step

def _reasoning_fp(rs: dict[str, Any]) -> tuple:
    return (
        int(rs.get("encrypted_chars") or 0),
        int(rs.get("encrypted_tokens") or 0),
        int(rs.get("summary_tokens") or 0),
        str(rs.get("preview") or "")[:48],
    )

def _unique_reasonings(reasonings: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop consecutive identical Enc blobs (jsonl rewrite loops)."""
    out: list[dict[str, Any]] = []
    prev_fp: Optional[tuple] = None
    for rs in reasonings:
        if not isinstance(rs, dict):
            continue
        fp = _reasoning_fp(rs)
        if prev_fp is not None and fp == prev_fp and (fp[0] > 0 or fp[1] > 0):
            continue
        prev_fp = fp
        out.append(rs)
    return out

def _clear_enc_stamp(step: dict[str, Any]) -> None:
    step["thought_encrypted_chars"] = 0
    step["thought_encrypted_tokens"] = 0
    step["thought_encrypted_tokens_own"] = 0
    step["thought_full_json_chars"] = 0

def _stamp_stream_window(step: dict[str, Any]) -> None:
    """Save harness snaps before reconstruct / F bump. Never overwrite."""
    if not isinstance(step, dict):
        return
    cs = step.get("context_start")
    ce = step.get("context_end")
    if isinstance(cs, int) and step.get("stream_context_start") is None:
        step["stream_context_start"] = cs
    if isinstance(ce, int) and step.get("stream_context_end") is None:
        step["stream_context_end"] = ce

def _load_reasonings_fresh(hb: Any) -> list[dict[str, Any]]:
    """Reload Enc reasonings when jsonl or compact/recap sidecars change."""
    if not hb._session_dir:
        return []
    sig = _tool_results_sig(Path(hb._session_dir))
    if hb._reasonings_cache is None or sig != getattr(hb, "_reasonings_mtime", None):
        hb._reasonings_cache = load_chat_history_reasonings(hb._session_dir)
        hb._reasonings_mtime = sig
    return list(hb._reasonings_cache or [])

def _tool_results_sig(session_dir: Path) -> tuple:
    parts: list[tuple] = []
    ch_path = session_dir / "chat_history.jsonl"
    try:
        if ch_path.is_file():
            st = ch_path.stat()
            parts.append(("chat_history.jsonl", float(st.st_mtime), int(st.st_size)))
    except OSError:
        pass
    for sub in ("compaction_requests", "recap_requests"):
        folder = session_dir / sub
        if not folder.is_dir():
            continue
        try:
            files = sorted(folder.glob("*.json"))
        except OSError:
            continue
        for fp in files:
            try:
                st = fp.stat()
            except OSError:
                continue
            parts.append((fp.name, float(st.st_mtime), int(st.st_size)))
    return tuple(parts)

def _load_tool_results_fresh(hb: Any) -> dict[str, dict[str, Any]]:
    """Reload chat_history tool_result content tokens when files change."""
    if not hb._session_dir:
        return {}
    sig = _tool_results_sig(Path(hb._session_dir))
    if hb._tool_results_cache is None or sig != getattr(hb, "_tool_results_mtime", None):
        hb._tool_results_cache = load_chat_history_tool_results(hb._session_dir)
        hb._tool_results_mtime = sig
    return dict(hb._tool_results_cache or {})

def _load_tool_requests_fresh(hb: Any) -> dict[str, dict[str, Any]]:
    """Reload chat_history tool_calls[].arguments tokens when files change."""
    if not hb._session_dir:
        return {}
    sig = _tool_results_sig(Path(hb._session_dir))
    if hb._tool_requests_cache is None or sig != getattr(
        hb, "_tool_requests_mtime", None
    ):
        hb._tool_requests_cache = load_chat_history_tool_requests(hb._session_dir)
        hb._tool_requests_mtime = sig
    return dict(hb._tool_requests_cache or {})

def _stamp_tool_chat_results(hb: Any, tools: list[dict[str, Any]]) -> None:
    """Attach chat_history tool_result.content tokenizer size onto harness tools.

    chat_history is authoritative for result **chars** and tokenizer **weights**
    (stream tool_call_update bodies under-count / truncate tools like grep).
    Weights = inner ``content`` only (grok-build ToolResult) — never the ACP
    ``{type,tool_call_id,content}`` envelope.
    """
    from token_telemetry.session.subagents import apply_history_subagent_ids

    by_id = hb._load_tool_results_fresh()
    if not by_id:
        return
    for t in tools:
        if not isinstance(t, dict):
            continue
        tid = t.get("tool_call_id")
        if not tid:
            continue
        hit = by_id.get(str(tid))
        if not hit:
            continue
        name = str(t.get("name") or "").lower()
        waitish = (
            "get_command" in name
            or name in ("spawn_subagent", "kill_command_or_subagent")
        )
        ch_chars = int(hit.get("body_chars") or 0)
        ch_tok = int(hit.get("body_tokens") or 0)
        t["ch_result_chars"] = ch_chars
        t["ch_result_tokens"] = ch_tok
        # Prefer history body for UI chars + harness pro-rata weights.
        if ch_chars > 0:
            t["result_chars"] = ch_chars
        if ch_tok > 0:
            t["result_tokens_est"] = ch_tok
            t["weight_source"] = "chat_history_tokenizer"
        if hit.get("preview") and not t.get("result_preview"):
            t["result_preview"] = hit["preview"]
        if waitish:
            apply_history_subagent_ids(t, hit)

def _stamp_tool_chat_args(hb: Any, tools: list[dict[str, Any]]) -> None:
    """Attach chat_history tool_calls[].arguments as ToolRequest Out weights.

    Authoritative over stream ``rawInput`` (updates often add harness-only
    fields like ``variant`` / ``is_background``).
    """
    by_id = hb._load_tool_requests_fresh()
    if not by_id:
        return
    for t in tools:
        if not isinstance(t, dict):
            continue
        tid = t.get("tool_call_id")
        if not tid:
            continue
        hit = by_id.get(str(tid))
        if not hit:
            continue
        ch_chars = int(hit.get("arg_chars") or 0)
        ch_tok = int(hit.get("arg_tokens") or 0)
        if ch_chars <= 0 and ch_tok <= 0:
            continue
        t["ch_arg_chars"] = ch_chars
        t["ch_arg_tokens"] = ch_tok
        t["arg_chars"] = ch_chars
        t["arg_tokens_est"] = ch_tok
        t["arg_weight_source"] = "chat_history_arguments"
        if hit.get("name") and (
            not t.get("name") or str(t.get("name") or "") in ("tool", "")
        ):
            t["name"] = hit["name"]

def _stamp_step_reasoning(step: dict[str, Any], rs: dict[str, Any]) -> None:
    """Apply one chat_history reasoning item onto a model step."""
    full_c = int(rs.get("full_chars") or 0)
    enc_c = int(rs.get("encrypted_chars") or 0)
    sum_c = int(rs.get("summary_chars") or 0)
    sum_text = rs.get("summary_text") or ""
    if isinstance(sum_text, str) and sum_text:
        sum_tok = int(rs.get("summary_tokens") or count_tokens(sum_text))
    else:
        sum_tok = int(rs.get("summary_tokens") or 0)
    enc_tok = int(rs.get("encrypted_tokens") or 0)
    if enc_tok <= 0 and enc_c > 0:
        enc_tok = count_chars_as_tokens(enc_c)
    # thought_chars / summary = readable Out weight only.
    # encrypted_content sizes weight the Reasoning share of reasoningTokens.
    stream_c = int(step.get("thought_chars") or 0)
    stream_text = step.get("thought_summary_text") or ""
    if sum_c >= stream_c and sum_text:
        step["thought_summary_text"] = sum_text
        step["thought_summary_chars"] = sum_c
        step["thought_summary_tokens"] = sum_tok
        step["thought_chars"] = sum_c
    else:
        step["thought_summary_chars"] = max(
            int(step.get("thought_summary_chars") or 0), sum_c, stream_c
        )
        step["thought_chars"] = max(stream_c, sum_c)
        if stream_text:
            step["thought_summary_tokens"] = count_tokens(stream_text)
        elif sum_tok > 0:
            step["thought_summary_tokens"] = sum_tok
    # Absolute assign (session remap clears first)
    step["thought_encrypted_chars"] = int(enc_c)
    step["thought_encrypted_tokens"] = int(enc_tok)
    # 1:1 stamp only — extra reasonings merged onto last must not inflate tok/s
    step["thought_encrypted_tokens_own"] = int(enc_tok)
    step["thought_full_json_chars"] = int(full_c)
    if rs.get("preview") and not step.get("thought_preview"):
        step["thought_preview"] = rs["preview"]
    if not step.get("thought_chunks"):
        step["thought_chunks"] = 1

def _enrich_session_thoughts(hb: Any) -> None:
    """
    Map Enc reasoning items onto LLM calls (1:1, session order).

    Source is compact/recap sidecar snapshots (pre-compact Enc) plus the
    current chat_history.jsonl window, unique oldest-first. That recovered
    log belongs on early calls — not only the live round.

    Empty placeholder steps do not consume a row. Consecutive identical
    blobs are skipped. Extra unique rows merge into the last kept call.
    """
    if not hb._session_dir:
        return
    reasonings = _unique_reasonings(hb._load_reasonings_fresh())
    if not reasonings:
        return

    rounds: list[dict[str, Any]] = list(hb.rounds)
    if hb._open is not None and hb._open not in rounds:
        rounds.append(hb._open)

    all_steps: list[dict[str, Any]] = []
    kept: list[dict[str, Any]] = []
    for rr in rounds:
        for s in rr.get("model_steps") or []:
            if not isinstance(s, dict):
                continue
            all_steps.append(s)
            if _is_kept_model_step(s):
                kept.append(s)
    if not kept:
        return

    for s in all_steps:
        _clear_enc_stamp(s)
    for step, rs in zip(kept, reasonings):
        hb._stamp_step_reasoning(step, rs)
    extras = reasonings[len(kept) :]
    if extras:
        last = kept[-1]
        for extra in extras:
            last["thought_encrypted_chars"] = int(
                last.get("thought_encrypted_chars") or 0
            ) + int(extra.get("encrypted_chars") or 0)
            last["thought_encrypted_tokens"] = int(
                last.get("thought_encrypted_tokens") or 0
            ) + int(extra.get("encrypted_tokens") or 0)

def _patch_reasoning_chars_on_trees(hb: Any) -> None:
    """Push step thought_encrypted/summary chars+TokZ onto existing LLM children."""
    rounds: list[dict[str, Any]] = list(hb.rounds)
    if hb._open is not None and hb._open not in rounds:
        rounds.append(hb._open)
    for rr in rounds:
        for step in rr.get("model_steps") or []:
            if not isinstance(step, dict):
                continue
            enc_c = int(step.get("thought_encrypted_chars") or 0)
            enc_tok = int(step.get("thought_encrypted_tokens") or 0)
            if enc_tok <= 0 and enc_c > 0:
                enc_tok = count_chars_as_tokens(enc_c)
            sum_c = int(
                step.get("thought_summary_chars")
                or step.get("thought_chars")
                or 0
            )
            sum_tok = int(step.get("thought_summary_tokens") or 0)
            for ch in step.get("children") or []:
                if not isinstance(ch, dict):
                    continue
                kids = ch.get("children") if ch.get("kind") == "phase_llm" else None
                if kids is None and ch.get("kind") in ("reasoning", "thought"):
                    kids = [ch]
                if not kids:
                    continue
                for c in kids:
                    if not isinstance(c, dict):
                        continue
                    if c.get("kind") == "reasoning":
                        c["chars"] = enc_c
                        c["encrypted_chars"] = enc_c
                        c["encrypted_tokens"] = int(enc_tok)
                        if enc_tok > 0:
                            c["tokenizer_tokens"] = int(enc_tok)
                        else:
                            c.pop("tokenizer_tokens", None)
                    elif c.get("kind") == "thought":
                        c["chars"] = sum_c
                        c["summary_chars"] = sum_c
                        if sum_tok > 0:
                            c["summary_tokens"] = sum_tok
                            c["tokenizer_tokens"] = sum_tok

def _enrich_round_thoughts(hb: Any, r: dict[str, Any]) -> None:
    """Keep stream thought floors. Enc mapping is snapshot → latest round only."""
    for step in r.get("model_steps") or []:
        if not isinstance(step, dict):
            continue
        stream_c = int(step.get("thought_chars") or 0)
        if stream_c > 0:
            step["thought_summary_chars"] = max(
                int(step.get("thought_summary_chars") or 0), stream_c
            )
    hb._patch_reasoning_chars_on_trees()
