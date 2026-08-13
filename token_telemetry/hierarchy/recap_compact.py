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

    # Immediate estimate: FULL pre-compact context had to be in the prompt (tokens_before).
    # At large windows this is mostly cache hits — use cache rate as lower bound until
    # we refine from the last call of the previous round.
    pre_read_usd = None
    pre_read_cache_usd = None
    if isinstance(before_i, int) and before_i > 0:
        try:
            from token_telemetry.pricing import estimate_cost_usd

            e = estimate_cost_usd(
                input_tokens=before_i,
                output_tokens=0,
                cached_read_tokens=before_i,
                peak_context_tokens=before_i,
                model_calls=1,
            )
            pre_read_cache_usd = float(e["cost_usd"]["cached_input"])
            pre_read_usd = pre_read_cache_usd
        except Exception:
            from token_telemetry.pricing import pick_tier

            rate = pick_tier(before_i)["cached_input"]
            pre_read_cache_usd = before_i * rate / 1_000_000.0
            pre_read_usd = pre_read_cache_usd

    compact = {
        "kind": "compaction",
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
        "pre_read_note": (
            "Read of the FULL pre-compact context (tokens_before) — what the model "
            "had to hold/read before compact. Refined from last call cache split when known."
        ),
        # Filled once the next round runs (tools/system rehydration → next In)
        "deferred_reload_tokens": None,
        "deferred_reload_usd": None,
        "deferred_reload_note": (
            "Post-compact prompt reload (tools/system/summary) paid as In on "
            "the next call(s); estimated after the following round starts."
        ),
        "cost_usd": round(pre_read_usd, 8) if pre_read_usd is not None else None,
        "cost_note": (
            "Compact $ = pre-read(full ctx before) + reload tools/system. "
            "Kept-window re-read is on the next user prompt, not here."
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

    # Attach to the right place:
    # 1) open round mid-flight → note + fix its context_end
    # 2) between completed rounds → compact_after on last + pending for next
    # 3) else buffer for next round as pending_compact
    if hb._open is not None:
        r = hb._open
        r.setdefault("notes", []).append(compact)
        r.setdefault("compactions", []).append(compact)
        if isinstance(after_i, int):
            r["context_end"] = after_i
            # context_start stays; delta will reflect compact on finalize
            if r.get("context_start") is None and isinstance(before_i, int):
                r["context_start"] = before_i
        return

    if hb.rounds:
        last = hb.rounds[-1]
        # First-class between-round card (UI renders between R[n] and R[n+1])
        last["compact_after"] = compact
        last.setdefault("notes", []).append(compact)
        last.setdefault("compactions", []).append(compact)
        if isinstance(after_i, int):
            last["context_after_compact"] = after_i
            # Keep pre-compact context_end for the round's own story; peak stays.
        # Also pending so the next round owns compact_before (same dict ref)
        hb._pending_compact = compact
        hb._bump()
        return

    # No rounds yet — stash for next round start
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


def _fill_compact_cost(hb: Any, r: dict[str, Any]) -> None:
    """
    Compact bill (between-round card):

      1) pre-read of FULL context before compact (tokens_before)
      2) deferred reload (tools/system/summary snap-back) as uncached In

    Kept-window re-read (tokens_after) is NOT here — it lands on the next
    user-prompt row. total_usd = pre_read_usd + deferred_reload_usd
    """
    compact = r.get("compact_before")
    if not isinstance(compact, dict) or compact.get("kind") != "compaction":
        return

    def _as_int(v: Any) -> Optional[int]:
        try:
            return int(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    before_i = _as_int(compact.get("tokens_before"))
    after_i = _as_int(compact.get("tokens_after"))

    steps = r.get("model_steps") or []
    s0 = steps[0] if steps and isinstance(steps[0], dict) else None
    est0 = (s0.get("estimate") if s0 else None) or {}
    if not isinstance(est0, dict):
        est0 = {}

    # Last completed round before this one (for pre-compact cache split)
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

    def _split_from_est(total: int, est: dict[str, Any]) -> tuple[int, int]:
        """Split total tokens into (uncached, cached) using an estimate's ratio."""
        if total <= 0:
            return 0, 0
        log_cache = est.get("logical_cached_tokens")
        log_in = est.get("logical_input_tokens")
        paid_cache = est.get("cached_read_tokens")
        paid_in = est.get("input_tokens")
        if isinstance(log_cache, int) and isinstance(log_in, int) and log_in > 0:
            frac = min(1.0, max(0.0, log_cache / max(log_in, 1)))
            c = int(round(total * frac))
            return max(0, total - c), c
        if isinstance(paid_cache, int) and isinstance(paid_in, int) and paid_in > 0:
            frac = min(1.0, max(0.0, paid_cache / max(paid_in, 1)))
            c = int(round(total * frac))
            return max(0, total - c), c
        # Default: treat as fully cached (lower bound for large pre-compact windows)
        return 0, total

    # --- 1) Pre-read FULL context before compact (tokens_before) ---
    pre_tok = max(0, before_i or 0)
    if pre_tok > 0:
        prev_est: dict[str, Any] = {}
        if prev:
            psteps = prev.get("model_steps") or []
            if psteps:
                prev_est = (psteps[-1].get("estimate") or {}) if isinstance(psteps[-1], dict) else {}
        pre_unc, pre_cache = _split_from_est(pre_tok, prev_est if isinstance(prev_est, dict) else {})
        u_usd, c_usd, pre_usd = _price(pre_unc, pre_cache, pre_tok)
        compact["pre_read_tokens"] = pre_tok
        compact["pre_read_uncached_tokens"] = pre_unc
        compact["pre_read_cached_tokens"] = pre_cache
        compact["pre_read_uncached_usd"] = round(u_usd, 8)
        compact["pre_read_cached_usd"] = round(c_usd, 8)
        compact["pre_read_usd"] = round(pre_usd, 8)
        compact["pre_read_note"] = (
            "Read of the FULL pre-compact context (tokens_before) — the window "
            "the model had to hold before compact fired."
        )
    else:
        compact["pre_read_tokens"] = 0
        compact["pre_read_usd"] = 0.0

    tier_after = max(0, after_i or 0) or pre_tok or 1

    # --- 2) Deferred reload (tools/system/summary) ---
    rehyd = 0
    if s0 is not None:
        for ch in s0.get("children") or []:
            if not isinstance(ch, dict) or ch.get("kind") != "phase_harness":
                continue
            for sub in ch.get("children") or []:
                if isinstance(sub, dict) and sub.get("kind") == "caused_in_residual":
                    rehyd += int(sub.get("tokens_in") or sub.get("context_delta") or 0)
        if rehyd <= 0:
            emit = int(s0.get("model_emit_delta") or 0)
            arg_emit = sum(
                int(t.get("arg_tokens_est") or 0) for t in (s0.get("tools") or [])
            )
            if emit > max(arg_emit, 1) * 3:
                rehyd = max(0, emit - max(arg_emit, 0))
            elif est0:
                caused = int(est0.get("uncached_input_tokens") or 0)
                tools = sum(
                    int(t.get("context_delta") or t.get("tokens_in") or 0)
                    for t in (s0.get("tools") or [])
                )
                rehyd = max(0, caused - tools)
        caused0 = int(est0.get("uncached_input_tokens") or s0.get("tokens_in") or 0)
        if caused0 > 0:
            rehyd = min(rehyd, caused0)

    if rehyd > 0:
        _, _, rehyd_usd = _price(rehyd, 0, tier_after or rehyd)
        compact["deferred_reload_tokens"] = int(rehyd)
        compact["deferred_reload_usd"] = round(rehyd_usd, 8)
        compact["deferred_reload_note"] = (
            "Post-compact rehydration (tools/system/summary) paid as uncached In "
            "on the next call(s). Moved off Call 1 onto this Compact row."
        )
        # Strip from call-1 caused In so Compact owns that slice
        if s0 is not None and est0:
            try:
                peak = int(
                    est0.get("logical_input_tokens")
                    or est0.get("input_tokens")
                    or tier_after
                    or rehyd
                )
                for key in ("uncached_input_tokens", "logical_uncached_tokens"):
                    v = est0.get(key)
                    if isinstance(v, int) and v > 0:
                        est0[key] = max(0, v - rehyd)
                new_unc = int(est0.get("uncached_input_tokens") or 0)
                new_in_usd, _, _ = _price(new_unc, 0, peak)
                est0["cost_in_usd"] = round(new_in_usd, 8)
                log_unc = int(est0.get("logical_uncached_tokens") or 0)
                log_in_usd, _, _ = _price(log_unc, 0, peak)
                est0["cost_in_logical_usd"] = round(log_in_usd, 8)
                cache_usd = float(est0.get("cost_cached_usd") or 0)
                out_usd = float(est0.get("cost_out_usd") or 0)
                est0["estimate_usd"] = round(new_in_usd + cache_usd + out_usd, 8)
                est0["api_call_usd"] = est0["estimate_usd"]
                if isinstance(est0.get("cost_usd"), dict):
                    est0["cost_usd"]["uncached_input"] = round(new_in_usd, 8)
                    est0["cost_usd"]["input"] = round(new_in_usd, 8)
                    est0["cost_usd"]["total"] = est0["estimate_usd"]
                s0["tokens_in"] = new_unc
                s0["cost_in_usd"] = est0["cost_in_usd"]
                s0["estimate_usd"] = est0["estimate_usd"]

                left = rehyd
                for ch in s0.get("children") or []:
                    if not isinstance(ch, dict) or ch.get("kind") != "phase_harness":
                        continue
                    kept_sub = []
                    for sub in ch.get("children") or []:
                        if not isinstance(sub, dict):
                            continue
                        if sub.get("kind") == "caused_in_residual" and left > 0:
                            tin = int(
                                sub.get("tokens_in") or sub.get("context_delta") or 0
                            )
                            take = min(tin, left)
                            new_t = max(0, tin - take)
                            left -= take
                            if new_t <= 0:
                                continue
                            sub = dict(sub)
                            sub["tokens_in"] = new_t
                            sub["context_delta"] = new_t
                            su, _, _ = _price(new_t, 0, peak)
                            sub["cost_in_usd"] = round(su, 8)
                            sub["estimate_usd"] = round(su, 8)
                        kept_sub.append(sub)
                    ch["children"] = kept_sub
                    h_tok = new_unc if new_unc > 0 else sum(
                        int(s.get("tokens_in") or 0) for s in kept_sub
                    )
                    hu, _, _ = _price(h_tok, 0, peak)
                    ch["tokens_in"] = h_tok
                    ch["cost_in_usd"] = round(hu, 8)
                    ch["estimate_usd"] = round(hu, 8)
            except Exception:
                pass
    else:
        compact.setdefault("deferred_reload_tokens", None)
        compact.setdefault("deferred_reload_usd", None)

    # --- Total compact cost (no kept re-read — that is next user prompt) ---
    pre_usd = float(compact.get("pre_read_usd") or 0)
    def_usd = float(compact.get("deferred_reload_usd") or 0)
    compact["cost_usd"] = round(pre_usd + def_usd, 8)
    compact["cost_note"] = (
        "Compact $ = pre-read(full ctx before) + reload tools/system. "
        "Kept-window re-read appears on the next user prompt."
    )
