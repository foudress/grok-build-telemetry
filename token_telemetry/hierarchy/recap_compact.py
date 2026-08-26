"""Session recap + auto-compact card logic (between-round / fork cards).

Free functions take a HierarchyBuilder-like object as first arg ``hb``.
HierarchyBuilder methods are thin wrappers so behavior stays identical.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from token_telemetry.tokenizer import count_tokens
from token_telemetry.hierarchy.text_metrics import (
    _extract_recap_prompt_text,
    _preview,
)


def _on_recap(hb: Any, update: dict[str, Any], agent_ms: Any) -> None:
    """
    Harness auto recap on a *fork* of the session (does not enter chat
    history / next-round growth).

    Bill model (UI card, same shell as Compact):
      · full context window → Cached read
      · recap system-reminder prompt → In (uncached)
      · recap summary message → Out

    Never mutates _last_ctx / _cache_baseline / context_end.
    """
    summary = str(update.get("summary") or "")
    auto = bool(update.get("auto", True))

    # Context size at end of last turn (fork re-reads full session ctx)
    ctx: Optional[int] = None
    if isinstance(hb._last_ctx, int) and hb._last_ctx > 0:
        ctx = hb._last_ctx
    elif hb.rounds:
        end = hb.rounds[-1].get("context_end")
        if isinstance(end, int) and end > 0:
            ctx = end
    if ctx is None and isinstance(hb._session_peak, int):
        ctx = hb._session_peak
    ctx_i = max(0, int(ctx or 0))

    prompt_tok, prompt_preview, request_id = _recap_prompt_info(hb, summary)
    out_tok = int(count_tokens(summary)) if summary else 0

    pre_usd = prompt_usd = out_usd = total_usd = None
    try:
        from token_telemetry.pricing import estimate_cost_usd

        e = estimate_cost_usd(
            input_tokens=ctx_i + max(0, prompt_tok),
            output_tokens=out_tok,
            cached_read_tokens=ctx_i,
            peak_context_tokens=max(ctx_i + max(0, prompt_tok), 1),
            model_calls=1,
        )
        pre_usd = float(e["cost_usd"]["cached_input"])
        prompt_usd = float(e["cost_usd"]["uncached_input"])
        out_usd = float(e["cost_usd"]["output"])
        total_usd = float(e["cost_usd"]["total"])
    except Exception:
        from token_telemetry.pricing import pick_tier

        t = pick_tier(ctx_i)
        rate_c, rate_u, rate_o = t["cached_input"], t["input"], t["output"]
        pre_usd = ctx_i * rate_c / 1e6
        prompt_usd = max(0, prompt_tok) * rate_u / 1e6
        out_usd = out_tok * rate_o / 1e6
        total_usd = pre_usd + prompt_usd + out_usd

    recap = {
        "kind": "session_recap",
        "auto": auto,
        "summary_preview": _preview(summary, 100),
        "summary": summary[:400] if summary else "",
        "context_tokens": ctx_i or None,
        "context_cached_tokens": ctx_i or None,
        "prompt_tokens": prompt_tok if prompt_tok > 0 else None,
        "prompt_preview": prompt_preview,
        "out_tokens": out_tok if out_tok > 0 else None,
        "pre_read_cached_usd": round(pre_usd, 8) if pre_usd is not None else None,
        "prompt_in_usd": round(prompt_usd, 8) if prompt_usd is not None else None,
        "out_usd": round(out_usd, 8) if out_usd is not None else None,
        "cost_usd": round(total_usd, 8) if total_usd is not None else None,
        "cost_note": (
            "Fork recap (isolated): full ctx as Cached · recap prompt as In · "
            "summary as Out. Does not grow session context or next-round In."
        ),
        "fork_isolated": True,
        "request_id": request_id,
        "agent_ms": agent_ms,
        # Bind to session so pending never paints onto another attach
        "session_id": hb._session_key(),
    }

    # Attach only as a side card — never as model_step / Out→In continuity.
    # Data on completed round (recaps_after); UI placement like compact:
    # pending → next round's recaps_before so newest-first sits *between*
    # R[n+1] and R[n] (after R[n+1] card), not under R[n] (one round early).
    if hb.rounds:
        last = hb.rounds[-1]
        last.setdefault("recaps_after", []).append(recap)
        last["recap_after"] = recap
        last.setdefault("notes", []).append(recap)
    elif hb._open is not None:
        r = hb._open
        r.setdefault("recaps_after", []).append(recap)
        r["recap_after"] = recap
        r.setdefault("notes", []).append(recap)
    hb._pending_recaps.append(recap)
    hb._bump()


def _recap_prompt_info(
    hb: Any, summary: str
) -> tuple[int, str, Optional[str]]:
    """
    Prompt tokens for the recap system-reminder (In). Prefer matching
    recap_requests/*.json **in this session only**; fallback stub.
    Requires a summary match so we never pick another recap's prompt.
    """
    summary_s = (summary or "").strip()
    if hb._session_dir and summary_s:
        rr_dir = Path(hb._session_dir) / "recap_requests"
        if rr_dir.is_dir():
            files = sorted(
                rr_dir.glob("*.json"),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
            for f in files[:80]:
                try:
                    data = json.loads(
                        f.read_text(encoding="utf-8", errors="replace")
                    )
                except (OSError, json.JSONDecodeError, UnicodeError):
                    continue
                if not isinstance(data, dict):
                    continue
                file_sum = str(
                    data.get("summary") or data.get("raw_response") or ""
                ).strip()
                if not file_sum:
                    continue
                # Require same recap body (exact or shared prefix when truncated)
                if file_sum != summary_s and not (
                    file_sum.startswith(summary_s[:80])
                    or summary_s.startswith(file_sum[:80])
                ):
                    continue
                prompt_text = _extract_recap_prompt_text(data.get("chat_history"))
                rid = str(data.get("request_id") or f.stem)
                if prompt_text:
                    tok = int(count_tokens(prompt_text))
                    return max(1, tok), _preview(prompt_text, 80), rid
                # Matched summary, no extractable prompt — still bind request id
                return (
                    max(1, int(count_tokens(
                        "Write ONE sentence recap body for a user returning from idle. "
                        "Output ONLY the body. Do NOT call any tools."
                    ))),
                    "recap prompt (est.)",
                    rid,
                )

    # Fallback: known reminder is ~1.4k chars / ~350 tok (see Learning)
    stub = (
        "Write ONE sentence recap body for a user returning from idle. "
        "Output ONLY the body. Do NOT call any tools."
    )
    return max(1, int(count_tokens(stub))), "recap prompt (est.)", None


def _on_compact(hb: Any, update: dict[str, Any], agent_ms: Any) -> None:
    before = update.get("tokens_before")
    after = update.get("tokens_after")
    try:
        before_i = int(before) if before is not None else None
    except (TypeError, ValueError):
        before_i = None
    try:
        after_i = int(after) if after is not None else None
    except (TypeError, ValueError):
        after_i = None

    removed = (
        max(0, before_i - after_i)
        if isinstance(before_i, int) and isinstance(after_i, int)
        else None
    )

    # Default: warm compact = full window as Cached + compressed Out.
    # _fill_compact_cost flips Cached → In on a miss and prices Out.
    pre_read_usd = None
    pre_read_cache_usd = None
    out_usd = None
    out_tok = after_i if isinstance(after_i, int) and after_i > 0 else 0
    if isinstance(before_i, int) and before_i > 0:
        try:
            from token_telemetry.pricing import estimate_cost_usd

            e = estimate_cost_usd(
                input_tokens=before_i,
                output_tokens=int(out_tok or 0),
                cached_read_tokens=before_i,
                peak_context_tokens=before_i,
                model_calls=1,
            )
            pre_read_cache_usd = float(e["cost_usd"]["cached_input"])
            pre_read_usd = pre_read_cache_usd
            out_usd = float(e["cost_usd"]["output"])
        except Exception:
            from token_telemetry.pricing import pick_tier

            t = pick_tier(before_i)
            pre_read_cache_usd = before_i * t["cached_input"] / 1_000_000.0
            pre_read_usd = pre_read_cache_usd
            out_usd = int(out_tok or 0) * t["output"] / 1_000_000.0

    compact = {
        "kind": "compaction",
        "auto": True,
        "tokens_before": before_i,
        "tokens_after": after_i,
        "tokens_removed": removed,
        "context_delta": (
            (after_i - before_i)
            if isinstance(before_i, int) and isinstance(after_i, int)
            else None
        ),
        # Full context read BEFORE compact (the window that was billed to reach compact)
        "pre_read_tokens": before_i if isinstance(before_i, int) else None,
        "pre_read_cached_tokens": before_i if isinstance(before_i, int) else None,
        "pre_read_uncached_tokens": 0 if isinstance(before_i, int) else None,
        "pre_read_cached_usd": (
            round(pre_read_cache_usd, 8) if pre_read_cache_usd is not None else None
        ),
        "pre_read_uncached_usd": 0.0 if isinstance(before_i, int) else None,
        "pre_read_usd": round(pre_read_usd, 8) if pre_read_usd is not None else None,
        "pre_read_cache_miss": False,
        "out_tokens": int(out_tok) if out_tok else None,
        "out_usd": round(out_usd, 8) if out_usd is not None else None,
        "pre_read_note": (
            "Read of the FULL pre-compact context (tokens_before). "
            "Cached on hit, In on miss — never both. Out = compressed history."
        ),
        # Filled once the next round runs (tools/system rehydration → next In)
        "deferred_reload_tokens": None,
        "deferred_reload_usd": None,
        "deferred_reload_note": (
            "Post-compact prompt reload (tools/system/summary) paid as In on "
            "the next call(s); estimated after the following round starts."
        ),
        "cost_usd": round(
            (pre_read_usd or 0) + (out_usd or 0), 8
        ) if pre_read_usd is not None or out_usd is not None else None,
        "cost_note": (
            "Compact $ = (Cached hit | In miss) of tokens_before + Out "
            "(compressed history ≈ tokens_after)."
        ),
        "summary_preview": _preview(str(update.get("summary_preview") or ""), 80),
        "agent_ms": agent_ms,
        "session_id": hb._session_key(),
    }
    hb._last_compact = compact

    # Session cursor + cache baseline drop to post-compact size
    if isinstance(after_i, int):
        hb._last_ctx = after_i
        hb._cache_baseline = after_i

    open_steps = []
    if hb._open is not None:
        open_steps = [
            s for s in (hb._open.get("model_steps") or []) if isinstance(s, dict)
        ]

    def _stamp_after_index(step: Optional[dict[str, Any]]) -> None:
        if isinstance(step, dict) and step.get("index") is not None:
            compact["after_step_index"] = step.get("index")

    # Placement:
    # 1) open round with LLM calls → mid-round card on last step (do not
    #    stomp round.context_end; do not also pending as between-rounds)
    # 2) open round but zero steps → compact_before on this round
    # 3) between completed rounds → compact_after on last + pending
    # 4) else buffer for next round
    if hb._open is not None and open_steps:
        r = hb._open
        last_s = open_steps[-1]
        compact["placement"] = "mid_round"
        _stamp_after_index(last_s)
        last_s.setdefault("compacts_after", []).append(compact)
        r.setdefault("notes", []).append(compact)
        r.setdefault("compactions", []).append(compact)
        r["mid_round_compacts"] = True
        if r.get("context_start") is None and isinstance(before_i, int):
            r["context_start"] = before_i
        if isinstance(after_i, int):
            r["context_after_compact"] = after_i
        hb._bump()
        return

    if hb._open is not None and not open_steps:
        r = hb._open
        compact["placement"] = "between_rounds"
        if hb.rounds:
            prev_steps = [
                s
                for s in (hb.rounds[-1].get("model_steps") or [])
                if isinstance(s, dict)
            ]
            _stamp_after_index(prev_steps[-1] if prev_steps else None)
            last = hb.rounds[-1]
            last["compact_after"] = compact
            last.setdefault("notes", []).append(compact)
            last.setdefault("compactions", []).append(compact)
            if isinstance(after_i, int):
                last["context_after_compact"] = after_i
        r["compact_before"] = compact
        r.setdefault("compactions", []).append(compact)
        hb._bump()
        return

    if hb.rounds:
        last = hb.rounds[-1]
        compact["placement"] = "between_rounds"
        prev_steps = [
            s for s in (last.get("model_steps") or []) if isinstance(s, dict)
        ]
        _stamp_after_index(prev_steps[-1] if prev_steps else None)
        last["compact_after"] = compact
        last.setdefault("notes", []).append(compact)
        last.setdefault("compactions", []).append(compact)
        if isinstance(after_i, int):
            last["context_after_compact"] = after_i
        hb._pending_compact = compact
        hb._bump()
        return

    compact["placement"] = "between_rounds"
    hb._pending_compact = compact


def _attach_pending_recap_compact(hb: Any) -> None:
    """Bind buffered between-turn compact/recap cards onto the open round."""
    sid = hb._session_key()
    pending = hb._pending_compact
    if pending:
        # Drop foreign-session compact (attach/reset should already clear)
        if pending.get("session_id") and sid and pending.get("session_id") != sid:
            hb._pending_compact = None
        else:
            if isinstance(pending, dict) and not pending.get("session_id"):
                pending["session_id"] = sid
            # Between-round compact: same object as previous round's compact_after
            hb._open["compact_before"] = pending
            # notes already on previous round — do not re-append (avoids note bloat)
            hb._pending_compact = None
    # Recaps after previous turn → sit before this round (UI between R[n] and R[n-1]
    # when newest-first renders recaps_before after this card).
    if hb._pending_recaps:
        mine = [
            c for c in hb._pending_recaps
            if isinstance(c, dict)
            and (
                not c.get("session_id")
                or not sid
                or c.get("session_id") == sid
            )
        ]
        if mine:
            hb._open.setdefault("recaps_before", []).extend(mine)
        hb._pending_recaps = []


def _as_int(v: Any) -> Optional[int]:
    try:
        return int(v) if v is not None else None
    except (TypeError, ValueError):
        return None


def _compacts_to_fill(r: dict[str, Any]) -> list[dict[str, Any]]:
    """Unique compaction dicts on this round (between-rounds + mid-round)."""
    out: list[dict[str, Any]] = []
    seen: set[int] = set()

    def add(c: Any) -> None:
        if not isinstance(c, dict) or c.get("kind") != "compaction":
            return
        key = id(c)
        if key in seen:
            return
        seen.add(key)
        out.append(c)

    add(r.get("compact_before"))
    for s in r.get("model_steps") or []:
        if not isinstance(s, dict):
            continue
        for c in s.get("compacts_after") or []:
            add(c)
    return out


def _fill_compact_cost(hb: Any, r: dict[str, Any]) -> None:
    """
    Compact bill:

      1) pre-read of FULL context: Cached on hit, In on miss (never both)
      2) Out = compressed history (tokens_after)

    Compact Out re-entry + post-compact tools stay on the next harness
    (compact_out_in). Do not steal that mass onto deferred_reload.
    """
    compacts = _compacts_to_fill(r)
    if not compacts:
        return

    prev = None
    if r in hb.rounds:
        idx = hb.rounds.index(r)
        prev = hb.rounds[idx - 1] if idx > 0 else None
    elif hb.rounds:
        prev = hb.rounds[-1]

    try:
        from token_telemetry.pricing import estimate_cost_usd
    except Exception:
        estimate_cost_usd = None  # type: ignore[assignment]

    def _price(unc: int, cache: int, peak: int) -> tuple[float, float, float]:
        if estimate_cost_usd is None:
            hi = peak > 200_000
            ru = 4.0 if hi else 2.0
            rc = 0.6 if hi else 0.3
            return unc * ru / 1e6, cache * rc / 1e6, (unc * ru + cache * rc) / 1e6
        e = estimate_cost_usd(
            input_tokens=max(0, unc) + max(0, cache),
            output_tokens=0,
            cached_read_tokens=max(0, cache),
            peak_context_tokens=max(peak, 1),
            model_calls=1,
        )
        cu = float(e["cost_usd"]["uncached_input"])
        cc = float(e["cost_usd"]["cached_input"])
        return cu, cc, cu + cc

    miss = False
    if isinstance(prev, dict):
        up = prev.get("user_prompt") if isinstance(prev.get("user_prompt"), dict) else {}
        bd_prev = prev.get("breakdown") if isinstance(prev.get("breakdown"), dict) else {}
        miss = bool(
            prev.get("session_restart")
            or prev.get("cache_miss")
            or prev.get("context_reread")
            or up.get("session_restart")
            or up.get("cache_miss")
            or up.get("context_reread")
            or bd_prev.get("context_reread")
        )

    for compact in compacts:
        before_i = _as_int(compact.get("tokens_before"))
        after_i = _as_int(compact.get("tokens_after"))
        pre_tok = max(0, before_i or 0)
        if pre_tok > 0:
            if miss:
                pre_unc, pre_cache = pre_tok, 0
            else:
                pre_unc, pre_cache = 0, pre_tok
            u_usd, c_usd, pre_usd = _price(pre_unc, pre_cache, pre_tok)
            compact["pre_read_tokens"] = pre_tok
            compact["pre_read_uncached_tokens"] = pre_unc
            compact["pre_read_cached_tokens"] = pre_cache
            compact["pre_read_uncached_usd"] = round(u_usd, 8)
            compact["pre_read_cached_usd"] = round(c_usd, 8)
            compact["pre_read_usd"] = round(pre_usd, 8)
            compact["pre_read_cache_miss"] = bool(miss)
            compact["pre_read_note"] = (
                "Cache miss: full tokens_before billed as In."
                if miss
                else "Cache hit: full tokens_before billed as Cached."
            )
        else:
            compact["pre_read_tokens"] = 0
            compact["pre_read_usd"] = 0.0
            compact["pre_read_cache_miss"] = bool(miss)

        # Compact Out stays on this row; next harness owns re-entry.
        compact["deferred_reload_tokens"] = None
        compact["deferred_reload_usd"] = None

        out_tok = 0
        if isinstance(after_i, int) and after_i > 0:
            out_tok = int(after_i)
        if out_tok > 0:
            peak_out = max(after_i or out_tok, pre_tok, 1)
            if estimate_cost_usd is None:
                hi = peak_out > 200_000
                ro = 12.0 if hi else 6.0
                o_usd = out_tok * ro / 1e6
            else:
                e_out = estimate_cost_usd(
                    input_tokens=0,
                    output_tokens=int(out_tok),
                    cached_read_tokens=0,
                    peak_context_tokens=peak_out,
                    model_calls=1,
                )
                o_usd = float(e_out["cost_usd"]["output"])
            compact["out_tokens"] = int(out_tok)
            compact["out_usd"] = round(o_usd, 8)
        else:
            compact.setdefault("out_tokens", None)
            compact.setdefault("out_usd", None)

        pre_usd = float(compact.get("pre_read_usd") or 0)
        out_usd = float(compact.get("out_usd") or 0)
        compact["cost_usd"] = round(pre_usd + out_usd, 8)
        compact["cost_note"] = (
            "Compact $ = (Cached hit | In miss) of tokens_before + Out "
            "(compressed history). Reload tools/system is next-call In."
        )
