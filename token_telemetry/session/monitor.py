"""Live session state engine (SessionMonitor)."""

from __future__ import annotations

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
from token_telemetry.hierarchy import HierarchyBuilder
from token_telemetry.session.discover import (
    SESSIONS_ROOT,
    list_sessions_for_ui,
    read_active_session_ids,
    resolve_session_dir,
)
from token_telemetry.session.subagents import (
    collect_child_ids_from_round,
    is_subagent_session,
    read_session_summary,
    sibling_session_dir,
)


def _enrich_user_prompt(up: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Price the user-prompt continuity row (prior cache + new uncached)."""
    if not up or not isinstance(up, dict):
        return up
    out = dict(up)
    # Already priced by hierarchy bootstrap split (first round)
    if out.get("cost_in_usd") is not None and out.get("tokens_in") is not None:
        if out.get("estimate_usd") is None:
            out["estimate_usd"] = float(out.get("cost_in_usd") or 0) + float(
                out.get("cost_cached_usd") or 0
            )
        return out
    cached = out.get("cached_est")
    uncached = out.get("uncached_est")
    try:
        c = int(cached) if cached is not None else 0
        u = int(uncached) if uncached is not None else 0
    except (TypeError, ValueError):
        return out
    total = c + u
    if total <= 0 and c <= 0 and u <= 0:
        out["estimate_usd"] = 0.0
        out["cost_in_usd"] = 0.0
        out["cost_cached_usd"] = 0.0
        out["tokens_in"] = 0
        out["tokens_cached"] = 0
        return out
    est = estimate_cost_usd(
        input_tokens=total,
        output_tokens=0,
        cached_read_tokens=c,
        peak_context_tokens=total or c or u,
        model_calls=1,
    )
    out["input_est"] = total
    out["tokens_in"] = u
    out["tokens_cached"] = c
    out["cost_in_usd"] = float(est["cost_usd"]["uncached_input"])
    out["cost_cached_usd"] = float(est["cost_usd"]["cached_input"])
    out["estimate_usd"] = float(est["cost_usd"]["total"])
    out["estimate_breakdown"] = est["cost_usd"]
    out["tier"] = est["tier"]
    return out


def _enrich_system_prompt(sp: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """Pass-through / light price for first-round system bootstrap card."""
    if not sp or not isinstance(sp, dict):
        return sp
    out = dict(sp)
    # Keep parts list (breakdown)
    if isinstance(out.get("parts"), list):
        out["parts"] = [dict(p) for p in out["parts"] if isinstance(p, dict)]
    if out.get("cost_in_usd") is not None:
        return out
    u = int(out.get("tokens_in") or out.get("uncached_est") or out.get("logical_tokens") or 0)
    if u <= 0:
        out["estimate_usd"] = 0.0
        out["cost_in_usd"] = 0.0
        out["tokens_in"] = 0
        return out
    est = estimate_cost_usd(
        input_tokens=u,
        output_tokens=0,
        cached_read_tokens=0,
        peak_context_tokens=u,
        model_calls=1,
    )
    out["tokens_in"] = u
    out["tokens_cached"] = 0
    out["cost_in_usd"] = float(est["cost_usd"]["uncached_input"])
    out["cost_cached_usd"] = 0.0
    out["estimate_usd"] = float(est["cost_usd"]["total"])
    return out


# RAM guards
MAX_TURNS = 40
MAX_CONTEXT_POINTS = 200
MAX_READ_CHUNK = 1_500_000  # bytes per tick when catching up
API_ROUNDS = 20  # rounds sent to browser


class _ChildWatch:
    """Incremental hierarchy for one sub-agent session under a parent."""

    def __init__(self, session_dir: Path) -> None:
        self.session_dir = session_dir
        self.session_id = session_dir.name
        self.hierarchy = HierarchyBuilder()
        self.hierarchy.set_session_dir(session_dir)
        self._updates_path = session_dir / "updates.jsonl"
        self._updates_offset = 0
        self.turns: list[dict[str, Any]] = []
        self.live: dict[str, Any] = {"context_tokens": None}
        self.error: Optional[str] = None

    def tick(self, read_lines) -> None:
        try:
            lines, self._updates_offset = read_lines(
                self._updates_path, self._updates_offset
            )
            for line in lines:
                try:
                    raw = json.loads(line)
                except json.JSONDecodeError:
                    continue
                self.hierarchy.feed_raw(raw)
                upd = ((raw.get("params") or {}).get("update") or {})
                if upd.get("sessionUpdate") == "turn_completed":
                    usage = upd.get("usage") or {}
                    ticks = usage.get("costUsdTicks")
                    self.turns.append(
                        {
                            "index": len(self.turns) + 1,
                            "input_tokens": int(usage.get("inputTokens") or 0),
                            "output_tokens": int(usage.get("outputTokens") or 0),
                            "cached_read_tokens": int(usage.get("cachedReadTokens") or 0),
                            "total_tokens": int(usage.get("totalTokens") or 0),
                            "model_calls": usage.get("modelCalls"),
                            "official_ticks": ticks,
                            "official_usd": ticks_to_usd(ticks) if ticks is not None else 0.0,
                        }
                    )
                meta = (raw.get("params") or {}).get("_meta") or {}
                tt = meta.get("totalTokens")
                if isinstance(tt, int):
                    self.live["context_tokens"] = tt
            self.error = None
        except Exception as e:  # noqa: BLE001
            self.error = f"{type(e).__name__}: {e}"

    def snapshot(self, enrich_round) -> dict[str, Any]:
        summary = read_session_summary(self.session_dir)
        official = sum(float(t.get("official_usd") or 0) for t in self.turns)
        rounds_all = self.hierarchy.snapshot_rounds(include_open=True)
        rounds_raw = rounds_all[-API_ROUNDS:] if len(rounds_all) > API_ROUNDS else rounds_all
        rounds = [enrich_round(r) for r in rounds_raw]
        title = (
            summary.get("generated_title")
            or summary.get("session_summary")
            or summary.get("agent_name")
            or self.session_id[:8]
        )
        return {
            "session_id": self.session_id,
            "session_kind": summary.get("session_kind") or "subagent",
            "agent_name": summary.get("agent_name"),
            "title": title,
            "label": title,
            "official_usd": round(official, 6),
            "turns": list(self.turns),
            "rounds": rounds,
            "live": dict(self.live),
            "error": self.error,
        }


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
        self._snap_sig_key: Optional[tuple] = None
        self._children: dict[str, "_ChildWatch"] = {}

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
        self._snap_sig_key = None
        self._children = {}
        self.live["model"] = getattr(self.hierarchy, "_pricing_model", None)

    def select_session(self, session_id: Optional[str]) -> dict[str, Any]:
        """
        Pin dashboard to a session id, or pass None / '' to follow active again.
        """
        with self.lock:
            if not session_id:
                self.pinned_session_id = None
                d = resolve_session_dir()
                if d:
                    self._attach_unlocked(d, pin=False)
                    return {"ok": True, "session_id": d.name, "pinned": False}
                return {"ok": False, "error": "no session available"}
            d = resolve_session_dir(session_id)
            if not d:
                return {"ok": False, "error": f"unknown session {session_id}"}
            self._attach_unlocked(d, pin=True)
            return {"ok": True, "session_id": d.name, "pinned": True}

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
        """Attach official usage + list-rate estimate + per-step reconstruction."""
        from token_telemetry.pricing import reconstruct_model_step_usage  # local; always available

        usage = round_.get("usage_raw") or {}
        peak = round_.get("context_peak")
        steps = list(round_.get("model_steps") or [])
        # Ensure per-step estimates exist (open rounds / older builders)
        step_usage = round_.get("step_usage")
        if steps and (
            not step_usage
            or not (steps[0].get("estimate") if steps else None)
        ):
            prior = round_.get("cache_baseline_at_start")
            if not isinstance(prior, int):
                prior = (step_usage or {}).get("prior_context_tokens")
            cr = round_.get("context_reread")
            reread_flag = bool(
                round_.get("session_restart")
                or round_.get("cache_miss")
                or cr
                or (round_.get("user_prompt") or {}).get("session_restart")
            )
            reread_tok = 0
            if isinstance(cr, dict):
                reread_tok = int(cr.get("reread_tokens") or 0)
            reread_tok = int(
                round_.get("reread_in_tokens")
                or (round_.get("user_prompt") or {}).get("reread_tokens")
                or reread_tok
                or 0
            )
            up0 = round_.get("user_prompt") if isinstance(round_.get("user_prompt"), dict) else {}
            user_unc = 0
            try:
                user_unc = int(up0.get("tokens_in") or up0.get("uncached_est") or 0)
            except (TypeError, ValueError):
                user_unc = 0
            fam = (
                round_.get("model_family")
                or getattr(self.hierarchy, "_pricing_model", None)
            )
            with pricing_model_scope(fam):
                recon = reconstruct_model_step_usage(
                    steps,
                    official_usage=usage if usage else None,
                    prior_context_tokens=prior if isinstance(prior, int) else None,
                    context_reread=reread_flag,
                    reread_uncached_tokens=reread_tok,
                    user_uncached_tokens=int(user_unc),
                )
            steps = recon["steps"]
            step_usage = {
                "method": recon["method"],
                "calibrated": recon["calibrated"],
                "totals": recon["totals"],
                "breakdown": recon.get("breakdown") or {},
                "note": recon.get("note"),
                "prior_context_tokens": recon.get("prior_context_tokens"),
            }

        fam = (
            round_.get("model_family")
            or getattr(self.hierarchy, "_pricing_model", None)
        )
        with pricing_model_scope(fam):
            user_prompt = _enrich_user_prompt(round_.get("user_prompt"))
            system_prompt = _enrich_system_prompt(round_.get("system_prompt"))
        # Prefer hierarchy merge (includes session-restart user In / tree_in)
        breakdown = dict(
            round_.get("breakdown")
            or (step_usage or {}).get("breakdown")
            or {}
        )
        # Keep hierarchy reread fields even if step_usage breakdown overwrote
        for k in (
            "reread_in_tokens",
            "reread_in_usd",
            "reread_tokens",
            "context_reread",
            "user_cache_share_tokens",
        ):
            if round_.get("breakdown") and round_["breakdown"].get(k) is not None:
                breakdown[k] = round_["breakdown"][k]
        if system_prompt:
            breakdown["system_in_tokens"] = int(system_prompt.get("tokens_in") or 0)
            breakdown["system_in_usd"] = float(system_prompt.get("cost_in_usd") or 0)
        if user_prompt:
            breakdown["user_in_tokens"] = int(user_prompt.get("tokens_in") or 0)
            breakdown["user_in_usd"] = float(user_prompt.get("cost_in_usd") or 0)
            breakdown["user_cached_tokens"] = int(user_prompt.get("tokens_cached") or 0)
            breakdown["user_cached_usd"] = float(user_prompt.get("cost_cached_usd") or 0)
            # Round In = User + Σ call In (+ reread). Prefer step sum over harness bag.
            user_tok = int(user_prompt.get("tokens_in") or 0)
            user_usd = float(user_prompt.get("cost_in_usd") or 0)
            sum_call = 0
            sum_call_usd = 0.0
            for s in steps or []:
                if not isinstance(s, dict):
                    continue
                sum_call += int(s.get("tokens_in") or s.get("harness_in_tokens") or 0)
                sum_call_usd += float(
                    s.get("cost_in_usd") or s.get("harness_in_usd") or 0
                )
            harness_tok = (
                int(sum_call)
                if sum_call > 0
                else int(breakdown.get("harness_in_tokens") or 0)
            )
            harness_usd = (
                float(sum_call_usd)
                if sum_call > 0
                else float(breakdown.get("harness_in_usd") or 0)
            )
            breakdown["harness_in_tokens"] = harness_tok
            breakdown["harness_in_usd"] = harness_usd
            reread_tok = int(
                breakdown.get("reread_in_tokens")
                or breakdown.get("reread_tokens")
                or round_.get("reread_in_tokens")
                or user_prompt.get("reread_tokens")
                or 0
            )
            reread_usd = float(
                breakdown.get("reread_in_usd")
                or round_.get("reread_in_usd")
                or user_prompt.get("reread_in_usd")
                or 0
            )
            if reread_tok > 0 or breakdown.get("context_reread") or round_.get(
                "session_restart"
            ):
                breakdown["tree_in_tokens"] = reread_tok + user_tok + harness_tok
                breakdown["tree_in_usd"] = reread_usd + user_usd + harness_usd
                breakdown["reread_in_tokens"] = reread_tok
                breakdown["reread_in_usd"] = reread_usd
            else:
                breakdown["tree_in_tokens"] = user_tok + harness_tok
                breakdown["tree_in_usd"] = user_usd + harness_usd

        su_totals = (step_usage or {}).get("totals") or {}
        base = {
            "index": round_.get("index"),
            "prompt_id": round_.get("prompt_id"),
            "user_preview": round_.get("user_preview"),
            "user_chars": round_.get("user_chars"),
            "context_start": round_.get("context_start"),
            "context_end": round_.get("context_end"),
            "context_peak": peak,
            "context_delta": round_.get("context_delta"),
            "compact_delta": round_.get("compact_delta"),
            "context_after_compact": round_.get("context_after_compact"),
            "cache_baseline_at_start": round_.get("cache_baseline_at_start"),
            "idle_gap_ms": round_.get("idle_gap_ms"),
            "started_ms": round_.get("started_ms"),
            "completed_ms": round_.get("completed_ms"),
            "turn_start_ms": round_.get("turn_start_ms"),
            "session_restart": bool(
                round_.get("session_restart")
                or (user_prompt or {}).get("session_restart")
            ),
            "cache_miss": bool(
                round_.get("cache_miss") or (user_prompt or {}).get("cache_miss")
            ),
            "context_reread": round_.get("context_reread"),
            "reread_in_tokens": (
                round_.get("reread_in_tokens")
                or breakdown.get("reread_in_tokens")
                or (user_prompt or {}).get("reread_tokens")
            ),
            "reread_in_usd": (
                round_.get("reread_in_usd")
                or breakdown.get("reread_in_usd")
                or (user_prompt or {}).get("reread_in_usd")
            ),
            "completed": round_.get("completed"),
            "model_step_count": round_.get("model_step_count") or len(steps),
            "model_steps": steps,
            "step_usage": step_usage,
            "breakdown": breakdown,
            "cost_in_usd": su_totals.get("cost_in_usd"),
            "cost_cached_usd": su_totals.get("cost_cached_usd"),
            "cost_out_usd": su_totals.get("cost_out_usd"),
            "notes": round_.get("notes") or [],
            "compactions": round_.get("compactions") or [
                n for n in (round_.get("notes") or []) if isinstance(n, dict) and n.get("kind") == "compaction"
            ],
            "compact_before": round_.get("compact_before"),
            "compact_after": round_.get("compact_after"),
            "recap_after": round_.get("recap_after"),
            "recaps_after": round_.get("recaps_after") or [
                n for n in (round_.get("notes") or [])
                if isinstance(n, dict) and n.get("kind") == "session_recap"
            ],
            "recaps_before": round_.get("recaps_before") or [],
            "user_prompt": user_prompt,
            "system_prompt": system_prompt,
            "stop_reason": round_.get("stop_reason"),
            "model_id": round_.get("model_id"),
            "model_family": round_.get("model_family")
            or getattr(self.hierarchy, "_pricing_model", None),
        }

        if not usage:
            # Live open round: estimate from reconstructed steps only
            su = (step_usage or {}).get("totals") or {}
            return {
                **base,
                "usage": None,
                "estimate": None,
                "estimate_usd": su.get("cost_usd"),
                "input_tokens": su.get("input"),
                "output_tokens": su.get("output"),
                "cached_read_tokens": su.get("cached_read"),
                "uncached_input_tokens": su.get("uncached_input"),
                "official_usd": None,
                "tier": None,
            }

        est = estimate_from_usage(
            usage,
            peak_context_tokens=peak,
            model=round_.get("model_family")
            or getattr(self.hierarchy, "_pricing_model", None),
        )
        ticks = usage.get("costUsdTicks")
        official = ticks_to_usd(ticks) if ticks is not None else None
        api_ms = usage.get("apiDurationMs") or 0
        out_t = int(usage.get("outputTokens") or 0)
        reason_t = int(usage.get("reasoningTokens") or 0)
        in_t = int(usage.get("inputTokens") or 0)
        cache_t = int(usage.get("cachedReadTokens") or 0)
        calls = usage.get("modelCalls")
        sec = api_ms / 1000.0 if api_ms else 0
        uncached = max(0, in_t - min(cache_t, in_t))
        step_sum = (step_usage or {}).get("totals") or {}
        # Prefer sum of per-step list prices when steps align with modelCalls
        estimate_usd = est["cost_usd"]["total"]
        cost_in_usd = est["cost_usd"].get("uncached_input")
        cost_cached_usd = est["cost_usd"].get("cached_input")
        cost_out_usd = est["cost_usd"].get("output")
        # Prefer calibrated per-step API bill (paid In + cache + out) when present
        if step_sum.get("cost_usd") is not None and steps:
            estimate_usd = float(step_sum["cost_usd"])
            if step_sum.get("cost_in_usd") is not None:
                cost_in_usd = float(step_sum["cost_in_usd"])
            if step_sum.get("cost_cached_usd") is not None:
                cost_cached_usd = float(step_sum["cost_cached_usd"])
            if step_sum.get("cost_out_usd") is not None:
                cost_out_usd = float(step_sum["cost_out_usd"])
        # R1: hierarchy peels System from white total (tree In + Cached + Out)
        if (
            system_prompt
            and isinstance(round_.get("estimate_usd"), (int, float))
            and (breakdown.get("round_total_peeled_system") or round_.get("system_prompt"))
        ):
            estimate_usd = float(round_["estimate_usd"])
            if breakdown.get("tree_in_usd") is not None:
                # tree In is the R1 In bar; keep API unc under cost_in if needed
                pass
            if breakdown.get("total_usd") is not None:
                breakdown = dict(breakdown)
                breakdown["total_usd"] = float(estimate_usd)
        return {
            **base,
            "input_tokens": in_t,
            "output_tokens": out_t,
            "reasoning_tokens": reason_t,
            "cached_read_tokens": cache_t,
            "uncached_input_tokens": uncached,
            "total_tokens": int(usage.get("totalTokens") or 0),
            "api_duration_ms": api_ms,
            "model_calls": calls,
            "official_ticks": ticks,
            "official_usd": official if official is not None else 0.0,
            "estimate_usd": estimate_usd,
            "estimate_usd_from_usage_aggregate": est["cost_usd"]["total"],
            "estimate_breakdown": est["cost_usd"],
            "cost_in_usd": cost_in_usd,
            "cost_cached_usd": cost_cached_usd,
            "cost_out_usd": cost_out_usd,
            "tier": est["tier"],
            "context_tokens_for_tier": est["context_tokens_for_tier"],
            "tier_method": (est.get("tier_resolution") or {}).get("method"),
            "output_tokens_per_sec": round(out_t / sec, 3) if sec else None,
            "reasoning_tokens_per_sec": round(reason_t / sec, 3) if sec else None,
            "gen_tokens_per_sec": round(out_t / sec, 3) if sec else None,
        }

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
            turn = {
                "index": len(self.turns) + 1,
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
                "output_tokens_per_sec": round(out_t / sec, 3) if sec else None,
                "reasoning_tokens_per_sec": round(reason_t / sec, 3) if sec else None,
                "gen_tokens_per_sec": round(out_t / sec, 3) if sec else None,
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

    def _known_child_ids(self) -> list[str]:
        ids: list[str] = []
        seen: set[str] = set()
        for rr in self.hierarchy.rounds:
            if not isinstance(rr, dict):
                continue
            for uid in collect_child_ids_from_round(rr):
                if uid not in seen:
                    seen.add(uid)
                    ids.append(uid)
        open_r = getattr(self.hierarchy, "_open", None)
        if isinstance(open_r, dict):
            for uid in collect_child_ids_from_round(open_r):
                if uid not in seen:
                    seen.add(uid)
                    ids.append(uid)
        return ids

    def _sync_children(self) -> None:
        if not self.session_dir or is_subagent_session(self.session_dir):
            self._children = {}
            return
        wanted = self._known_child_ids()
        for uid in list(self._children):
            if uid not in wanted:
                self._children.pop(uid, None)
        for uid in wanted:
            watch = self._children.get(uid)
            if watch is None:
                d = sibling_session_dir(self.session_dir, uid)
                if d is None or not (d / "updates.jsonl").is_file():
                    continue
                watch = _ChildWatch(d)
                self._children[uid] = watch
            watch.tick(self._read_new_lines)

    def snapshot_bytes(self) -> bytes:
        """Return JSON bytes for /api/state; rebuild only when data revision changes."""
        with self.lock:
            # Ingest new lines first (background poller also ticks; both under lock)
            self.tick()
            rev = self.hierarchy.revision
            sessions = list_sessions_for_ui()
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

            def _round_api_bill(rr: dict) -> float:
                """Full list-rate bill for one turn (R1 includes System)."""
                bd = rr.get("breakdown") if isinstance(rr.get("breakdown"), dict) else {}
                api_tot = bd.get("api_total_usd")
                if api_tot is None:
                    su = rr.get("step_usage") if isinstance(rr.get("step_usage"), dict) else {}
                    tot = su.get("totals") if isinstance(su.get("totals"), dict) else {}
                    api_tot = tot.get("api_cost_usd")
                if api_tot is not None:
                    return float(api_tot)
                return float(rr.get("estimate_usd") or 0)

            # Session Cost estimate over *all* rounds (not the wire-truncated slice).
            # R1 white UI total is peeled; session sum uses full API bill per turn
            # so we never do peeled_rounds + System (double) or peeled-only (short).
            est = 0.0
            for rr in rounds_all:
                # Light enrich path for totals: hierarchy already priced completed rounds
                est += _round_api_bill(rr if isinstance(rr, dict) else {})
                # Fork recaps (isolated) — billed but not in round tree growth
                for rec in (rr.get("recaps_after") or []) if isinstance(rr, dict) else []:
                    if isinstance(rec, dict) and rec.get("cost_usd") is not None:
                        try:
                            est += float(rec["cost_usd"])
                        except (TypeError, ValueError):
                            pass

            # Only last N rounds over the wire (browser tree)
            rounds_raw = rounds_all
            if len(rounds_raw) > API_ROUNDS:
                rounds_raw = rounds_raw[-API_ROUNDS:]
            rounds = [self._enrich_round_usage(r) for r in rounds_raw]
            sub_sessions = [w.snapshot(self._enrich_round_usage) for w in self._children.values()]
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

            payload = {
                "watching": self.bootstrapped and self.error is None,
                "error": self.error,
                "session_id": self.session_id,
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
                    "combined_usd": round(official, 6),
                    "subagent_count": len(sub_sessions),
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
            }
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode(
                "utf-8"
            )
            self._snap_bytes = body
            self._snap_rev = rev
            self._snap_sig_key = sig_key
            return body


MONITOR = SessionMonitor()


def _slim_signals(sig: dict[str, Any]) -> dict[str, Any]:
    """Keep only fields the dashboard reads — signals.json can be large."""
    if not sig:
        return {}
    keys = (
        "contextTokensUsed",
        "contextWindowTokens",
        "avgTimeToFirstTokenMs",
        "avgResponseTimeMs",
        "itlP50Ms",
        "itlP99Ms",
        "itlMeanMs",
        "totalChunkCount",
        "toolCallCount",
        "turnCount",
        "sessionDurationSeconds",
        "modelsUsed",
        "primaryModelId",
        "toolsUsed",
    )
    out = {k: sig[k] for k in keys if k in sig}
    # Cap long lists
    for lk in ("modelsUsed", "toolsUsed"):
        v = out.get(lk)
        if isinstance(v, list) and len(v) > 40:
            out[lk] = v[:40]
    return out

