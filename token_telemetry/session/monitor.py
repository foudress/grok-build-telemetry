"""Live session state engine (SessionMonitor)."""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional

from token_telemetry.pricing import (
    estimate_cost_usd,
    estimate_from_usage,
    pick_tier,
    pricing_model_scope,
    pricing_payload,
    ticks_to_usd,
)
from token_telemetry.hierarchy import HierarchyBuilder, MAX_ROUNDS_RETAINED
from token_telemetry.session.discover import (
    SESSIONS_ROOT,
    list_sessions_for_ui,
    read_active_session_ids,
    resolve_session_dir,
)
from token_telemetry.session.subagents import (
    attach_subagents_after_steps,
    capture_child_sys,
    child_ids_from_session_update,
    collect_child_ids_from_round,
    collect_resume_alias_from_round,
    ensure_lineage_subagent_cards,
    is_subagent_session,
    latest_in_resume_chain,
    read_session_summary,
    root_subagent_id,
    sibling_session_dir,
)
from token_telemetry.hierarchy.gen_rate import mean_gen_rate



from token_telemetry.session.enrich import (
    _enrich_system_prompt,
    _enrich_user_prompt,
    _mean_round_tps,
    _round_api_bill,
    _sum_child_tab_estimates,
    _sum_rounds_estimate,
    enrich_round_usage,
)
from token_telemetry.session.wire import _slim_signals, _slim_wire
from token_telemetry.session.children import (
    _ChildWatch,
    child_lineage,
    sync_children,
)

# RAM guards
MAX_TURNS = 100_000
MAX_CONTEXT_POINTS = 20_000
MAX_READ_CHUNK = 1_500_000  # bytes per tick when catching up
# Wire window = builder retention so Compact · auto on last rounds is visible.
API_ROUNDS = MAX_ROUNDS_RETAINED


class SessionMonitor:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.session_dir: Optional[Path] = None
        self.session_id: Optional[str] = None
        # When set, auto-follow of newest active session is disabled
        self.pinned_session_id: Optional[str] = None
        self._updates_path: Optional[Path] = None
        self._updates_offset = 0
        self._events_path: Optional[Path] = None
        self._events_offset = 0

        self.context_series: deque[dict[str, Any]] = deque(maxlen=MAX_CONTEXT_POINTS)
        self.feed: deque[dict[str, Any]] = deque(maxlen=60)
        self.turns: list[dict[str, Any]] = []
        self._turn_seq: int = 0
        self.hierarchy = HierarchyBuilder()
        self._turn_peak_ctx: Optional[int] = None
        self.signals: dict[str, Any] = {}
        self.phase: Optional[str] = None
        self.live: dict[str, Any] = {
            "context_tokens": None,
            "context_tokens_stream": None,
            "context_tokens_ui": None,
            "phase": None,
            "last_kind": None,
            "prompt_id": None,
            "tier": None,
            "chars_per_sec": None,
            "model": None,
        }
        # stream phase char rate
        self._stream_kind: Optional[str] = None
        self._stream_start_ms: Optional[float] = None
        self._stream_chars = 0
        self._last_stream_ms: Optional[float] = None
        self._last_ctx_logged: Optional[int] = None
        self.error: Optional[str] = None
        self.bootstrapped = False
        # Cached /api/state payload (avoid re-json every browser poll)
        self._snap_rev: int = -1
        self._snap_bytes: Optional[bytes] = None
        self._snap_etag: Optional[str] = None
        self._snap_sig_key: Optional[tuple] = None
        self._children: dict[str, "_ChildWatch"] = {}
        self._spawned_child_ids: list[str] = []

    def attach(self, session_dir: Path, *, pin: bool = False) -> None:
        """Attach to a session. Call with lock held OR not — re-entrant safe via unlocked path."""
        with self.lock:
            self._attach_unlocked(session_dir, pin=pin)

    def _attach_unlocked(self, session_dir: Path, *, pin: bool = False) -> None:
        self.session_dir = session_dir
        self.session_id = session_dir.name
        if pin:
            self.pinned_session_id = session_dir.name
        self._updates_path = session_dir / "updates.jsonl"
        self._events_path = session_dir / "events.jsonl"
        self._updates_offset = 0
        self._events_offset = 0
        self.context_series.clear()
        self.feed.clear()
        self.turns.clear()
        self._turn_seq = 0
        self.hierarchy.reset()
        self.hierarchy.set_session_dir(session_dir)
        self._turn_peak_ctx = None
        self.signals = {}
        self.phase = None
        self.live = {
            "context_tokens": None,
            "context_tokens_stream": None,
            "context_tokens_ui": None,
            "phase": None,
            "last_kind": None,
            "prompt_id": None,
            "tier": None,
            "chars_per_sec": None,
            "model": None,
        }
        self._stream_kind = None
        self._stream_start_ms = None
        self._stream_chars = 0
        self._last_stream_ms = None
        self._last_ctx_logged = None
        self.bootstrapped = False
        self.error = None
        self._snap_rev = -1
        self._snap_bytes = None
        self._snap_etag = None
        self._snap_sig_key = None
        self._children = {}
        self._spawned_child_ids = []
        self.live["model"] = getattr(self.hierarchy, "_pricing_model", None)

    def select_session(self, session_id: Optional[str]) -> dict[str, Any]:
        """
        Pin dashboard to a session id, or pass None / '' to follow active again.
        """
        with self.lock:
            if not session_id:
                self.pinned_session_id = None
                d = resolve_session_dir()
                if not d:
                    return {"ok": False, "error": "no session available"}
                self._attach_unlocked(d, pin=False)
                out = {"ok": True, "session_id": d.name, "pinned": False}
            # /telemetry opens ?session= after the server already pinned that id.
            # Re-attach + full catch-up would replay the same jsonl twice.
            elif (
                self.pinned_session_id == session_id
                and self.session_id == session_id
                and self.session_dir is not None
                and (self.session_dir / "updates.jsonl").is_file()
                and self.bootstrapped
            ):
                return {
                    "ok": True,
                    "session_id": session_id,
                    "pinned": True,
                    "already": True,
                }
            else:
                d = resolve_session_dir(session_id)
                if not d:
                    return {"ok": False, "error": f"unknown session {session_id}"}
                self._attach_unlocked(d, pin=True)
                out = {"ok": True, "session_id": d.name, "pinned": True}
        # Drain outside the attach critical section: release between chunks so
        # concurrent /api/state is not wedged for the whole replay (D/W/M open).
        self._catch_up_yielding()
        return out

    def rebuild_current(self) -> dict[str, Any]:
        """Drop RAM hierarchy and replay updates.jsonl (Reset calc).

        Disk calc-cache is wiped separately. Without this, a closed session
        stays on the tree built at attach even after accounting code changes.
        """
        with self.lock:
            d = self.session_dir
            if d is None:
                self._snap_bytes = None
                self._snap_etag = None
                self._snap_rev = -1
                self._snap_sig_key = None
                return {"rebuilt": False, "session_id": None}
            pin = self.pinned_session_id is not None
            self._attach_unlocked(d, pin=pin)
            sid = self.session_id
        self._catch_up_yielding()
        with self.lock:
            self._snap_bytes = None
            self._snap_etag = None
            self._snap_rev = -1
            self._snap_sig_key = None
            return {"rebuilt": True, "session_id": sid}

    def _catch_up_yielding(self) -> None:
        """Drain updates.jsonl; release the monitor lock between chunks."""
        for _ in range(256):
            with self.lock:
                path = self._updates_path
                if path is None or not path.is_file():
                    return
                try:
                    size = path.stat().st_size
                except OSError:
                    return
                if int(self._updates_offset) >= int(size):
                    self._catch_up_children_unlocked()
                    return
                before = self._updates_offset
                self.tick()
                if self._updates_offset <= before:
                    self._catch_up_children_unlocked()
                    return
            # Lock released — other HTTP handlers can snapshot mid-catch-up.
        with self.lock:
            self._catch_up_children_unlocked()

    def _catch_up_unlocked(self) -> None:
        """Drain updates.jsonl past MAX_READ_CHUNK (lock already held)."""
        for _ in range(256):
            path = self._updates_path
            if path is None or not path.is_file():
                return
            try:
                size = path.stat().st_size
            except OSError:
                return
            if int(self._updates_offset) >= int(size):
                # Parent caught up — finish any still-streaming child watches.
                self._catch_up_children_unlocked()
                return
            before = self._updates_offset
            self.tick()
            if self._updates_offset <= before:
                self._catch_up_children_unlocked()
                return
        self._catch_up_children_unlocked()

    def _catch_up_children_unlocked(self) -> None:
        """Drain each sub-agent watch so Sub tabs are complete on first paint."""
        for _ in range(128):
            progressed = False
            for watch in list(self._children.values()):
                before = int(getattr(watch, "_updates_offset", 0) or 0)
                watch.tick(self._read_new_lines)
                if int(getattr(watch, "_updates_offset", 0) or 0) > before:
                    progressed = True
            if not progressed:
                return

    def _read_new_lines(self, path: Optional[Path], offset: int) -> tuple[list[str], int]:
        """Read new complete lines; cap bytes per call to avoid multi-MB spikes."""
        if path is None or not path.is_file():
            return [], offset
        try:
            size = path.stat().st_size
        except OSError:
            return [], offset
        if size < offset:
            offset = 0
        if size == offset:
            return [], offset
        to_read = min(size - offset, MAX_READ_CHUNK)
        with path.open("rb") as f:
            f.seek(offset)
            raw = f.read(to_read)
        if not raw:
            return [], offset
        # Only consume complete lines; leave partial trailing line for next tick
        if not raw.endswith(b"\n"):
            nl = raw.rfind(b"\n")
            if nl < 0:
                # whole chunk is incomplete line — wait for more unless file ended
                if offset + len(raw) >= size:
                    return [], offset
                return [], offset
            raw = raw[: nl + 1]
        new_offset = offset + len(raw)
        try:
            text = raw.decode("utf-8", errors="replace")
        except Exception:
            text = raw.decode("latin-1", errors="replace")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        return lines, new_offset

    def _push_feed(self, kind: str, detail: str, t_label: str = "") -> None:
        self.feed.append({"kind": kind, "detail": detail[:240], "t": t_label})

    def _enrich_round_usage(self, round_: dict[str, Any]) -> dict[str, Any]:
        return enrich_round_usage(self, round_)

    def _handle_update(self, raw: dict[str, Any]) -> None:
        params = raw.get("params") or {}
        update = params.get("update") or {}
        meta = params.get("_meta") or {}
        kind = update.get("sessionUpdate")
        if not kind:
            return

        # hierarchical reconstruction (round → model step → tools)
        self.hierarchy.feed_raw(raw)
        self.live["model"] = getattr(self.hierarchy, "_pricing_model", None)
        for uid in child_ids_from_session_update(update):
            if uid not in self._spawned_child_ids:
                self._spawned_child_ids.append(uid)

        agent_ms = meta.get("agentTimestampMs")
        t_label = ""
        if isinstance(agent_ms, (int, float)):
            # show mm:ss from epoch ms fractional
            sec = int(agent_ms / 1000) % 86400
            t_label = f"{sec // 3600:02d}:{(sec % 3600) // 60:02d}:{sec % 60:02d}"

        self.live["last_kind"] = kind
        pid = meta.get("promptId") or update.get("prompt_id")
        if pid:
            self.live["prompt_id"] = pid

        tt = meta.get("totalTokens")
        if isinstance(tt, int):
            self.live["context_tokens_stream"] = tt
            # Prefer UI/signals when available for "now"; stream is for series + peaks
            if self.live.get("context_tokens_ui") is None:
                self.live["context_tokens"] = tt
            else:
                # keep card on UI context; stream still recorded
                pass
            if self.live.get("context_tokens") is None:
                self.live["context_tokens"] = tt
            self.live["tier"] = pick_tier(
                int(self.live.get("context_tokens_ui") or tt)
            )["name"]
            # Sample context series (skip identical consecutive values)
            if self._last_ctx_logged is None or tt != self._last_ctx_logged:
                self.context_series.append(
                    {"t": agent_ms or time.time() * 1000, "v": tt}
                )
                self._last_ctx_logged = tt
            if self._turn_peak_ctx is None or tt > self._turn_peak_ctx:
                self._turn_peak_ctx = tt

        content = update.get("content") or {}
        text = ""
        if isinstance(content, dict):
            text = content.get("text") or ""
        elif isinstance(content, str):
            text = content

        if kind in ("agent_thought_chunk", "agent_message_chunk"):
            stream_start = meta.get("streamStartMs")
            if self._stream_kind != kind or (
                isinstance(stream_start, (int, float))
                and self._stream_start_ms != stream_start
            ):
                self._stream_kind = kind
                self._stream_start_ms = stream_start if isinstance(stream_start, (int, float)) else agent_ms
                self._stream_chars = 0
            self._stream_chars += len(text)
            if isinstance(agent_ms, (int, float)) and self._stream_start_ms:
                dur = max(1.0, agent_ms - self._stream_start_ms) / 1000.0
                self.live["chars_per_sec"] = round(self._stream_chars / dur, 1)
            preview = text.replace("\n", " ")[:80]
            self._push_feed(
                kind,
                f"ctx={tt} chunk={meta.get('chunkId')} {len(text)}ch · {preview}",
                t_label,
            )
        elif kind == "user_message_chunk":
            self._turn_peak_ctx = None
            self._push_feed(kind, f"user {len(text)} chars · {text.replace(chr(10),' ')[:80]}", t_label)
        elif kind == "tool_call":
            title = update.get("title") or "tool"
            if isinstance(title, str) and len(title) > 80:
                title = title[:77] + "…"
            self._push_feed(kind, f"{title} · ctx={tt}", t_label)
        elif kind == "tool_call_update":
            title = update.get("title") or "tool_update"
            if isinstance(title, str) and len(title) > 80:
                title = title[:77] + "…"
            if update.get("status") == "completed" or (
                isinstance(title, str) and title and not title.startswith("tool")
            ):
                # sparse feed: only when title present or completed
                if update.get("title"):
                    self._push_feed(kind, f"{title} · ctx={tt}", t_label)
        elif kind == "turn_completed":
            usage = update.get("usage") or {}
            # peak from hierarchy round just closed (last completed)
            peak = self._turn_peak_ctx
            last_round = self.hierarchy.rounds[-1] if self.hierarchy.rounds else None
            if last_round:
                peak = last_round.get("context_peak") or peak
            ce = (last_round or {}).get("context_end")
            if isinstance(peak, int) and isinstance(ce, int) and ce > 0 and peak > 2 * ce:
                peak = None
            est = estimate_from_usage(
                usage,
                peak_context_tokens=peak,
                model=(last_round or {}).get("model_family")
                or getattr(self.hierarchy, "_pricing_model", None),
            )
            # Prefer sum of reconstructed model-step list prices
            step_est = None
            if last_round and (last_round.get("step_usage") or {}).get("totals"):
                step_est = (last_round["step_usage"]["totals"] or {}).get("cost_usd")
            estimate_usd = float(step_est) if step_est is not None else est["cost_usd"]["total"]
            # R1 peeled white total (excludes System card)
            if last_round and isinstance(last_round.get("estimate_usd"), (int, float)):
                bd0 = last_round.get("breakdown") or {}
                if bd0.get("round_total_peeled_system") or last_round.get("system_prompt"):
                    estimate_usd = float(last_round["estimate_usd"])
            ticks = usage.get("costUsdTicks")
            official = ticks_to_usd(ticks) if ticks is not None else None
            api_ms = usage.get("apiDurationMs") or 0
            out_t = int(usage.get("outputTokens") or 0)
            reason_t = int(usage.get("reasoningTokens") or 0)
            in_t = int(usage.get("inputTokens") or 0)
            cache_t = int(usage.get("cachedReadTokens") or 0)
            sec = api_ms / 1000.0 if api_ms else 0
            # Prefer per-step cost parts when reconstructed (matches list est total)
            su_tot = (
                ((last_round or {}).get("step_usage") or {}).get("totals") or {}
            )
            cost_in = su_tot.get("cost_in_usd")
            cost_cached = su_tot.get("cost_cached_usd")
            cost_out = su_tot.get("cost_out_usd")
            if cost_in is None:
                cost_in = est["cost_usd"].get("uncached_input")
            if cost_cached is None:
                cost_cached = est["cost_usd"].get("cached_input")
            if cost_out is None:
                cost_out = est["cost_usd"].get("output")
            self._turn_seq += 1
            turn = {
                "index": self._turn_seq,
                "prompt_id": update.get("prompt_id") or pid,
                "input_tokens": in_t,
                "output_tokens": out_t,
                "reasoning_tokens": reason_t,
                "cached_read_tokens": cache_t,
                "uncached_input_tokens": max(0, in_t - min(cache_t, in_t)),
                "total_tokens": int(usage.get("totalTokens") or 0),
                "api_duration_ms": api_ms,
                "model_calls": usage.get("modelCalls"),
                "official_ticks": ticks,
                "official_usd": official if official is not None else 0.0,
                "estimate_usd": estimate_usd,
                "estimate_breakdown": est["cost_usd"],
                "cost_in_usd": cost_in,
                "cost_cached_usd": cost_cached,
                "cost_out_usd": cost_out,
                "tier": est["tier"],
                "context_tokens_for_tier": est["context_tokens_for_tier"],
                "tier_method": est["tier_resolution"]["method"],
                "peak_context_tokens": peak,
                "model_step_count": (last_round or {}).get("model_step_count"),
                "output_tokens_per_sec": (last_round or {}).get("gen_tokens_per_sec")
                if (last_round or {}).get("gen_tokens_per_sec") is not None
                else (round(out_t / sec, 3) if sec else None),
                "reasoning_tokens_per_sec": round(reason_t / sec, 3) if sec else None,
                "gen_tokens_per_sec": (last_round or {}).get("gen_tokens_per_sec")
                if (last_round or {}).get("gen_tokens_per_sec") is not None
                else (round(out_t / sec, 3) if sec else None),
                "gen_ms": (last_round or {}).get("gen_ms"),
            }
            self.turns.append(turn)
            if len(self.turns) > MAX_TURNS:
                self.turns = self.turns[-MAX_TURNS:]
            self._push_feed(
                kind,
                f"#{turn['index']} off={official and f'${official:.4f}'} est=${estimate_usd:.4f} "
                f"in={in_t} uncached={turn['uncached_input_tokens']} out={out_t} reason={reason_t} "
                f"cache={cache_t} peak_ctx={peak} tier={est['tier']} steps={turn.get('model_step_count')}",
                t_label,
            )
            self._stream_kind = None
            self._turn_peak_ctx = None
        elif kind == "hook_execution":
            runs = update.get("runs") or []
            names = [
                str(x.get("name") or "")
                for x in runs
                if isinstance(x, dict) and x.get("name")
            ]
            detail = update.get("event_name") or "hook"
            if names:
                detail = f"{detail} · {', '.join(names[:2])}"
            try:
                import json as _json

                detail += f" · {len(_json.dumps(update, ensure_ascii=False))}ch"
            except Exception:
                pass
            self._push_feed(kind, detail, t_label)

    def _handle_event(self, raw: dict[str, Any]) -> None:
        et = raw.get("type")
        if et == "phase_changed":
            self.phase = raw.get("phase")
            self.live["phase"] = self.phase
        elif et == "first_token":
            self._push_feed("first_token", "model first token", (raw.get("ts") or "")[-12:-1])
        elif et == "turn_started":
            mid = raw.get("model_id")
            if mid:
                self.hierarchy._note_model({"model_id": mid})
            self._push_feed(
                "turn_started",
                f"turn {raw.get('turn_number')} model={mid}",
                (raw.get("ts") or "")[-12:-1],
            )
        elif et == "turn_ended":
            self._push_feed("turn_ended", f"outcome={raw.get('outcome')}", (raw.get("ts") or "")[-12:-1])
        elif et == "tool_completed":
            self._push_feed(
                "tool_completed",
                f"{raw.get('tool_name')} {raw.get('duration_ms')}ms {raw.get('outcome')}",
                (raw.get("ts") or "")[-12:-1],
            )

    def _load_signals(self) -> None:
        if not self.session_dir:
            return
        p = self.session_dir / "signals.json"
        if not p.is_file():
            return
        try:
            self.signals = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        cu = self.signals.get("contextTokensUsed")
        if isinstance(cu, int):
            self.live["context_tokens_ui"] = cu
            # Card "context now" prefers UI/signals (matches TUI) over mid-tool stream spikes
            self.live["context_tokens"] = cu
            self.live["tier"] = pick_tier(cu)["name"]
        elif self.live.get("context_tokens") is None and self.live.get("context_tokens_stream") is not None:
            self.live["context_tokens"] = self.live["context_tokens_stream"]
            self.live["tier"] = pick_tier(int(self.live["context_tokens_stream"]))["name"]
        if self.live.get("phase") is None and self.phase:
            self.live["phase"] = self.phase
        self.hierarchy._note_model(self.signals)
        self.live["model"] = getattr(self.hierarchy, "_pricing_model", None)

    def tick(self) -> None:
        """One poll cycle — must be called with self.lock held (or from unlocked context carefully)."""
        try:
            # Pinned: stay on that session; only re-resolve if file vanished
            if self.pinned_session_id:
                if (
                    self.session_dir is None
                    or self.session_id != self.pinned_session_id
                    or not (self.session_dir / "updates.jsonl").is_file()
                ):
                    d = resolve_session_dir(self.pinned_session_id)
                    if d:
                        self._attach_unlocked(d, pin=True)
                    else:
                        self.error = f"pinned session gone: {self.pinned_session_id}"
                        return
            else:
                # auto-follow newest active session if none attached or stale
                if self.session_dir is None or not (self.session_dir / "updates.jsonl").is_file():
                    d = resolve_session_dir()
                    if d:
                        self._attach_unlocked(d, pin=False)
                    else:
                        self.error = f"no sessions under {SESSIONS_ROOT}"
                        return

                # session switch if a newer active session appears (follow mode only)
                preferred = resolve_session_dir()
                if preferred and preferred != self.session_dir:
                    active = set(read_active_session_ids())
                    if preferred.name in active or (
                        (preferred / "updates.jsonl").stat().st_mtime
                        > (self.session_dir / "updates.jsonl").stat().st_mtime
                    ):
                        self._attach_unlocked(preferred, pin=False)

            assert self._updates_path is not None
            lines, self._updates_offset = self._read_new_lines(
                self._updates_path, self._updates_offset
            )
            for line in lines:
                try:
                    self._handle_update(json.loads(line))
                except json.JSONDecodeError:
                    continue

            lines_e, self._events_offset = self._read_new_lines(
                self._events_path, self._events_offset
            )
            for line in lines_e:
                try:
                    self._handle_event(json.loads(line))
                except json.JSONDecodeError:
                    continue

            self._load_signals()
            self._sync_children()
            self.bootstrapped = True
            self.error = None
        except Exception as e:  # noqa: BLE001 — surface in UI
            self.error = f"{type(e).__name__}: {e}"

    def _child_lineage(self) -> tuple[list[str], dict[str, str], dict[str, str]]:
        return child_lineage(self)

    def _sync_children(self) -> None:
        return sync_children(self)

    def snapshot_bytes(self) -> bytes:
        """Return JSON bytes for /api/state; rebuild only when data revision changes."""
        with self.lock:
            # Ingest new lines first (background poller also ticks; both under lock)
            self.tick()
            rev = self.hierarchy.revision
            # Pinned (/telemetry): only active + focused session in the picker
            # payload — avoid usage-scanning dozens of unrelated jsonl files.
            sessions = list_sessions_for_ui(focus_session_id=self.pinned_session_id)
            sig_key = (
                self.live.get("context_tokens"),
                self.live.get("context_tokens_ui"),
                self.live.get("phase"),
                len(self.turns),
                len(self.feed),
                len(self.context_series),
                self.error,
                self.signals.get("contextTokensUsed"),
                self.signals.get("turnCount"),
                self.signals.get("toolCallCount"),
                self.signals.get("primaryModelId"),
                getattr(self.hierarchy, "_pricing_model", None),
                self.session_id,
                self.pinned_session_id,
                tuple(s["session_id"] for s in sessions[:20]),
                tuple(
                    (cid, w.hierarchy.revision, len(w.turns))
                    for cid, w in self._children.items()
                ),
            )
            if (
                self._snap_bytes is not None
                and self._snap_rev == rev
                and self._snap_sig_key == sig_key
            ):
                return self._snap_bytes

            official = 0.0
            ticks_sum = 0
            for t in self.turns:
                official += float(t.get("official_usd") or 0)
                ticks_sum += int(t.get("official_ticks") or 0)
            children_official = 0.0
            for w in self._children.values():
                children_official += sum(float(t.get("official_usd") or 0) for t in w.turns)
            # Parent-only = harness bill minus children (same $ the peel removes)
            parent_only = max(0.0, official - children_official)

            ctx = self.live.get("context_tokens")
            rounds_all = self.hierarchy.snapshot_rounds(include_open=True)

            # Session Cost estimate over *all* rounds (not the wire-truncated slice).
            # R1 white UI total is peeled; session sum uses full API bill per turn
            # so we never do peeled_rounds + System (double) or peeled-only (short).
            parent_est = _sum_rounds_estimate(rounds_all)
            _roots, _alias, latest_map = self._child_lineage()

            # Only last N rounds over the wire (browser tree)
            rounds_raw = rounds_all
            if len(rounds_raw) > API_ROUNDS:
                rounds_raw = rounds_raw[-API_ROUNDS:]
            rounds = [self._enrich_round_usage(r) for r in rounds_raw]
            ordmap = getattr(self.hierarchy, "_subagent_ordinal", None) or {}
            sub_sessions = []
            seen_tab: set[str] = set()
            for w in self._children.values():
                sid = str(w.session_id or "").lower()
                root = str(getattr(w, "root_id", None) or sid).lower()
                # One tab per agent: latest resume, else the original spawn.
                pick = str((latest_map or {}).get(root) or root).lower()
                if sid != pick:
                    continue
                if root in seen_tab:
                    continue
                seen_tab.add(root)
                snap = w.snapshot(self._enrich_round_usage)
                snap["root_session_id"] = root
                snap["n"] = ordmap.get(root) or ordmap.get(str(root or "").lower())
                snap["resume_index"] = 0
                snap["label"] = (
                    f"Sub Agent {snap['n']}" if snap.get("n") else (snap.get("label") or "Sub Agent")
                )
                sub_sessions.append(snap)
            # Parent session $ + each sub-agent tab $ (the number on that tab).
            children_est = _sum_child_tab_estimates(sub_sessions)
            est = parent_est + children_est
            # Drop heavy nested estimate blobs from completed steps already priced
            slim_rounds = list(rounds)
            for ss in sub_sessions:
                slim_rounds.extend(ss.get("rounds") or [])
            for rr in slim_rounds:
                for step in rr.get("model_steps") or []:
                    if not isinstance(step, dict):
                        continue
                    est_s = step.get("estimate")
                    if isinstance(est_s, dict):
                        # keep fields the UI reads; drop verbose notes / duplicates
                        for k in ("cache_note", "in_note", "method", "cost_usd"):
                            est_s.pop(k, None)

            kind = None
            if self.session_dir is not None:
                kind = (read_session_summary(self.session_dir) or {}).get("session_kind")
            payload = _slim_wire(
                {
                    "watching": self.bootstrapped and self.error is None,
                    "error": self.error,
                    "session_id": self.session_id,
                    "session_kind": kind,
                    "pinned_session_id": self.pinned_session_id,
                    "follow_active": self.pinned_session_id is None,
                    "sessions": sessions,
                    "source": str(self.session_dir) if self.session_dir else None,
                    "live": dict(self.live),
                    "signals": _slim_signals(self.signals),
                    "turns": list(self.turns),
                    "rounds": rounds,
                    "sub_sessions": sub_sessions,
                    "context_series": list(self.context_series),
                    "feed": list(self.feed),
                    "totals": {
                        "official_usd": round(official, 6),
                        "estimate_usd": round(est, 6),
                        "official_ticks": ticks_sum,
                        "turns": len(self.turns),
                        "parent_only_usd": round(parent_only, 6),
                        "children_usd": round(children_official, 6),
                        "parent_estimate_usd": round(parent_est, 6),
                        "children_estimate_usd": round(children_est, 6),
                        "combined_usd": round(official, 6),
                        "subagent_count": len(sub_sessions),
                        "gen_tokens_per_sec": _mean_round_tps(rounds_all),
                    },
                    "pricing": pricing_payload(
                        model=getattr(self.hierarchy, "_pricing_model", None),
                        models_raw=list(getattr(self.hierarchy, "_models_raw", None) or []),
                        assumed=getattr(self.hierarchy, "_pricing_model", None) is None,
                    ),
                    "context_now_estimate_note": (
                        "Context card uses signals.contextTokensUsed (TUI-aligned). "
                        f"Current context {ctx} → tier {pick_tier(ctx or 0)['name']}."
                        if ctx is not None
                        else None
                    ),
                    "revision": rev,
                }
            )
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
            self._snap_bytes = body
            self._snap_etag = '"' + hashlib.sha1(body).hexdigest()[:16] + '"'
            self._snap_rev = rev
            self._snap_sig_key = sig_key
            return body

    def snapshot_etag(self) -> str:
        """ETag for the last snapshot_bytes() payload (call after snapshot_bytes)."""
        with self.lock:
            if self._snap_etag:
                return self._snap_etag
            if self._snap_bytes is not None:
                self._snap_etag = (
                    '"' + hashlib.sha1(self._snap_bytes).hexdigest()[:16] + '"'
                )
                return self._snap_etag
            return '"0"'


MONITOR = SessionMonitor()
