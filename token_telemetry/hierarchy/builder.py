"""
Build a TUI-like hierarchical reconstruction of a Grok session turn:

  round (user prompt → turn_completed)
    └── model_step  (one streamStartMs / back-and-forth with the model)
          ├── model_emit (prompt growth when the model streams tool-calls)
          ├── thought / message aggregates
          └── tool calls with **serial** context deltas from _meta.totalTokens

Context accounting rules
------------------------
- totalTokens often jumps once when the model *declares* a batch of tools
  (model_emit), then grows again as each tool_call_update completes.
- Parallel tools: live cursor is serial, but finalize **re-splits** harness
  growth by result size when multi-tool / lag / last-tool skew so tool N
  does not inherit tool N-1's Δ; late residual merges into tools when it is
  lagging payload (not a second bill of the last tool).
- Gap between step N end and step N+1 start is folded into step N's end
  (late assistant tokens only visible at next stream start).
- auto_compact_completed updates the session cursor to tokens_after and is
  recorded as a between-round Compact card (compact_after on previous round,
  compact_before on next) with tokens_removed + deferred reload In estimate.
- Cache baseline carries across rounds (and through compact).
- Call In is *caused* growth (paid on next call), shifted onto call n-1.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from token_telemetry.tokenizer import count_chars_as_tokens

from token_telemetry.hierarchy.cache_miss import (
    _apply_session_restart_cache_miss as _cm_apply_session_restart_cache_miss,
    _attach_prev_llm_answer as _cm_attach_prev_llm_answer,
    _compute_idle_gap_ms as _cm_compute_idle_gap_ms,
    _detect_context_reread as _cm_detect_context_reread,
)
from token_telemetry.hierarchy.compact_out import (
    _shallow_round_copy,
    compact_round_inplace,
)
from token_telemetry.hierarchy.finalize import (
    _attach_step_estimates,
    _enc_stamp_signature,
    _enrich_round_thoughts,
    _enrich_session_thoughts,
    _finalize_round,
    _finalize_step,
    _inject_system_message_residual,
    _load_reasonings_fresh,
    _load_tool_results_fresh,
    _merge_bootstrap_into_breakdown,
    _patch_reasoning_chars_on_trees,
    _price_bootstrap_prompts,
    _reprice_completed_rounds,
    _stamp_step_reasoning,
    _stamp_tool_chat_results,
)
from token_telemetry.hierarchy.hooks import _hook_slot
from token_telemetry.hierarchy.recap_compact import (
    _attach_pending_recap_compact,
    _fill_compact_cost as _rc_fill_compact_cost,
    _on_compact as _rc_on_compact,
    _on_recap as _rc_on_recap,
    _recap_prompt_info as _rc_recap_prompt_info,
)
from token_telemetry.hierarchy.text_metrics import (
    MAX_ROUNDS_RETAINED,
    _preview,
    _text_of,
)
from token_telemetry.hierarchy.tools_meta import (
    _arg_metrics,
    _content_metrics,
    _extract_plan_meta,
    _is_plan_tool,
    _short_title,
    _tool_name,
    _tool_seq_from_id,
)


class HierarchyBuilder:
    """Incremental builder fed raw updates.jsonl records."""

    def __init__(self, *, max_rounds: int = MAX_ROUNDS_RETAINED) -> None:
        self.rounds: list[dict[str, Any]] = []
        self._open: Optional[dict[str, Any]] = None
        self._last_ctx: Optional[int] = None
        self._session_peak: Optional[int] = None
        # Context available for prompt-cache at next model call / next round
        self._cache_baseline: Optional[int] = None
        # Last compaction applied (for open-round bookkeeping)
        self._last_compact: Optional[dict[str, Any]] = None
        self.max_rounds = max(4, int(max_rounds))
        # Bumps when structure changes (for dashboard snapshot cache)
        self.revision: int = 0
        self._session_dir: Optional[Path] = None
        # hook_execution payloads (session-level; first-prompt ones feed bootstrap)
        self._hooks: list[dict[str, Any]] = []
        self._bootstrap_hooks: list[dict[str, Any]] = []
        # Hooks that fired between turns (completed → next user_message)
        self._pending_hooks: list[dict[str, Any]] = []
        # Between-turn cards — MUST clear on session switch (see reset)
        self._pending_recaps: list[dict[str, Any]] = []
        self._pending_compact: Optional[dict[str, Any]] = None
        self._seen_first_thought: bool = False
        self._session_bootstrap: Optional[dict[str, Any]] = None
        self._reasonings_cache: Optional[list[dict[str, Any]]] = None
        self._reasonings_mtime: Optional[float] = None
        self._reasonings_cursor: int = 0
        self._enc_stamp_sig: Optional[tuple] = None
        self._tool_results_cache: Optional[dict[str, dict[str, Any]]] = None
        self._tool_results_mtime: Optional[float] = None

    def _session_key(self) -> Optional[str]:
        """Stable id for the attached session dir (folder name)."""
        if self._session_dir is None:
            return None
        try:
            return Path(self._session_dir).name
        except Exception:
            return str(self._session_dir)

    def set_session_dir(self, session_dir: Optional[Path]) -> None:
        self._session_dir = Path(session_dir) if session_dir else None
        self._session_bootstrap = None
        self._reasonings_cache = None
        self._reasonings_mtime = None
        self._reasonings_cursor = 0
        self._tool_results_cache = None
        self._tool_results_mtime = None
        # Never carry fork cards / hooks across session dirs
        self._pending_hooks.clear()
        self._pending_recaps.clear()
        self._pending_compact = None

    def reset(self) -> None:
        self.rounds.clear()
        self._open = None
        self._last_ctx = None
        self._session_peak = None
        self._cache_baseline = None
        self._last_compact = None
        self._hooks.clear()
        self._bootstrap_hooks.clear()
        self._pending_hooks.clear()
        # Cross-session leak fix: pending recaps/compacts used to survive attach()
        # and paint foreign fork cards onto the next session's first rounds.
        self._pending_recaps.clear()
        self._pending_compact = None
        self._seen_first_thought = False
        self._session_bootstrap = None
        self._reasonings_cache = None
        self._reasonings_mtime = None
        self._reasonings_cursor = 0
        self._enc_stamp_sig = None
        self.revision += 1

    def _bump(self) -> None:
        self.revision += 1

    def _prune_rounds(self) -> None:
        overflow = len(self.rounds) - self.max_rounds
        if overflow > 0:
            del self.rounds[:overflow]

    # ------------------------------------------------------------------
    def feed_raw(self, raw: dict[str, Any]) -> None:
        params = raw.get("params") or {}
        update = params.get("update") or {}
        meta = params.get("_meta") or {}
        if not isinstance(meta, dict):
            meta = {}
        update_meta = update.get("_meta") or {}
        if not isinstance(update_meta, dict):
            update_meta = {}
        # Prefer params._meta, fall back to update._meta (stream often only has one)
        kind = update.get("sessionUpdate")
        if not kind:
            return
        # Any applied update invalidates dashboard snapshot cache
        self._bump()

        tt = meta.get("totalTokens")
        if not isinstance(tt, int):
            tt = update_meta.get("totalTokens")
        if isinstance(tt, int):
            if self._session_peak is None or tt > self._session_peak:
                self._session_peak = tt

        agent_ms = meta.get("agentTimestampMs")
        if agent_ms is None:
            agent_ms = update_meta.get("agentTimestampMs")
        prompt_id = (
            meta.get("promptId")
            or update_meta.get("promptId")
            or update.get("prompt_id")
        )
        stream_ms = meta.get("streamStartMs")
        if stream_ms is None:
            stream_ms = update_meta.get("streamStartMs")
        turn_ms = meta.get("turnStartMs")
        if turn_ms is None:
            turn_ms = update_meta.get("turnStartMs")

        if kind == "hook_execution":
            try:
                raw_chars = len(json.dumps(update, ensure_ascii=False))
            except (TypeError, ValueError):
                raw_chars = 64
            runs = update.get("runs") or []
            run_names = [
                str(x.get("name") or "")
                for x in runs
                if isinstance(x, dict)
            ]
            elapsed = sum(
                int((x.get("status") or {}).get("elapsed_ms") or 0)
                for x in runs
                if isinstance(x, dict)
            )
            event_name = update.get("event_name") or "hook"
            slot = _hook_slot(event_name)
            hook = {
                "kind": "hook",
                "event_name": event_name,
                "prompt_id": update.get("prompt_id") or prompt_id,
                "runs": runs,
                "run_names": [n for n in run_names if n],
                "chars": raw_chars,
                # Display-only size; never part of billed model In
                "tokens_est": max(1, count_chars_as_tokens(raw_chars) or 1),
                "elapsed_ms": elapsed or None,
                "slot": slot,
                "to_user": slot in ("user", "to_user"),
                "display_only": True,
            }
            hook["_raw"] = {
                "sessionUpdate": "hook_execution",
                "event_name": hook["event_name"],
                "prompt_id": hook["prompt_id"],
                "runs": runs,
            }
            self._hooks.append(hook)
            if not self._seen_first_thought:
                self._bootstrap_hooks.append(hook["_raw"])
            # user_prompt_submit fires after turn_completed (open is None) and
            # before user_message — buffer until the next round opens.
            if self._open is not None and not self._open.get("completed"):
                self._attach_hook_to_round(self._open, hook)
            else:
                self._pending_hooks.append(hook)
            return

        if kind == "agent_thought_chunk":
            self._seen_first_thought = True

        if kind == "user_message_chunk":
            text = _text_of(update)
            if self._open is None or self._open.get("completed"):
                self._start_round(prompt_id, turn_ms, agent_ms)
            r = self._open
            assert r is not None
            r["user_chars"] = int(r.get("user_chars") or 0) + len(text)
            if text:
                prev = r.get("user_preview") or ""
                r["user_preview"] = _preview((prev + text) if prev else text, 160)
            if prompt_id:
                r["prompt_id"] = prompt_id
            return

        if kind in (
            "agent_thought_chunk",
            "agent_message_chunk",
            "tool_call",
            "tool_call_update",
        ):
            if self._open is None:
                self._start_round(prompt_id, turn_ms, agent_ms)
            r = self._open
            assert r is not None
            if prompt_id and not r.get("prompt_id"):
                r["prompt_id"] = prompt_id
            if turn_ms and not r.get("turn_start_ms"):
                r["turn_start_ms"] = turn_ms

            step = self._ensure_model_step(r, stream_ms, agent_ms, tt)

            if kind == "agent_thought_chunk":
                self._note_ctx(r, step, tt, agent_ms)
                text = _text_of(update)
                # Always track thought chunks (even empty summary — encrypted lives in history)
                step["thought_chunks"] = int(step.get("thought_chunks") or 0) + 1
                if text:
                    buf = (step.get("thought_summary_text") or "") + text
                    step["thought_summary_text"] = buf
                    step["thought_chars"] = len(buf)
                    step["thought_summary_chars"] = len(buf)
                    # Tokens counted once at finalize (full string → correct BPE)
                    if not step.get("thought_preview"):
                        step["thought_preview"] = _preview(text, 80)
                else:
                    # Keep a presence flag so UI always has a thought row
                    step["thought_chars"] = int(step.get("thought_chars") or 0)
                    step.setdefault("thought_preview", step.get("thought_preview"))
            elif kind == "agent_message_chunk":
                self._note_ctx(r, step, tt, agent_ms)
                text = _text_of(update)
                step["message_chunks"] = int(step.get("message_chunks") or 0) + 1
                if text:
                    buf = (step.get("message_text") or "") + text
                    step["message_text"] = buf
                    step["message_chars"] = len(buf)
                    step["message_preview"] = _preview(buf, 120)
                else:
                    step["message_chars"] = int(step.get("message_chars") or 0)
            elif kind == "tool_call":
                self._on_tool_call(step, r, update, update_meta, meta, tt, agent_ms)
            elif kind == "tool_call_update":
                self._on_tool_update(step, r, update, update_meta, meta, tt, agent_ms)
            return

        if kind == "turn_completed":
            if self._open is None:
                self._start_round(prompt_id or update.get("prompt_id"), None, agent_ms)
            r = self._open
            assert r is not None
            if prompt_id or update.get("prompt_id"):
                r["prompt_id"] = prompt_id or update.get("prompt_id")
            usage = update.get("usage") or {}
            r["completed"] = True
            r["stop_reason"] = update.get("stop_reason")
            r["usage_raw"] = usage
            # End-of-round clock (for idle gap → next round)
            if isinstance(agent_ms, (int, float)):
                r["completed_ms"] = int(agent_ms)
            elif isinstance(update_meta.get("agentTimestampMs"), (int, float)):
                r["completed_ms"] = int(update_meta["agentTimestampMs"])
            self._finalize_round(r)
            compact_round_inplace(r)
            # After a completed round, cache baseline ≈ real end of last LLM call
            # (not a stale mid-stream under-count). R2+ call1 Cached must resume here.
            end = r.get("context_end")
            for s in r.get("model_steps") or []:
                if isinstance(s, dict) and isinstance(s.get("context_end"), int):
                    if end is None or s["context_end"] > end:
                        end = s["context_end"]
            # Prefer peak if higher (tools may advance past last thought snap)
            peak = r.get("context_peak")
            if isinstance(peak, int) and (end is None or peak > end):
                end = peak
            if isinstance(end, int):
                self._cache_baseline = end
                self._last_ctx = end
                r["context_end"] = end
            self.rounds.append(r)
            self._prune_rounds()
            self._open = None
            self._bump()
            return

        if kind == "auto_compact_completed":
            self._on_compact(update, agent_ms)
            return

        if kind == "session_recap":
            self._on_recap(update, agent_ms)
            return

        if kind == "compaction_checkpoint":
            # Lightweight note only; real size change is auto_compact_completed
            note = {
                "kind": "compaction_checkpoint",
                "checkpoint_id": update.get("checkpoint_id"),
                "prompt_index_at_compaction": update.get("prompt_index_at_compaction"),
            }
            target = self._open or (self.rounds[-1] if self.rounds else None)
            if target is not None:
                target.setdefault("notes", []).append(note)
            return

    # ------------------------------------------------------------------
    def _on_recap(self, update: dict[str, Any], agent_ms: Any) -> None:
        return _rc_on_recap(self, update, agent_ms)

    def _recap_prompt_info(
        self, summary: str
    ) -> tuple[int, str, Optional[str]]:
        return _rc_recap_prompt_info(self, summary)

    def _on_compact(self, update: dict[str, Any], agent_ms: Any) -> None:
        return _rc_on_compact(self, update, agent_ms)

    def _attach_hook_to_round(self, r: dict[str, Any], hook: dict[str, Any]) -> None:
        """Place a hook_execution on the round so the UI can always list it.

        Slots (by event_name / timing):
          • user     — user_prompt_submit (+ future user_prompt_*) after prompt
          • to_user  — stop / session_end on last model step (→ user, not In)
          • stream   — mid-call on current step, or hooks_before_llm if no step yet
        Unknown future events fall into stream by default so they still appear.
        """
        if not isinstance(r, dict) or not isinstance(hook, dict):
            return
        hl = r.setdefault("hooks", [])
        if isinstance(hl, list):
            # Avoid double-append if caller already pushed
            if hook not in hl:
                hl.append(hook)
        slot = str(hook.get("slot") or _hook_slot(hook.get("event_name")))
        hook["slot"] = slot
        if slot == "user":
            uh = r.setdefault("user_hooks", [])
            if isinstance(uh, list) and hook not in uh:
                uh.append(hook)
            return
        steps = r.get("model_steps") or []
        if steps:
            steps[-1].setdefault("hooks", []).append(hook)
            return
        if slot == "to_user":
            # No step yet (rare) — keep visible at round end
            r.setdefault("hooks_after", []).append(hook)
        else:
            r.setdefault("hooks_before_llm", []).append(hook)

    def _start_round(
        self,
        prompt_id: Optional[str],
        turn_ms: Any,
        agent_ms: Any,
    ) -> None:
        if self._open is not None and not self._open.get("completed"):
            self._finalize_round(self._open)
            self._open["completed"] = False
            self.rounds.append(self._open)

        # Prefer post-compact / cache baseline over a stale high watermark
        start_ctx = self._last_ctx
        if isinstance(self._cache_baseline, int):
            # After compact, baseline is the true window size
            if start_ctx is None or (
                isinstance(start_ctx, int) and self._cache_baseline < start_ctx
            ):
                # only prefer baseline when it is a real drop (compact) or equal
                if self._last_compact and isinstance(
                    self._last_compact.get("tokens_after"), int
                ):
                    start_ctx = self._last_compact["tokens_after"]
                elif start_ctx is None:
                    start_ctx = self._cache_baseline

        # Idle gap: time since previous round completed (KV often expires on long gaps)
        idle_gap_ms = None
        if self.rounds:
            prev = self.rounds[-1]
            prev_end = prev.get("completed_ms")
            start_ms = agent_ms if isinstance(agent_ms, (int, float)) else None
            if isinstance(prev_end, (int, float)) and isinstance(start_ms, (int, float)):
                idle_gap_ms = max(0, int(start_ms) - int(prev_end))

        self._open = {
            "index": len(self.rounds) + 1,
            "prompt_id": prompt_id,
            "turn_start_ms": turn_ms,
            "started_ms": agent_ms,
            "idle_gap_ms": idle_gap_ms,
            "user_preview": "",
            "user_chars": 0,
            "model_steps": [],
            "context_start": start_ctx,
            "context_end": start_ctx,
            "context_peak": start_ctx,
            "context_delta": 0,
            "completed": False,
            "notes": [],
            "compactions": [],
            "cache_baseline_at_start": self._cache_baseline,
            "hooks": [],
            "user_hooks": [],
            "hooks_before_llm": [],
        }
        # Hooks buffered between turns (esp. user_prompt_submit before user_message)
        if self._pending_hooks:
            for h in self._pending_hooks:
                self._attach_hook_to_round(self._open, h)
            self._pending_hooks = []
        _attach_pending_recap_compact(self)

    def _ensure_model_step(
        self,
        round_: dict[str, Any],
        stream_ms: Any,
        agent_ms: Any,
        tt: Any,
    ) -> dict[str, Any]:
        steps: list = round_["model_steps"]
        if steps:
            last = steps[-1]
            if stream_ms is not None and last.get("stream_start_ms") == stream_ms:
                return last
            if stream_ms is None:
                return last

        # Closing previous step: fold gap (late tokens visible at next stream)
        if steps and isinstance(tt, int):
            prev = steps[-1]
            prev_end = prev.get("context_end")
            if isinstance(prev_end, int) and tt > prev_end:
                prev["late_context_delta"] = tt - prev_end
                prev["context_end"] = tt
                if prev.get("context_peak") is None or tt > prev["context_peak"]:
                    prev["context_peak"] = tt
                # Cursor follows
                self._last_ctx = tt
            elif prev_end is None and isinstance(tt, int):
                prev["context_end"] = tt

        step = {
            "index": len(steps) + 1,
            "stream_start_ms": stream_ms,
            "started_ms": agent_ms,
            "context_start": tt if isinstance(tt, int) else self._last_ctx,
            "context_end": tt if isinstance(tt, int) else self._last_ctx,
            "context_peak": tt if isinstance(tt, int) else self._last_ctx,
            "context_delta": 0,
            "late_context_delta": 0,
            "model_emit_delta": 0,
            "model_emit_before": None,
            "model_emit_after": None,
            "tools_phase_start": None,
            "thought_chars": 0,
            "thought_chunks": 0,
            "message_chars": 0,
            "message_chunks": 0,
            "thought_preview": None,
            "message_preview": None,
            "tools": [],
            "children": [],
            # serial cursor for tool result attribution within this step
            "_tool_cursor": tt if isinstance(tt, int) else self._last_ctx,
        }
        if isinstance(tt, int):
            self._last_ctx = tt
            if round_.get("context_start") is None:
                round_["context_start"] = tt
            round_["context_end"] = tt
            peak = round_.get("context_peak")
            if peak is None or tt > peak:
                round_["context_peak"] = tt
        steps.append(step)
        return step

    def _note_ctx(
        self,
        round_: dict[str, Any],
        step: dict[str, Any],
        tt: Any,
        agent_ms: Any,
    ) -> None:
        if not isinstance(tt, int):
            return
        if round_.get("context_start") is None:
            round_["context_start"] = tt
        round_["context_end"] = tt
        peak = round_.get("context_peak")
        if peak is None or tt > peak:
            round_["context_peak"] = tt
        if step.get("context_start") is None:
            step["context_start"] = tt
        step["context_end"] = tt
        sp = step.get("context_peak")
        if sp is None or tt > sp:
            step["context_peak"] = tt
        # Advance tool cursor only if not already past (tools own their growth)
        cursor = step.get("_tool_cursor")
        if cursor is None or tt > cursor:
            # thought/message may advance context without tools
            step["_tool_cursor"] = tt
        self._last_ctx = tt

    def _on_tool_call(
        self,
        step: dict[str, Any],
        round_: dict[str, Any],
        update: dict[str, Any],
        update_meta: dict[str, Any],
        meta: dict[str, Any],
        tt: Any,
        agent_ms: Any,
    ) -> None:
        tid = update.get("toolCallId")
        name = _tool_name(update, update_meta) or "tool"
        title = _short_title(update.get("title"))

        # First tool declare in this step: jump thought→declare is model_emit
        if step.get("tools_phase_start") is None and isinstance(tt, int):
            step["tools_phase_start"] = tt
            cs = step.get("context_start")
            cursor = step.get("_tool_cursor")
            base = cursor if isinstance(cursor, int) else cs
            if isinstance(base, int) and tt > base:
                step["model_emit_delta"] = tt - base
                step["model_emit_before"] = base
                step["model_emit_after"] = tt
            step["_tool_cursor"] = tt
            self._last_ctx = tt
            # Update step/round peaks without attributing to a tool
            if step.get("context_start") is None:
                step["context_start"] = base if isinstance(base, int) else tt
            step["context_end"] = tt
            if step.get("context_peak") is None or tt > step["context_peak"]:
                step["context_peak"] = tt
            if round_.get("context_start") is None:
                round_["context_start"] = step.get("context_start")
            round_["context_end"] = tt
            if round_.get("context_peak") is None or tt > round_["context_peak"]:
                round_["context_peak"] = tt
        elif isinstance(tt, int):
            # Subsequent declares usually same tt; keep cursor
            if step.get("_tool_cursor") is None:
                step["_tool_cursor"] = tt
            if tt > (step.get("_tool_cursor") or 0):
                # Rare: more model emit mid-batch
                step["model_emit_delta"] = int(step.get("model_emit_delta") or 0) + (
                    tt - int(step["_tool_cursor"])
                )
                step["_tool_cursor"] = tt
                self._last_ctx = tt
                step["context_end"] = tt

        raw_in = update.get("rawInput") or {}
        path = None
        offset = None
        limit = None
        if isinstance(raw_in, dict):
            path = (
                raw_in.get("target_file")
                or raw_in.get("path")
                or raw_in.get("file_path")
            )
            offset = raw_in.get("offset")
            limit = raw_in.get("limit")
        xai = update_meta.get("x.ai/tool") if isinstance(update_meta, dict) else None
        if isinstance(xai, dict):
            inp = xai.get("input") or {}
            if isinstance(inp, dict):
                path = path or inp.get("path") or inp.get("target_file") or inp.get(
                    "file_path"
                )
                offset = offset if offset is not None else inp.get("offset")
                limit = limit if limit is not None else inp.get("limit")
                if not raw_in:
                    raw_in = inp
        args = _arg_metrics(raw_in if isinstance(raw_in, (dict, str)) else None)
        plan = _extract_plan_meta(
            name=name, title=title, raw_in=raw_in, raw_out=None
        )

        tool = {
            "kind": "tool",
            "tool_call_id": tid,
            "tool_seq": _tool_seq_from_id(tid),
            "name": name,
            "title": title,
            # filled when results grow context
            "context_before": None,
            "context_after": None,
            "context_delta": 0,
            "tt_delta_observed": 0,
            "declare_ctx": tt if isinstance(tt, int) else step.get("_tool_cursor"),
            "status": "started",
            "path": path,
            "offset": offset,
            "limit": limit,
            "result_chars": 0,
            "result_lines": 0,
            "result_tokens_est": 0,
            "result_preview": None,
            "arg_chars": int(args.get("arg_chars") or 0),
            "arg_tokens_est": int(args.get("arg_tokens_est") or 0),
            "plan": plan,
            "is_plan": bool(plan),
        }
        step["tools"].append(tool)

    def _on_tool_update(
        self,
        step: dict[str, Any],
        round_: dict[str, Any],
        update: dict[str, Any],
        update_meta: dict[str, Any],
        meta: dict[str, Any],
        tt: Any,
        agent_ms: Any,
    ) -> None:
        tid = update.get("toolCallId")
        tools: list = step["tools"]
        tool = None
        for t in reversed(tools):
            if tid and t.get("tool_call_id") == tid:
                tool = t
                break
        if tool is None and tools:
            tool = tools[-1]
        if tool is None:
            self._on_tool_call(
                step, round_, update, update_meta, meta, tt, agent_ms
            )
            tool = step["tools"][-1] if step["tools"] else None
            if tool is None:
                return

        title = _short_title(update.get("title"))
        if title:
            tool["title"] = title
        name = _tool_name(update, update_meta)
        if name and (tool.get("name") in (None, "tool") or name != "tool"):
            tool["name"] = name
        status = update.get("status")
        if status:
            tool["status"] = status
        elif title or update.get("kind"):
            tool["status"] = "running"

        # Result payload metrics (chars/lines) — more reliable than totalTokens lag
        metrics = _content_metrics(update)
        if metrics.get("result_chars"):
            tool["result_chars"] = max(int(tool.get("result_chars") or 0), metrics["result_chars"])
            tool["result_lines"] = max(int(tool.get("result_lines") or 0), metrics["result_lines"])
            tool["result_tokens_est"] = max(
                int(tool.get("result_tokens_est") or 0), metrics["result_tokens_est"]
            )
            if metrics.get("result_preview") and not tool.get("result_preview"):
                tool["result_preview"] = metrics["result_preview"]
        if metrics.get("path") and not tool.get("path"):
            tool["path"] = metrics["path"]
        if metrics.get("offset") is not None:
            tool["offset"] = metrics["offset"]
        if metrics.get("limit") is not None:
            tool["limit"] = metrics["limit"]
        if metrics.get("arg_chars"):
            tool["arg_chars"] = max(int(tool.get("arg_chars") or 0), int(metrics["arg_chars"]))
            tool["arg_tokens_est"] = max(
                int(tool.get("arg_tokens_est") or 0), int(metrics.get("arg_tokens_est") or 0)
            )

        # Plan (todo_write): refresh create/modify + step statuses from rawIn/rawOut
        raw_in = update.get("rawInput") or {}
        raw_out = update.get("rawOutput")
        plan = _extract_plan_meta(
            name=tool.get("name"),
            title=tool.get("title") or title,
            raw_in=raw_in if raw_in else None,
            raw_out=raw_out,
        )
        if plan:
            prev = tool.get("plan") if isinstance(tool.get("plan"), dict) else {}
            # completed event often has empty rawInput — keep prior mode
            if prev.get("mode") and not (
                isinstance(raw_in, dict) and ("merge" in raw_in or raw_in.get("todos"))
            ):
                plan = dict(plan)
                plan["mode"] = prev.get("mode") or plan.get("mode")
                if not plan.get("step_count") and prev.get("step_count"):
                    plan["step_count"] = prev["step_count"]
            # First declare with merge=false stays create even if title later says Updating
            if prev.get("mode") == "create" and plan.get("mode") == "modify":
                if isinstance(raw_in, dict) and raw_in.get("merge") is False:
                    plan = dict(plan)
                    plan["mode"] = "create"
            tool["plan"] = plan
            tool["is_plan"] = True
        elif not _is_plan_tool(tool.get("name"), tool.get("title") or title):
            tool.pop("plan", None)
            tool["is_plan"] = False

        # Serial attribution: only when totalTokens moves forward
        if isinstance(tt, int):
            cursor = step.get("_tool_cursor")
            if cursor is None:
                cursor = tool.get("declare_ctx")
                if cursor is None:
                    cursor = step.get("context_start")
            if isinstance(cursor, int) and tt > cursor:
                growth = tt - cursor
                if tool.get("context_before") is None:
                    tool["context_before"] = cursor
                tool["context_after"] = tt
                tool["tt_delta_observed"] = int(tool.get("tt_delta_observed") or 0) + growth
                tool["context_delta"] = int(tool.get("tt_delta_observed") or 0)
                step["_tool_cursor"] = tt
                self._last_ctx = tt
                step["context_end"] = tt
                if step.get("context_peak") is None or tt > step["context_peak"]:
                    step["context_peak"] = tt
                round_["context_end"] = tt
                if round_.get("context_peak") is None or tt > round_["context_peak"]:
                    round_["context_peak"] = tt
            elif status == "completed":
                if tool.get("context_before") is None:
                    tool["context_before"] = cursor if isinstance(cursor, int) else tt
                if tool.get("context_after") is None:
                    tool["context_after"] = tool["context_before"]
                tool["context_delta"] = int(tool.get("tt_delta_observed") or tool.get("context_delta") or 0)
                if isinstance(tt, int):
                    step["context_end"] = max(step.get("context_end") or 0, tt) or tt
                    self._last_ctx = tt if self._last_ctx is None else max(self._last_ctx, tt)

    def _finalize_round(self, r: dict[str, Any]) -> None:
        return _finalize_round(self, r)

    def _attach_prev_llm_answer(self, r: dict[str, Any]) -> None:
        return _cm_attach_prev_llm_answer(self, r)

    def _inject_system_message_residual(self, r: dict[str, Any], recon: dict[str, Any]) -> None:
        return _inject_system_message_residual(self, r, recon)

    def _merge_bootstrap_into_breakdown(self, r: dict[str, Any]) -> None:
        return _merge_bootstrap_into_breakdown(self, r)

    def _load_reasonings_fresh(self) -> list[dict[str, Any]]:
        return _load_reasonings_fresh(self)

    def _load_tool_results_fresh(self) -> dict[str, dict[str, Any]]:
        return _load_tool_results_fresh(self)

    def _stamp_tool_chat_results(self, tools: list[dict[str, Any]]) -> None:
        return _stamp_tool_chat_results(self, tools)

    @staticmethod
    def _stamp_step_reasoning(step: dict[str, Any], rs: dict[str, Any]) -> None:
        return _stamp_step_reasoning(step, rs)

    def _enrich_session_thoughts(self) -> None:
        return _enrich_session_thoughts(self)

    def _patch_reasoning_chars_on_trees(self) -> None:
        return _patch_reasoning_chars_on_trees(self)

    def _enrich_round_thoughts(self, r: dict[str, Any]) -> None:
        return _enrich_round_thoughts(self, r)

    def _detect_context_reread(self, r: dict[str, Any]) -> Optional[dict[str, Any]]:
        return _cm_detect_context_reread(self, r)

    def _compute_idle_gap_ms(self, r: dict[str, Any]) -> Optional[int]:
        return _cm_compute_idle_gap_ms(self, r)

    def _apply_session_restart_cache_miss(self, r: dict[str, Any]) -> None:
        return _cm_apply_session_restart_cache_miss(self, r)

    def _price_bootstrap_prompts(self, r: dict[str, Any]) -> None:
        return _price_bootstrap_prompts(self, r)

    def _finalize_step(self, step: dict[str, Any]) -> None:
        return _finalize_step(self, step)

    def _attach_step_estimates(self, r: dict[str, Any]) -> None:
        return _attach_step_estimates(self, r)

    def _fill_compact_cost(self, r: dict[str, Any]) -> None:
        return _rc_fill_compact_cost(self, r)

    def current_open(self) -> Optional[dict[str, Any]]:
        if self._open is None:
            return None
        # Shallow structural copy only — finalize mutates a dedicated live snapshot
        r = _shallow_round_copy(self._open)
        self._finalize_round(r)
        compact_round_inplace(r)
        return r

    def _enc_stamp_signature(self) -> tuple:
        return _enc_stamp_signature(self)

    def _reprice_completed_rounds(self) -> None:
        return _reprice_completed_rounds(self)

    def snapshot_rounds(self, *, include_open: bool = True) -> list[dict[str, Any]]:
        """Return rounds for API. Completed rounds are referenced as-is (already compact)."""
        # Remap encrypted_content from chat_history; re-price when stamps change
        # so Reasoning nodes always show real enc char counts.
        try:
            self._enrich_session_thoughts()
            sig = self._enc_stamp_signature()
            if self.rounds and sig != self._enc_stamp_sig:
                self._reprice_completed_rounds()
                self._enc_stamp_sig = sig
            else:
                self._patch_reasoning_chars_on_trees()
        except Exception:
            pass
        out: list[dict[str, Any]] = list(self.rounds)
        if include_open and self._open is not None:
            live = _shallow_round_copy(self._open)
            self._finalize_round(live)
            compact_round_inplace(live)
            out.append(live)
        return out
