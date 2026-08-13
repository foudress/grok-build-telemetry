#!/usr/bin/env python3
"""
Extract token-related telemetry from a Grok Build session's updates.jsonl.

Focus: smallest stream events (thought/message chunks) plus turn usage.

Grok records:
  - agent_*_chunk._meta.totalTokens  → context size at that moment (not output delta)
  - turn_completed.usage             → official input/output/reasoning/cache tokens
  - agentTimestampMs / streamStartMs / turnStartMs → wall-clock for rates

Usage:
  python extract_session_events.py
  python extract_session_events.py --session-id 019f9753-...
  python extract_session_events.py --updates PATH\\updates.jsonl --out out\\events.jsonl
  python extract_session_events.py --summary
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional


# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

def default_sessions_root() -> Path:
    home = Path.home() / ".grok" / "sessions"
    # Windows: sessions are keyed by URL-encoded cwd (e.g. C%3A%5CUsers%5C…%5Cproject)
    if not home.is_dir():
        return home
    return home


def list_session_dirs(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    out: list[Path] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        # either a session id dir, or a workspace slug containing session dirs
        if (child / "updates.jsonl").is_file():
            out.append(child)
        else:
            for sub in child.iterdir():
                if sub.is_dir() and (sub / "updates.jsonl").is_file():
                    out.append(sub)
    return out


def resolve_updates_path(
    session_id: Optional[str],
    updates: Optional[Path],
    sessions_root: Path,
) -> Path:
    if updates:
        p = updates.expanduser().resolve()
        if not p.is_file():
            raise SystemExit(f"updates file not found: {p}")
        return p

    dirs = list_session_dirs(sessions_root)
    if session_id:
        matches = [d for d in dirs if d.name == session_id]
        if not matches:
            raise SystemExit(f"session id not found under {sessions_root}: {session_id}")
        return matches[0] / "updates.jsonl"

    if not dirs:
        raise SystemExit(f"no sessions found under {sessions_root}")

    # most recently modified updates.jsonl
    dirs.sort(key=lambda d: (d / "updates.jsonl").stat().st_mtime, reverse=True)
    return dirs[0] / "updates.jsonl"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

STREAM_TYPES = frozenset(
    {
        "agent_thought_chunk",
        "agent_message_chunk",
        "user_message_chunk",
    }
)

def ms_to_iso(ms: Optional[int | float]) -> Optional[str]:
    if ms is None:
        return None
    try:
        # agentTimestampMs looks like unix ms (sometimes slightly nonstandard epoch)
        sec = float(ms) / 1000.0
        return datetime.fromtimestamp(sec, tz=timezone.utc).isoformat()
    except (OSError, OverflowError, ValueError):
        return None


def text_preview(text: str, limit: int) -> str:
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    if limit <= 0 or len(text) <= limit:
        return text
    return text[:limit] + f"…[+{len(text) - limit} chars]"


def count_lines(text: str) -> int:
    if not text:
        return 0
    # number of display lines (split on newline; trailing empty after final \n ignored once)
    return text.count("\n") + (0 if text.endswith("\n") else 1)


def iter_raw_records(path: Path) -> Iterator[tuple[int, dict[str, Any]]]:
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for line_no, line in enumerate(f, start=1):
            line = line.strip()
            if not line:
                continue
            try:
                yield line_no, json.loads(line)
            except json.JSONDecodeError as e:
                yield line_no, {"_parse_error": str(e), "_raw": line[:200]}


def normalize_event(
    line_no: int,
    raw: dict[str, Any],
    *,
    text_limit: int,
    include_text: bool,
) -> Optional[dict[str, Any]]:
    if "_parse_error" in raw:
        return {
            "kind": "parse_error",
            "source_line": line_no,
            "error": raw["_parse_error"],
            "raw_preview": raw.get("_raw"),
        }

    params = raw.get("params") or {}
    update = params.get("update") or {}
    meta = params.get("_meta") or {}
    update_meta = update.get("_meta") or {}

    session_update = update.get("sessionUpdate")
    if not session_update:
        return None

    file_ts = raw.get("timestamp")  # often unix seconds
    agent_ms = meta.get("agentTimestampMs")
    stream_start_ms = meta.get("streamStartMs")
    turn_start_ms = meta.get("turnStartMs")

    content = update.get("content") or {}
    text = ""
    if isinstance(content, dict):
        text = content.get("text") or ""
    elif isinstance(content, str):
        text = content

    # Tool titles / names for TUI-ish labels
    tool_title = update.get("title")
    tool_call_id = update.get("toolCallId")
    tool_kind = update.get("kind")
    tool_name = None
    xai_tool = update_meta.get("x.ai/tool") if isinstance(update_meta, dict) else None
    if isinstance(xai_tool, dict):
        tool_name = xai_tool.get("name")

    usage = update.get("usage")
    usage_out = None
    if isinstance(usage, dict):
        # Official scale (headless docs): 1 USD = 10^10 ticks
        try:
            from pricing import estimate_from_usage, ticks_to_usd, COST_USD_TICKS_PER_USD
        except ImportError:
            COST_USD_TICKS_PER_USD = 10**10

            def ticks_to_usd(ticks):  # type: ignore
                if ticks is None:
                    return None
                return float(ticks) / COST_USD_TICKS_PER_USD

            def estimate_from_usage(u):  # type: ignore
                return None

        ticks = usage.get("costUsdTicks")
        cost_usd = ticks_to_usd(ticks) if ticks is not None else None
        # peak_context filled later in extract() when we have stream peaks
        est = estimate_from_usage(usage)

        usage_out = {
            "input_tokens": usage.get("inputTokens"),
            "output_tokens": usage.get("outputTokens"),
            "total_tokens": usage.get("totalTokens"),
            "cached_read_tokens": usage.get("cachedReadTokens"),
            "reasoning_tokens": usage.get("reasoningTokens"),
            "model_calls": usage.get("modelCalls"),
            "api_duration_ms": usage.get("apiDurationMs"),
            "cost_usd_ticks": ticks,
            "cost_usd": cost_usd,
            "cost_usd_ticks_per_usd": COST_USD_TICKS_PER_USD,
            "estimate": est,
            "num_turns": usage.get("numTurns"),
            "model_usage": usage.get("modelUsage"),
        }
        # derived rates from official usage (tokens / wall API time)
        api_ms = usage.get("apiDurationMs") or 0
        if api_ms and api_ms > 0:
            sec = api_ms / 1000.0
            out_t = usage.get("outputTokens") or 0
            reason_t = usage.get("reasoningTokens") or 0
            usage_out["output_tokens_per_sec"] = round(out_t / sec, 3)
            usage_out["reasoning_tokens_per_sec"] = round(reason_t / sec, 3)
            # generation rate: output only (reasoning already inside output shape)
            usage_out["gen_tokens_per_sec"] = round(out_t / sec, 3)
            if cost_usd is not None:
                usage_out["usd_per_sec"] = round(cost_usd / sec, 6)
        # rough $ per 1M tokens (totalTokens is input+cache+output style total)
        total_t = usage.get("totalTokens") or 0
        if cost_usd is not None and total_t:
            usage_out["usd_per_1m_total_tokens"] = round(cost_usd / total_t * 1_000_000, 4)

    event: dict[str, Any] = {
        "kind": session_update,
        "source_line": line_no,
        "method": raw.get("method"),
        "session_id": params.get("sessionId"),
        "event_id": meta.get("eventId"),
        "prompt_id": meta.get("promptId") or update.get("prompt_id"),
        # timestamps
        "file_timestamp": file_ts,
        "file_timestamp_iso": ms_to_iso(file_ts * 1000) if isinstance(file_ts, (int, float)) and file_ts < 1e12 else ms_to_iso(file_ts),
        "agent_timestamp_ms": agent_ms,
        "agent_timestamp_iso": ms_to_iso(agent_ms),
        "stream_start_ms": stream_start_ms,
        "turn_start_ms": turn_start_ms,
        "ms_since_stream_start": (agent_ms - stream_start_ms)
        if isinstance(agent_ms, (int, float)) and isinstance(stream_start_ms, (int, float))
        else None,
        "ms_since_turn_start": (agent_ms - turn_start_ms)
        if isinstance(agent_ms, (int, float)) and isinstance(turn_start_ms, (int, float))
        else None,
        # tokens / stream meta
        "context_total_tokens": meta.get("totalTokens"),  # context size snapshot
        "chunk_id": meta.get("chunkId"),
        "update_type": meta.get("updateType"),
        "model_id": (update.get("_meta") or {}).get("modelId")
        if isinstance(update.get("_meta"), dict)
        else None,
        # TUI / display payload
        "char_count": len(text) if text else 0,
        "line_count": count_lines(text) if text else 0,
        "text_preview": text_preview(text, text_limit) if text else None,
        "tool_call_id": tool_call_id,
        "tool_title": tool_title,
        "tool_name": tool_name,
        "tool_kind": tool_kind,
        "stop_reason": update.get("stop_reason"),
        "hook_event_name": update.get("event_name"),
        "usage": usage_out,
    }

    if include_text and text:
        event["text"] = text

    # Drop null noise for smaller events? Keep keys stable for consumers.
    return event


@dataclass
class StreamPhase:
    """A contiguous stream of thought or message chunks for one prompt_id."""

    kind: str
    prompt_id: Optional[str]
    first_ms: Optional[float] = None
    last_ms: Optional[float] = None
    stream_start_ms: Optional[float] = None
    chunk_count: int = 0
    char_count: int = 0
    line_count: int = 0
    context_tokens_first: Optional[int] = None
    context_tokens_last: Optional[int] = None
    chunk_ids: list[int] = field(default_factory=list)

    def add(self, ev: dict[str, Any]) -> None:
        self.chunk_count += 1
        self.char_count += int(ev.get("char_count") or 0)
        self.line_count += int(ev.get("line_count") or 0)
        ms = ev.get("agent_timestamp_ms")
        if isinstance(ms, (int, float)):
            if self.first_ms is None:
                self.first_ms = ms
            self.last_ms = ms
        if self.stream_start_ms is None:
            self.stream_start_ms = ev.get("stream_start_ms")
        tt = ev.get("context_total_tokens")
        if isinstance(tt, int):
            if self.context_tokens_first is None:
                self.context_tokens_first = tt
            self.context_tokens_last = tt
        cid = ev.get("chunk_id")
        if isinstance(cid, int):
            self.chunk_ids.append(cid)

    def as_dict(self) -> dict[str, Any]:
        duration_ms = None
        if self.first_ms is not None and self.last_ms is not None:
            duration_ms = max(0.0, self.last_ms - self.first_ms)
        # Prefer stream_start → last for full phase wall time
        if self.stream_start_ms is not None and self.last_ms is not None:
            wall_ms = max(0.0, self.last_ms - self.stream_start_ms)
        else:
            wall_ms = duration_ms

        chars_per_sec = None
        chunks_per_sec = None
        if wall_ms and wall_ms > 0:
            sec = wall_ms / 1000.0
            chars_per_sec = round(self.char_count / sec, 3)
            chunks_per_sec = round(self.chunk_count / sec, 3)

        return {
            "kind": self.kind,
            "prompt_id": self.prompt_id,
            "chunk_count": self.chunk_count,
            "char_count": self.char_count,
            "line_count": self.line_count,
            "duration_ms_first_to_last": duration_ms,
            "duration_ms_stream_start_to_last": wall_ms,
            "chars_per_sec": chars_per_sec,
            "chunks_per_sec": chunks_per_sec,
            "context_total_tokens_first": self.context_tokens_first,
            "context_total_tokens_last": self.context_tokens_last,
            "context_token_delta": (
                (self.context_tokens_last - self.context_tokens_first)
                if self.context_tokens_first is not None and self.context_tokens_last is not None
                else None
            ),
            "chunk_id_min": min(self.chunk_ids) if self.chunk_ids else None,
            "chunk_id_max": max(self.chunk_ids) if self.chunk_ids else None,
            "note": (
                "context_total_tokens is a context-window snapshot, not generated "
                "output tokens. Official gen rates are on turn_completed.usage."
            ),
        }


def extract(
    updates_path: Path,
    *,
    text_limit: int = 120,
    include_text: bool = False,
    kinds: Optional[set[str]] = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    events: list[dict[str, Any]] = []
    counts: dict[str, int] = defaultdict(int)
    phases: list[StreamPhase] = []
    current_phase: Optional[StreamPhase] = None
    prev_context: Optional[int] = None
    context_jumps: list[dict[str, Any]] = []
    turns: list[dict[str, Any]] = []

    for line_no, raw in iter_raw_records(updates_path):
        ev = normalize_event(
            line_no, raw, text_limit=text_limit, include_text=include_text
        )
        if ev is None:
            continue
        kind = ev["kind"]
        if kinds and kind not in kinds and kind != "parse_error":
            continue

        # context delta between successive events that carry totalTokens
        tt = ev.get("context_total_tokens")
        if isinstance(tt, int):
            if prev_context is not None and tt != prev_context:
                jump = {
                    "from": prev_context,
                    "to": tt,
                    "delta": tt - prev_context,
                    "at_kind": kind,
                    "source_line": line_no,
                    "prompt_id": ev.get("prompt_id"),
                    "agent_timestamp_ms": ev.get("agent_timestamp_ms"),
                }
                context_jumps.append(jump)
                ev["context_token_delta_from_prev"] = tt - prev_context
            else:
                ev["context_token_delta_from_prev"] = 0 if prev_context is not None else None
            prev_context = tt

        # stream phase aggregation
        if kind in ("agent_thought_chunk", "agent_message_chunk"):
            pid = ev.get("prompt_id")
            if (
                current_phase is None
                or current_phase.kind != kind
                or current_phase.prompt_id != pid
            ):
                if current_phase is not None:
                    phases.append(current_phase)
                current_phase = StreamPhase(kind=kind, prompt_id=pid)
            current_phase.add(ev)
        else:
            if current_phase is not None:
                phases.append(current_phase)
                current_phase = None

        if kind == "turn_completed" and ev.get("usage"):
            turns.append(
                {
                    "prompt_id": ev.get("prompt_id"),
                    "source_line": line_no,
                    "agent_timestamp_ms": ev.get("agent_timestamp_ms"),
                    "stop_reason": ev.get("stop_reason"),
                    **ev["usage"],
                }
            )

        counts[kind] += 1
        events.append(ev)

    if current_phase is not None:
        phases.append(current_phase)

    # Recompute estimates with peak context per turn (from stream totalTokens)
    try:
        from hierarchy import HierarchyBuilder
        from pricing import estimate_from_usage as _est
    except ImportError:
        HierarchyBuilder = None  # type: ignore
        _est = None

    hierarchy_rounds: list[dict[str, Any]] = []
    if HierarchyBuilder is not None:
        hb = HierarchyBuilder()
        for line_no, raw in iter_raw_records(updates_path):
            if "_parse_error" in raw:
                continue
            hb.feed_raw(raw)
        hierarchy_rounds = hb.snapshot_rounds(include_open=True)
        # map completed rounds with usage → re-estimate
        by_prompt = {
            r.get("prompt_id"): r
            for r in hierarchy_rounds
            if r.get("completed") and r.get("prompt_id")
        }
        for t in turns:
            peak = None
            r = by_prompt.get(t.get("prompt_id"))
            if r:
                peak = r.get("context_peak")
                t["peak_context_tokens"] = peak
                t["context_start"] = r.get("context_start")
                t["context_end"] = r.get("context_end")
                t["context_delta"] = r.get("context_delta")
                t["model_step_count"] = r.get("model_step_count") or len(
                    r.get("model_steps") or []
                )
            if _est is not None and t.get("input_tokens") is not None:
                usage_like = {
                    "inputTokens": t.get("input_tokens"),
                    "outputTokens": t.get("output_tokens"),
                    "reasoningTokens": t.get("reasoning_tokens"),
                    "cachedReadTokens": t.get("cached_read_tokens"),
                    "modelCalls": t.get("model_calls"),
                }
                est = _est(usage_like, peak_context_tokens=peak)
                t["estimate"] = est
                t["tier"] = est.get("tier")
                t["context_tokens_for_tier"] = est.get("context_tokens_for_tier")
                t["tier_method"] = est.get("tier_resolution", {}).get("method")

    summary = {
        "source": str(updates_path),
        "session_id": events[0].get("session_id") if events else None,
        "event_count": len(events),
        "counts_by_kind": dict(sorted(counts.items(), key=lambda x: (-x[1], x[0]))),
        "stream_phases": [p.as_dict() for p in phases],
        "context_jumps": context_jumps,
        "turns": turns,
        "hierarchy_rounds": hierarchy_rounds,
        "field_notes": {
            "context_total_tokens": (
                "From stream/tool event _meta.totalTokens. This is a snapshot of "
                "context size (often flat across a stream of chunks, jumps after tools)."
            ),
            "usage.output_tokens / reasoning_tokens": (
                "Only reliable on turn_completed (whole round). Rates use apiDurationMs. "
                "Sub-steps have context deltas only."
            ),
            "context_tokens_for_tier": (
                "Peak _meta.totalTokens during the round (not input+cache, not sum of "
                "modelCalls). Fallback: input/modelCalls."
            ),
            "cached_read_tokens": "Subset of inputTokens (prompt-cache hits), not full context.",
            "chunk_id": "Stream chunk sequence id for thought/message TUI lines.",
            "char_count / line_count": (
                "Derived from chunk text; used for stream chars/sec until official "
                "per-chunk token counts exist."
            ),
            "agent_timestamp_ms": "Wall clock for the event (best for rates).",
            "stream_start_ms / turn_start_ms": "Phase anchors on stream events.",
        },
    }
    return events, summary


def print_human_summary(summary: dict[str, Any]) -> None:
    print(f"source: {summary['source']}")
    print(f"session_id: {summary.get('session_id')}")
    print(f"events: {summary['event_count']}")
    print("counts:")
    for k, v in summary["counts_by_kind"].items():
        print(f"  {k:28} {v}")
    print()
    print("turns (official usage + rates):")
    if not summary["turns"]:
        print("  (none — turn may still be in progress)")
    for t in summary["turns"]:
        cost = t.get("cost_usd")
        cost_s = f"${cost:.6f}" if isinstance(cost, (int, float)) else "n/a"
        ticks = t.get("cost_usd_ticks")
        print(
            f"  prompt={t.get('prompt_id')}  "
            f"in={t.get('input_tokens')} out={t.get('output_tokens')} "
            f"reason={t.get('reasoning_tokens')} cache={t.get('cached_read_tokens')} "
            f"api_ms={t.get('api_duration_ms')}  "
            f"out/s={t.get('output_tokens_per_sec')} "
            f"reason/s={t.get('reasoning_tokens_per_sec')} "
            f"gen/s={t.get('gen_tokens_per_sec')}  "
            f"cost={cost_s} ticks={ticks}"
        )
    print()
    print("stream phases (chars/sec from timestamps; context tokens are snapshots):")
    for p in summary["stream_phases"]:
        print(
            f"  {p['kind']:22} chunks={p['chunk_count']:4} "
            f"chars={p['char_count']:6} lines={p['line_count']:4} "
            f"wall_ms={p['duration_ms_stream_start_to_last']} "
            f"chars/s={p['chars_per_sec']} "
            f"ctx={p['context_total_tokens_first']}→{p['context_total_tokens_last']}"
        )
    print()
    print(f"context jumps: {len(summary['context_jumps'])}")
    for j in summary["context_jumps"][:20]:
        print(
            f"  line {j['source_line']}: {j['from']} → {j['to']} "
            f"(Δ{j['delta']:+d}) @ {j['at_kind']}"
        )
    if len(summary["context_jumps"]) > 20:
        print(f"  … +{len(summary['context_jumps']) - 20} more")


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Extract Grok Build token telemetry events")
    ap.add_argument("--session-id", help="Session UUID under ~/.grok/sessions")
    ap.add_argument("--updates", type=Path, help="Path to updates.jsonl")
    ap.add_argument(
        "--sessions-root",
        type=Path,
        default=default_sessions_root(),
        help="Root of Grok sessions (default: ~/.grok/sessions)",
    )
    ap.add_argument(
        "--out",
        type=Path,
        help="Write one normalized event per line (JSONL)",
    )
    ap.add_argument(
        "--summary-out",
        type=Path,
        help="Write summary JSON (phases, turns, jumps)",
    )
    ap.add_argument(
        "--summary",
        action="store_true",
        help="Print human-readable summary to stdout",
    )
    ap.add_argument(
        "--kinds",
        help="Comma-separated kinds to keep (default: all)",
    )
    ap.add_argument(
        "--smallest-only",
        action="store_true",
        help="Only stream chunks: agent_thought_chunk, agent_message_chunk, user_message_chunk",
    )
    ap.add_argument(
        "--include-text",
        action="store_true",
        help="Include full chunk text (can be large)",
    )
    ap.add_argument(
        "--text-limit",
        type=int,
        default=120,
        help="Preview length for text_preview (default 120)",
    )
    ap.add_argument(
        "--sample",
        type=int,
        default=0,
        help="Print N sample events as pretty JSON to stdout",
    )
    args = ap.parse_args(argv)

    updates = resolve_updates_path(args.session_id, args.updates, args.sessions_root)

    kinds = None
    if args.smallest_only:
        kinds = set(STREAM_TYPES)
    elif args.kinds:
        kinds = {k.strip() for k in args.kinds.split(",") if k.strip()}

    events, summary = extract(
        updates,
        text_limit=args.text_limit,
        include_text=args.include_text,
        kinds=kinds,
    )

    # default out path next to repo out/
    if args.out is None and args.summary_out is None and not args.summary and not args.sample:
        # always produce something useful
        args.summary = True
        repo_out = Path(__file__).resolve().parent.parent / "out"
        repo_out.mkdir(parents=True, exist_ok=True)
        sid = summary.get("session_id") or "session"
        args.out = repo_out / f"{sid}-events.jsonl"
        args.summary_out = repo_out / f"{sid}-summary.json"

    if args.out:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        with args.out.open("w", encoding="utf-8") as f:
            for ev in events:
                f.write(json.dumps(ev, ensure_ascii=False) + "\n")
        print(f"wrote {len(events)} events → {args.out}", file=sys.stderr)

    if args.summary_out:
        args.summary_out.parent.mkdir(parents=True, exist_ok=True)
        args.summary_out.write_text(
            json.dumps(summary, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        print(f"wrote summary → {args.summary_out}", file=sys.stderr)

    if args.summary:
        print_human_summary(summary)

    if args.sample:
        for ev in events[: args.sample]:
            print(json.dumps(ev, indent=2, ensure_ascii=False))
            print("---")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
