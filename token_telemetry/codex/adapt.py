"""Map a Codex snapshot onto the dashboard wire the Grok UI already renders.

Sessions, rounds, the context chart, cost stacks, and D/W/M buckets are the
same JSON the existing page reads. ``official_usd`` stays 0: the logs have no
invoice. ``estimate_usd`` is the published API list price for that model.
Line tokens are a split of that call's ``usage``, not a second ledger.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from token_telemetry.codex.prices import price_usage, priced_model
from token_telemetry.session.windows import (
    _bucket_specs,
    normalize_grain,
    period_window,
)

_MISS_SLACK = 256

_WIRE_ROUNDS = 2000


def _epoch(ts: Any) -> Optional[float]:
    if not isinstance(ts, str) or not ts:
        return None
    text = ts.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def _int(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def _age(epoch: Optional[float], now: float) -> str:
    if epoch is None:
        return ""
    sec = max(0, int(now - epoch))
    if sec < 60:
        return f"{sec}s"
    if sec < 3600:
        return f"{sec // 60}m"
    if sec < 86400:
        return f"{sec // 3600}h"
    return f"{sec // 86400}d"


def _threads(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    rows = snapshot.get("threads") if isinstance(snapshot, dict) else None
    if not isinstance(rows, list):
        return []
    return [t for t in rows if isinstance(t, dict) and t.get("id")]


def _by_id(threads: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(t["id"]): t for t in threads}


def _children(threads: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    ids = {str(t["id"]) for t in threads}
    for thread in threads:
        parent = str(thread.get("parent_id") or "")
        if parent and parent in ids and parent != str(thread["id"]):
            out.setdefault(parent, []).append(thread)
    for kids in out.values():
        kids.sort(key=lambda t: str(t.get("updated") or ""))
    return out


def _roots(threads: list[dict[str, Any]], kids: dict[str, list[dict[str, Any]]]) -> list[dict[str, Any]]:
    child_ids = {str(t["id"]) for group in kids.values() for t in group}
    roots = [t for t in threads if str(t["id"]) not in child_ids]
    roots.sort(key=lambda t: str(t.get("updated") or ""), reverse=True)
    return roots


def _calls(thread: dict[str, Any]) -> list[dict[str, Any]]:
    rows = thread.get("calls")
    if not isinstance(rows, list):
        return []
    return [c for c in rows if isinstance(c, dict)]


def _group_turns(calls: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    order: list[str] = []
    groups: dict[str, list[dict[str, Any]]] = {}
    for i, call in enumerate(calls):
        key = str(call.get("turn_id") or "") or f"call-{i}"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(call)
    return [groups[k] for k in order]


def _sum_calls(calls: list[dict[str, Any]]) -> dict[str, int]:
    unc = cached = out = reason = 0
    for call in calls:
        unc += _int(call.get("uncached_input_tokens"))
        cached += _int(call.get("cached_input_tokens"))
        out += _int(call.get("output_tokens"))
        reason += _int(call.get("reasoning_output_tokens"))
    return {
        "uncached": unc,
        "cached": cached,
        "output": out,
        "reasoning": reason,
        "message": max(0, out - reason),
        "input": unc + cached,
    }


def _spread(total: int, weights: list[int]) -> list[int]:
    n = len(weights)
    if n == 0 or total <= 0:
        return [0] * n
    w = [x if x > 0 else 0 for x in weights]
    if sum(w) <= 0:
        w = [1] * n
    denom = sum(w)
    base = [total * x // denom for x in w]
    left = total - sum(base)
    order = sorted(range(n), key=lambda i: (total * w[i] % denom, w[i]), reverse=True)
    for i in order[:left]:
        base[i] += 1
    return base


def _ev_chars(ev: Any) -> int:
    if not isinstance(ev, dict):
        return 0
    try:
        return max(0, int(ev.get("chars") or 0))
    except (TypeError, ValueError):
        return 0


def _annotate(calls: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Split each call's official usage onto miss / user / harness / output lines."""
    notes: list[dict[str, Any]] = []
    prev_prompt = 0
    for call in calls:
        unc = _int(call.get("uncached_input_tokens"))
        cached = _int(call.get("cached_input_tokens"))
        prompt = _int(call.get("input_tokens")) or (unc + cached)
        out = _int(call.get("output_tokens"))
        reason = _int(call.get("reasoning_output_tokens"))
        write = _int(call.get("cache_write_input_tokens"))
        if prev_prompt and cached + _MISS_SLACK < prev_prompt:
            miss = min(unc, prev_prompt - cached)
        else:
            miss = 0
        bill = price_usage(
            call.get("model"),
            uncached=unc,
            cached=cached,
            output=out,
            cache_write=write,
            prompt=prompt,
        )
        notes.append({
            "miss": miss,
            "new": max(0, unc - miss),
            "bill": bill,
            "reason": reason,
            "out_rate": (bill["out"] / out) if out else 0.0,
            "in_rate": (bill["in"] / unc) if unc else 0.0,
            "detail_tok": {},
            "harness_in": [],
            "harness_rate": 0.0,
            "caused_tok": 0,
            "user_new": 0,
            "harness_new": 0,
        })
        prev_prompt = prompt
    for call, note in zip(calls, notes):
        detail = call.get("detail") if isinstance(call.get("detail"), list) else []
        out = _int(call.get("output_tokens"))
        rest = max(0, out - note["reason"])
        idxs = [
            i for i, ev in enumerate(detail)
            if isinstance(ev, dict) and ev.get("kind") in ("tool_request", "message")
        ]
        weights = [_ev_chars(detail[i]) for i in idxs]
        parts = _spread(rest, weights)
        note["detail_tok"] = {i: n for i, n in zip(idxs, parts)}
    order: list[str] = []
    groups: dict[str, list[int]] = {}
    for i, call in enumerate(calls):
        key = str(call.get("turn_id") or "") or f"call-{i}"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(i)
    for key in order:
        idxs = groups[key]
        for pos, i in enumerate(idxs):
            if pos == 0:
                notes[i]["user_new"] = notes[i]["new"]
                continue
            notes[i]["harness_new"] = notes[i]["new"]
            prev = idxs[pos - 1]
            # The next prompt pays this. The call that ran the tools owns the In.
            # Paid uncached of the next prompt. Kept for the round bill.
            # The tree does not spend it: Out and tool results are separate.
            notes[prev]["harness_rate"] = notes[i]["in_rate"]
            out_prev = _int(calls[prev].get("output_tokens"))
            prompt_prev = _int(calls[prev].get("input_tokens")) or (
                _int(calls[prev].get("uncached_input_tokens"))
                + _int(calls[prev].get("cached_input_tokens"))
            )
            prompt_next = _int(calls[i].get("input_tokens")) or (
                _int(calls[i].get("uncached_input_tokens"))
                + _int(calls[i].get("cached_input_tokens"))
            )
            # Context this call added. LLM Out is this call's billed Out,
            # not a slice of that growth. Tools keep whatever is left.
            delta = prompt_next - prompt_prev
            reentry = out_prev
            tool_pool = max(0, delta - out_prev)
            harness = calls[prev].get("harness") if isinstance(calls[prev].get("harness"), list) else []
            weights = [_ev_chars(h) for h in harness]
            parts = _spread(tool_pool, weights)
            notes[prev]["llm_reentry"] = reentry
            notes[prev]["harness_in"] = parts
            shown = reentry + sum(parts)
            if not parts:
                shown = max(reentry, max(0, delta))
            notes[prev]["display_in"] = shown
    return notes


def _tool_summary(detail: list[Any]) -> str:
    counts: dict[str, int] = {}
    for ev in detail:
        if not isinstance(ev, dict) or ev.get("kind") != "tool_request":
            continue
        name = str(ev.get("name") or "tool")
        counts[name] = counts.get(name, 0) + 1
    return " · ".join(f"{name} x{n}" if n > 1 else name for name, n in counts.items())


def _step_children(
    call: dict[str, Any],
    note: dict[str, Any],
    *,
    reason: int,
    out: int,
    step_n: int,
) -> list[dict[str, Any]]:
    """Same phase tree the Grok round card already renders.

    Token totals stay on the call. Tool rows are names and short labels.
    """
    detail = call.get("detail") if isinstance(call.get("detail"), list) else []
    harness = call.get("harness") if isinstance(call.get("harness"), list) else []
    msg = max(0, out - reason)
    rate_out = float(note.get("out_rate") or 0)
    detail_tok = note.get("detail_tok") if isinstance(note.get("detail_tok"), dict) else {}
    if not detail and not harness:
        children = []
        if reason:
            children.append({
                "kind": "reasoning",
                "label": "reasoning",
                "tokens_out": reason,
                "cost_out_usd": reason * rate_out,
            })
        if msg or out:
            children.append({
                "kind": "message",
                "label": "message",
                "tokens_out": msg,
                "cost_out_usd": msg * rate_out,
            })
        return children

    llm: list[dict[str, Any]] = []
    stamped_reason = False
    for i, ev in enumerate(detail):
        if not isinstance(ev, dict):
            continue
        kind = ev.get("kind")
        if kind == "reasoning":
            tok = reason if not stamped_reason else 0
            stamped_reason = True
            llm.append({
                "kind": "reasoning",
                "label": "reasoning",
                "preview": ev.get("preview") or "",
                "tokens_out": tok,
                "cost_out_usd": tok * rate_out,
            })
        elif kind == "thought":
            llm.append({
                "kind": "thought",
                "preview": ev.get("preview") or "[summary]",
                "tokens_out": 0,
                "cost_out_usd": 0,
            })
        elif kind == "message":
            tok = int(detail_tok.get(i) or 0)
            llm.append({
                "kind": "message",
                "label": "message",
                "preview": ev.get("preview") or "",
                "tokens_out": tok,
                "cost_out_usd": tok * rate_out,
            })
        elif kind == "tool_request":
            tok = int(detail_tok.get(i) or 0)
            llm.append({
                "kind": "tool_request",
                "name": ev.get("name") or "tool",
                "command": ev.get("command") or "",
                "path": ev.get("path") or "",
                "title": ev.get("preview") or ev.get("command") or "",
                "call_id": ev.get("call_id") or "",
                "tokens_out": tok,
                "cost_out_usd": tok * rate_out,
            })
    if reason and not stamped_reason:
        llm.insert(0, {
            "kind": "reasoning",
            "label": "reasoning",
            "tokens_out": reason,
            "cost_out_usd": reason * rate_out,
        })
    # Tool requests already carry `rest`. A message line only keeps what
    # they did not take — never a second copy of the same output.
    assigned = sum(int(v) for v in detail_tok.values())
    left = msg - assigned
    if left > 0 and not any(n.get("kind") == "message" for n in llm):
        llm.append({
            "kind": "message",
            "label": "message",
            "tokens_out": left,
            "cost_out_usd": left * rate_out,
        })
    children = []
    if llm:
        children.append({
            "kind": "phase_llm",
            "tokens_out": out,
            "cost_out_usd": float(note.get("bill", {}).get("out") or 0),
            "children": llm,
        })
    harness_in = note.get("harness_in") if isinstance(note.get("harness_in"), list) else []
    rate_h = float(note.get("harness_rate") or 0)
    reentry = int(note.get("llm_reentry") or 0)
    tools = []
    for i, ev in enumerate(harness):
        if not isinstance(ev, dict):
            continue
        tok = int(harness_in[i]) if i < len(harness_in) else 0
        tools.append({
            "kind": "tool",
            "name": ev.get("name") or "tool",
            "command": ev.get("command") or ev.get("preview") or "",
            "path": ev.get("path") or "",
            "title": ev.get("preview") or "",
            "call_id": ev.get("call_id") or "",
            "tokens_in": tok,
            "cost_in_usd": tok * rate_h,
        })
    harness_rows: list[dict[str, Any]] = []
    if reentry > 0:
        reason_in = min(reason, reentry)
        harness_rows.append({
            "kind": "llm_to_in",
            "label": f"LLM Out [{step_n}]",
            "call_index": step_n,
            "tool_summary": _tool_summary(detail),
            "tokens_in": reentry,
            "context_delta": reentry,
            "tokens_out_source": out,
            "reentry_reasoning_tokf": reason_in,
            "cost_in_usd": reentry * rate_h,
            "estimate_note": (
                "LLM Out of this call re-enters the next prompt as In. "
                "The figure is this call's billed Out. Tool results keep "
                "the rest of the prompt growth, so they are not scaled down "
                "to absorb the Out."
            ),
        })
    harness_rows.extend(tools)
    if harness_rows:
        hin = sum(int(t.get("tokens_in") or 0) for t in harness_rows)
        children.append({
            "kind": "phase_harness",
            "tool_count": len(tools),
            "tokens_in": hin,
            "cost_in_usd": hin * rate_h,
            "llm_out_in_tokens": reentry,
            "children": harness_rows,
        })
    return children


def _round_from_calls(
    index: int,
    calls: list[dict[str, Any]],
    prev_end: Optional[float],
    notes: list[dict[str, Any]],
) -> dict[str, Any]:
    totals = _sum_calls(calls)
    first = calls[0]
    last = calls[-1]
    model = str(last.get("model") or first.get("model") or "")
    effort = str(last.get("effort") or first.get("effort") or "")
    start_ep = _epoch(first.get("t"))
    end_ep = _epoch(last.get("t"))
    idle_ms = None
    if prev_end is not None and start_ep is not None:
        idle_ms = max(0, int((start_ep - prev_end) * 1000))
    steps = []
    first_prompt = _int(first.get("input_tokens")) or (
        _int(first.get("uncached_input_tokens")) + _int(first.get("cached_input_tokens"))
    )
    # Prefix before this turn's new user text. Chart User Y = prefix + user.
    ctx_cursor = max(0, first_prompt - int(notes[0].get("user_new") or 0))
    usd_in = usd_cached = usd_out = 0.0
    miss_tok = user_tok = harness_tok = 0
    msg_out = tool_out = 0
    msg_usd = tool_usd = 0.0
    for n, call in enumerate(calls, start=1):
        note = notes[n - 1]
        bill = note["bill"]
        unc = _int(call.get("uncached_input_tokens"))
        cached = _int(call.get("cached_input_tokens"))
        out = _int(call.get("output_tokens"))
        reason = _int(call.get("reasoning_output_tokens"))
        prompt = _int(call.get("input_tokens")) or (unc + cached)
        ctx_a = ctx_cursor
        ctx_b = prompt
        ctx_cursor = prompt
        usd_in += bill["in"]
        usd_cached += bill["cached"]
        usd_out += bill["out"]
        miss_tok += note["miss"]
        user_tok += note["user_new"]
        harness_tok += note["harness_new"]
        children = _step_children(call, note, reason=reason, out=out, step_n=n)
        caused = int(note.get("display_in") or 0)
        caused_usd = caused * float(note.get("harness_rate") or 0)
        flat = []
        for ch in children:
            if not isinstance(ch, dict):
                continue
            if ch.get("kind") == "phase_llm":
                flat.extend(ch.get("children") or [])
            else:
                flat.append(ch)
        for sub in flat:
            if not isinstance(sub, dict):
                continue
            tok = _int(sub.get("tokens_out"))
            usd = float(sub.get("cost_out_usd") or 0)
            if sub.get("kind") == "message":
                msg_out += tok
                msg_usd += usd
            elif sub.get("kind") == "tool_request":
                tool_out += tok
                tool_usd += usd
        step = {
            "index": n,
            "completed": True,
            # Call In = this call's Out plus the tool share of the next prompt's
            # growth. The API uncached of this call belongs to the previous cause.
            "tokens_in": caused,
            "uncached_input_tokens": caused,
            "tokens_cached": cached,
            "cached_read_tokens": cached,
            "tokens_out": out,
            "cost_in_usd": caused_usd,
            "cost_cached_usd": bill["cached"],
            "cost_out_usd": bill["out"],
            "context_start": ctx_a,
            "context_end": ctx_b,
            "context_plot_tokens": prompt,
            "cache_miss_in_tokens": note["miss"],
            "cache_miss_in_usd": note["miss"] * float(note.get("in_rate") or 0),
            "children": children,
        }
        if len(calls) >= 2 and n == len(calls):
            step["skip_context"] = True
        if call.get("compact_after"):
            nxt = calls[n] if n < len(calls) else None
            after = None
            if nxt is not None:
                after = _int(nxt.get("input_tokens")) or (
                    _int(nxt.get("uncached_input_tokens")) + _int(nxt.get("cached_input_tokens"))
                )
            removed = max(0, prompt - after) if after is not None else None
            step["compacts_after"] = [{
                "kind": "compaction",
                "auto": True,
                "placement": "mid_round",
                "tokens_before": prompt,
                "tokens_after": after,
                "tokens_removed": removed,
                "pre_read_cached_tokens": cached,
                "pre_read_uncached_tokens": note["miss"],
                "pre_read_cache_miss": note["miss"] > 0,
                "pre_read_cached_usd": bill["cached"],
                "pre_read_uncached_usd": note["miss"] * note["in_rate"],
                "cost_usd": 0,
            }]
        steps.append(step)
    # Cached shown on the row that owns the prefix: User has call 1,
    # call k has call k+1, and the last call has none (no next send).
    cache_toks = [_int(c.get("cached_input_tokens")) for c in calls]
    cache_usds = [float(n["bill"].get("cached") or 0) for n in notes]
    for i, step in enumerate(steps):
        nxt = i + 1
        show = cache_toks[nxt] if nxt < len(cache_toks) else 0
        show_usd = cache_usds[nxt] if nxt < len(cache_usds) else 0.0
        step["tokens_cached"] = show
        step["cached_read_tokens"] = show
        step["cost_cached_usd"] = show_usd
        for ch in step.get("children") or []:
            if isinstance(ch, dict) and ch.get("kind") == "phase_harness":
                ch["tokens_cached"] = show
                ch["cost_cached_usd"] = show_usd
                ch["display_cached_tokens"] = show
                ch["display_cached_usd"] = show_usd
    user_cache = cache_toks[0] if cache_toks else 0
    user_cache_usd = cache_usds[0] if cache_usds else 0.0
    reentry_tok = sum(int(n.get("llm_reentry") or 0) for n in notes)
    reentry_usd = sum(
        int(n.get("llm_reentry") or 0) * float(n.get("harness_rate") or 0) for n in notes
    )
    tool_tok = sum(sum(int(x) for x in (n.get("harness_in") or [])) for n in notes)
    tool_usd = sum(
        sum(int(x) for x in (n.get("harness_in") or [])) * float(n.get("harness_rate") or 0)
        for n in notes
    )
    miss_usd = sum(n["miss"] * n["in_rate"] for n in notes)
    user_usd = sum(n["user_new"] * n["in_rate"] for n in notes)
    harness_usd = sum(n["harness_new"] * n["in_rate"] for n in notes)
    reason_usd = sum(n["reason"] * n["out_rate"] for n in notes)
    breakdown = {
        "tree_in_tokens": totals["uncached"],
        "tree_in_usd": usd_in,
        "cached_tokens": totals["cached"],
        "cached_usd": usd_cached,
        "output_tokens": totals["output"],
        "output_usd": usd_out,
        "user_in_tokens": user_tok,
        "user_in_usd": user_usd,
        "harness_in_tokens": harness_tok,
        "harness_in_usd": harness_usd,
        "llm_out_to_harness_in_tokens": reentry_tok,
        "llm_out_to_harness_in_usd": reentry_usd,
        "harness_tools_in_tokens": tool_tok,
        "harness_tools_in_usd": tool_usd,
        "cache_miss_in_tokens": miss_tok,
        "cache_miss_in_usd": miss_usd,
        "llm_reasoning_tokens": totals["reasoning"],
        "llm_reasoning_usd": reason_usd,
        "llm_out_to_harness_tokens": tool_out,
        "llm_out_to_harness_usd": tool_usd,
        "llm_out_to_user_tokens": msg_out,
        "llm_out_to_user_usd": msg_usd,
        "total_usd": usd_in + usd_cached + usd_out,
    }
    ctx_start = max(0, first_prompt - user_tok)
    ctx_end = _int(last.get("input_tokens")) or (totals["uncached"] + totals["cached"])
    return {
        "index": index,
        "completed": True,
        "model_id": model,
        "model_family": model,
        "effort": effort,
        "breakdown": breakdown,
        "tree_in_tokens": totals["uncached"],
        "cached_read_tokens": totals["cached"],
        "output_tokens": totals["output"],
        "estimate_usd": usd_in + usd_cached + usd_out,
        "cost_cached_usd": usd_cached,
        "cost_out_usd": usd_out,
        "context_start": ctx_start,
        "context_end": ctx_end,
        "idle_gap_ms": idle_ms,
        "user_prompt": {
            "kind": "user_prompt",
            "tokens_in": user_tok,
            "uncached_est": user_tok,
            "prompt_tokens_in": user_tok,
            "tokens_cached": user_cache,
            "cost_cached_usd": user_cache_usd,
            "cost_in_usd": user_usd,
            "prompt_cost_in_usd": user_usd,
            "preview": next((str(c.get("user_preview")) for c in calls if c.get("user_preview")), ""),
        },
        "model_steps": steps,
        "_epoch": end_ep or start_ep,
    }


def _turn_from_round(rnd: dict[str, Any]) -> dict[str, Any]:
    bd = rnd["breakdown"]
    return {
        "index": rnd["index"],
        "turn_index": rnd["index"],
        "official_usd": 0,
        "estimate_usd": bd.get("total_usd") or 0,
        "uncached_input_tokens": bd["tree_in_tokens"],
        "cached_read_tokens": bd["cached_tokens"],
        "output_tokens": bd["output_tokens"],
        "input_tokens": bd["tree_in_tokens"] + bd["cached_tokens"],
        "tokens_in": bd["tree_in_tokens"],
        "tokens_cached": bd["cached_tokens"],
        "tokens_out": bd["output_tokens"],
        "tokens_reason": bd["llm_reasoning_tokens"],
        "cost_in_usd": bd.get("tree_in_usd") or 0,
        "cost_cached_usd": bd.get("cached_usd") or 0,
        "cost_out_usd": bd.get("output_usd") or 0,
        "context_tokens_for_tier": rnd.get("context_end") or 0,
        "peak_context_tokens": rnd.get("context_end") or 0,
        "epoch": rnd.get("_epoch"),
    }


def _view(thread: dict[str, Any]) -> dict[str, Any]:
    calls = _calls(thread)
    notes = _annotate(calls)
    rounds_all = []
    prev = None
    order: list[str] = []
    groups: dict[str, list[int]] = {}
    for i, call in enumerate(calls):
        key = str(call.get("turn_id") or "") or f"call-{i}"
        if key not in groups:
            groups[key] = []
            order.append(key)
        groups[key].append(i)
    for n, key in enumerate(order, start=1):
        idxs = groups[key]
        rnd = _round_from_calls(
            n,
            [calls[i] for i in idxs],
            prev,
            [notes[i] for i in idxs],
        )
        prev = rnd.get("_epoch")
        rounds_all.append(rnd)
    turns = [_turn_from_round(r) for r in rounds_all]
    series = []
    for call in calls:
        ep = _epoch(call.get("t"))
        prompt = _int(call.get("input_tokens")) or (
            _int(call.get("uncached_input_tokens")) + _int(call.get("cached_input_tokens"))
        )
        if ep is None:
            continue
        series.append({"t": int(ep * 1000), "v": prompt})
    last_prompt = series[-1]["v"] if series else 0
    model = str(thread.get("model") or "")
    window = _int(thread.get("context_window")) or 0
    wire_rounds = rounds_all[-_WIRE_ROUNDS:]
    for rnd in wire_rounds:
        rnd.pop("_epoch", None)
    return {
        "rounds": wire_rounds,
        "turns": turns,
        "series": series,
        "live": {
            "context_tokens": last_prompt or None,
            "context_tokens_ui": last_prompt or None,
            "model": model,
            "phase": "idle",
        },
        "window": window,
        "model": model,
        "n_calls": len(calls),
    }


def _session_row(
    thread: dict[str, Any],
    *,
    active: bool,
    now: float,
) -> dict[str, Any]:
    ep = _epoch(thread.get("updated")) or _epoch(thread.get("started"))
    title = str(thread.get("title") or thread.get("id"))
    return {
        "session_id": str(thread["id"]),
        "label": title,
        "title": title,
        "active": active,
        "mtime": ep or 0,
        "last_active_at": thread.get("updated") or thread.get("started"),
        "last_active_epoch": ep,
        "age_seconds": round(max(0.0, now - ep), 1) if ep else None,
        "age_label": _age(ep, now),
        "updates_bytes": 0,
        "path": thread.get("cwd") or "",
        "cwd": thread.get("cwd") or "",
        "session_kind": "main",
        "agent_name": None,
    }


def sessions_for_ui(
    snapshot: dict[str, Any],
    *,
    focus: Optional[str] = None,
    limit: int = 30,
    now: Optional[float] = None,
) -> list[dict[str, Any]]:
    now_s = datetime.now(timezone.utc).timestamp() if now is None else now
    threads = _threads(snapshot)
    kids = _children(threads)
    roots = _roots(threads, kids)
    active_id = str(roots[0]["id"]) if roots else ""
    rows = [
        _session_row(t, active=(str(t["id"]) == active_id), now=now_s)
        for t in roots[: max(0, limit)]
    ]
    if focus and not any(r["session_id"] == focus for r in rows):
        hit = _by_id(threads).get(focus)
        if hit is not None:
            rows.insert(0, _session_row(hit, active=False, now=now_s))
    return rows


def _sub_snapshot(thread: dict[str, Any], n: int) -> dict[str, Any]:
    view = _view(thread)
    title = str(thread.get("title") or thread.get("source") or thread["id"])
    return {
        "session_id": str(thread["id"]),
        "root_session_id": str(thread.get("parent_id") or thread["id"]),
        "session_kind": "subagent",
        "n": n,
        "resume_index": 0,
        "label": f"Sub Agent {n}",
        "title": title,
        "agent_name": thread.get("source") or None,
        "rounds": view["rounds"],
        "turns": view["turns"],
        "live": view["live"],
        "official_usd": 0,
        "estimate_usd": sum(float(t.get("estimate_usd") or 0) for t in view["turns"]),
    }


def state_for(
    snapshot: dict[str, Any],
    thread_id: Optional[str],
    *,
    pinned: Optional[str],
    now: Optional[float] = None,
) -> dict[str, Any]:
    threads = _threads(snapshot)
    index = _by_id(threads)
    kids = _children(threads)
    roots = _roots(threads, kids)
    follow = pinned is None
    chosen = thread_id or (str(roots[0]["id"]) if roots else "")
    thread = index.get(chosen)
    if thread is None:
        return {
            "watching": False,
            "error": "no Codex thread" if not threads else f"unknown session {chosen}",
            "session_id": None,
            "sessions": [],
            "rounds": [],
            "turns": [],
            "sub_sessions": [],
            "source": "codex",
        }
    view = _view(thread)
    subs = [
        _sub_snapshot(child, i)
        for i, child in enumerate(kids.get(str(thread["id"]), []), start=1)
    ]
    child_calls = sum(len(s["turns"]) for s in subs)
    model = view["model"]
    models = []
    for rnd in view["rounds"]:
        mid = rnd.get("model_id")
        if mid and mid not in models:
            models.append(mid)
    window = view["window"] or None
    parent_est = sum(float(t.get("estimate_usd") or 0) for t in view["turns"])
    child_est = sum(
        float(t.get("estimate_usd") or 0)
        for sub in subs
        for t in (sub.get("turns") or [])
    )
    priced = [priced_model(m) for m in models]
    priced = [m for m in priced if m]
    return {
        "watching": True,
        "error": None,
        "session_id": str(thread["id"]),
        "session_kind": "subagent" if not thread.get("is_root", True) and thread.get("parent_id") else "main",
        "pinned_session_id": None if follow else str(thread["id"]),
        "follow_active": follow,
        "sessions": sessions_for_ui(snapshot, focus=str(thread["id"]), now=now, limit=200),
        "source": thread.get("cwd") or "codex",
        "live": view["live"],
        "signals": {
            "contextTokensUsed": view["live"].get("context_tokens"),
            "contextWindowTokens": window,
            "turnCount": len(view["turns"]),
            "primaryModelId": model or None,
            "modelsUsed": models,
        },
        "turns": view["turns"],
        "rounds": view["rounds"],
        "sub_sessions": subs,
        "context_series": view["series"],
        "feed": [],
        "totals": {
            "official_usd": 0,
            "estimate_usd": parent_est + child_est,
            "official_ticks": 0,
            "turns": len(view["turns"]),
            "parent_only_usd": 0,
            "children_usd": 0,
            "parent_estimate_usd": parent_est,
            "children_estimate_usd": child_est,
            "combined_usd": parent_est + child_est,
            "subagent_count": len(subs),
            "gen_tokens_per_sec": None,
            "child_turns": child_calls,
        },
        "pricing": {
            "model": (priced[0] if priced else model) or None,
            "model_label": (priced[0] if priced else model) or "Codex",
            "model_ids": models,
            "assumed": False,
            "mixed": len(set(priced)) > 1,
            "note": (
                "Estimate uses OpenAI standard API list prices "
                "(developers.openai.com/api/docs/pricing). "
                "Context above 272k uses the published long-context rates. "
                "codex-auto-review is priced as gpt-5.6-luna. "
                "A Codex subscription may not bill these calls."
            ),
        },
        "context_now_estimate_note": (
            "Context is the last Codex prompt size (input tokens, cached included)."
        ),
        "revision": len(view["turns"]),
        "ledger_source": "codex",
    }


def _acc() -> dict[str, float]:
    return {
        "tokens_in": 0,
        "tokens_cached": 0,
        "tokens_out": 0,
        "tokens_reason": 0,
        "cost_in_usd": 0.0,
        "cost_cached_usd": 0.0,
        "cost_out_usd": 0.0,
        "cost_reason_usd": 0.0,
        "official_usd": 0.0,
        "estimate_usd": 0.0,
        "turns": 0,
    }


def _add_call(acc: dict[str, float], call: dict[str, Any]) -> None:
    unc = _int(call.get("uncached_input_tokens"))
    cached = _int(call.get("cached_input_tokens"))
    out = _int(call.get("output_tokens"))
    reason = _int(call.get("reasoning_output_tokens"))
    acc["tokens_in"] += unc
    acc["tokens_cached"] += cached
    acc["tokens_out"] += out
    acc["tokens_reason"] += reason
    acc["turns"] += 1
    prompt = _int(call.get("input_tokens")) or (unc + cached)
    bill = price_usage(
        call.get("model"),
        uncached=unc,
        cached=cached,
        output=out,
        cache_write=_int(call.get("cache_write_input_tokens")),
        prompt=prompt,
    )
    acc["cost_in_usd"] += bill["in"]
    acc["cost_cached_usd"] += bill["cached"]
    acc["cost_out_usd"] += bill["out"]
    acc["estimate_usd"] += bill["total"]
    acc["cost_reason_usd"] += (bill["out"] * reason / out) if out else 0.0


def _public_acc(acc: dict[str, float]) -> dict[str, Any]:
    tin = int(acc["tokens_in"])
    tc = int(acc["tokens_cached"])
    tout = int(acc["tokens_out"])
    tr = int(acc["tokens_reason"])
    return {
        "tokens_in": tin,
        "tokens_cached": tc,
        "tokens_out": tout,
        "tokens_reason": tr,
        "tokens_all": tin + tc + tout,
        "cost_in_usd": acc["cost_in_usd"],
        "cost_cached_usd": acc["cost_cached_usd"],
        "cost_out_usd": acc["cost_out_usd"],
        "cost_reason_usd": acc["cost_reason_usd"],
        "official_usd": 0.0,
        "estimate_usd": acc["estimate_usd"],
        "turns": int(acc["turns"]),
    }


def _cat(key: str, label: str, tok: int, usd: float = 0.0) -> dict[str, Any]:
    return {"key": key, "k": key, "label": label, "usd": usd, "tok": tok}


def _cats_from_acc(acc: dict[str, float], *, tools: bool) -> list[dict[str, Any]]:
    msg = max(0, int(acc["tokens_out"]) - int(acc["tokens_reason"]))
    reason_usd = float(acc["cost_reason_usd"])
    msg_usd = max(0.0, float(acc["cost_out_usd"]) - reason_usd)
    if tools:
        rows = [
            _cat("prompt", "prompt", int(acc["tokens_in"]), float(acc["cost_in_usd"])),
            _cat("reasoning", "reasoning", int(acc["tokens_reason"]), reason_usd),
            _cat("message", "message", msg, msg_usd),
        ]
    else:
        rows = [
            _cat("user", "user", int(acc["tokens_in"]), float(acc["cost_in_usd"])),
            _cat("cached", "cached", int(acc["tokens_cached"]), float(acc["cost_cached_usd"])),
            _cat("reasoning", "reasoning", int(acc["tokens_reason"]), reason_usd),
            _cat("message", "message", msg, msg_usd),
        ]
    return [r for r in rows if r["tok"] > 0]


def _in_window(ep: Optional[float], start: float, end: float) -> bool:
    return ep is not None and start <= ep < end


def build_period(
    snapshot: dict[str, Any],
    *,
    period: str = "daily",
    offset: int = 0,
    grain: str = "hour",
    now: Optional[datetime] = None,
) -> dict[str, Any]:
    period = (period or "daily").strip().lower()
    if period not in ("daily", "weekly", "monthly"):
        period = "daily"
    grain = normalize_grain(period, grain)
    try:
        offset = int(offset)
    except (TypeError, ValueError):
        offset = 0
    start, end, label = period_window(period, offset, now=now)
    start_e, end_e = start.timestamp(), end.timestamp()
    specs = _bucket_specs(period, start, end, grain)
    session_grain = grain == "session"
    buckets = [{**s, **_acc(), "_parts": _acc(), "_tools": _acc()} for s in specs]

    threads = _threads(snapshot)
    kids = _children(threads)
    child_ids = {str(t["id"]) for group in kids.values() for t in group}
    roots = [t for t in threads if str(t["id"]) not in child_ids]
    roots.sort(key=lambda t: _epoch(t.get("started")) or _epoch(t.get("updated")) or 0)

    def calls_in(thread: dict[str, Any]) -> list[dict[str, Any]]:
        out = []
        for call in _calls(thread):
            if _in_window(_epoch(call.get("t")), start_e, end_e):
                out.append(call)
        return out

    sessions: list[dict[str, Any]] = []
    totals = _acc()
    n_parent = 0
    for root in roots:
        root_calls = calls_in(root)
        child_rows = []
        for child in kids.get(str(root["id"]), []):
            cc = calls_in(child)
            if cc:
                child_rows.append((child, cc))
        if not root_calls and not child_rows:
            continue
        n_parent += 1
        sessions.append(_period_session(root, root_calls, n=n_parent, depth=0, child_n=None))
        for acc_calls in (root_calls,):
            _fold_calls(acc_calls, totals, buckets, session_grain)
        for i, (child, cc) in enumerate(child_rows, start=1):
            sessions.append(
                _period_session(
                    child,
                    cc,
                    n=n_parent,
                    depth=1,
                    child_n=i,
                    parent_id=str(root["id"]),
                )
            )
            _fold_calls(cc, totals, buckets, session_grain)

    if session_grain:
        buckets_out = []
        for s in sessions:
            label_x = (
                f"{s['n']}.{s['child_n']}"
                if s.get("depth") and s.get("child_n") is not None
                else str(s.get("n") or "")
            )
            buckets_out.append({
                "key": f"sess:{s['session_id']}",
                "label": label_x,
                "start_epoch": s.get("first_epoch"),
                "end_epoch": s.get("last_epoch"),
                "session_id": s["session_id"],
                "session_label": s.get("label") or s.get("title"),
                **{k: v for k, v in s.items() if k in _acc() or k in ("tokens_all",)},
                "parts": s.get("parts") or [],
                "tools": s.get("tools") or [],
            })
    else:
        buckets_out = []
        for b in buckets:
            parts_acc = b.pop("_parts")
            tools_acc = b.pop("_tools")
            buckets_out.append({
                "key": b["key"],
                "label": b["label"],
                "start_epoch": b.get("start_epoch"),
                "end_epoch": b.get("end_epoch"),
                **_public_acc(b),
                "parts": _cats_from_acc(parts_acc, tools=False),
                "tools": _cats_from_acc(tools_acc, tools=True),
            })

    return {
        "period": period,
        "offset": offset,
        "grain": grain,
        "label": label,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "cats_ready": True,
        "rate_full": False,
        "totals": {
            **_public_acc(totals),
            "parts": _cats_from_acc(totals, tools=False),
            "tools": _cats_from_acc(totals, tools=True),
        },
        "buckets": buckets_out,
        "sessions": sessions,
        "session_count": len(sessions),
        "tps_rounds": [],
        "tps_sessions": [],
        "ledger_source": "codex",
    }


def _fold_calls(
    calls: list[dict[str, Any]],
    totals: dict[str, float],
    buckets: list[dict[str, Any]],
    session_grain: bool,
) -> None:
    for call in calls:
        _add_call(totals, call)
        if session_grain:
            continue
        ep = _epoch(call.get("t"))
        if ep is None:
            continue
        for bucket in buckets:
            if bucket["start_epoch"] <= ep < bucket["end_epoch"]:
                _add_call(bucket, call)
                _add_call(bucket["_parts"], call)
                _add_call(bucket["_tools"], call)
                break


def _period_session(
    thread: dict[str, Any],
    calls: list[dict[str, Any]],
    *,
    n: int,
    depth: int,
    child_n: Optional[int],
    parent_id: Optional[str] = None,
) -> dict[str, Any]:
    acc = _acc()
    first = None
    last = None
    for call in calls:
        _add_call(acc, call)
        ep = _epoch(call.get("t"))
        if ep is None:
            continue
        if first is None or ep < first:
            first = ep
        if last is None or ep > last:
            last = ep
    title = str(thread.get("title") or thread["id"])
    kind = "subagent" if depth else "main"
    label = f"Sub Agent {child_n}" if depth and child_n else f"Session {n}"
    return {
        "session_id": str(thread["id"]),
        "title": title,
        "label": label,
        "session_kind": kind,
        "agent_name": thread.get("source") or None,
        "parent_id": parent_id,
        "n": n,
        "child_n": child_n,
        "depth": depth,
        "model_family": thread.get("model") or None,
        "first_epoch": first,
        "last_epoch": last,
        "spans": (
            [{"start": first, "end": last, "kind": "work"}]
            if first is not None and last is not None and last > first
            else []
        ),
        "gen_tokens_per_sec": None,
        **_public_acc(acc),
        "parts": _cats_from_acc(acc, tools=False),
        "tools": _cats_from_acc(acc, tools=True),
    }
