"""Period aggregates (daily / weekly / monthly) from official turn usage.

Parent ``turn_completed`` includes sub-agent bills — those are peeled so I/O
matches Parts/Tools (hierarchy is already parent-only). Subagent dirs are
listed on Session grain but excluded from period totals / time buckets.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

from token_telemetry.pricing.rates import normalize_model_id
from token_telemetry.session.discover import (  # noqa: F401
    _read_session_summary,
    list_session_dirs,
    pick_session_title,
)
from token_telemetry.session.kind import is_subagent_kind
from token_telemetry.session.usage import root_subagent_id, summary_parent_session_id
from token_telemetry.session.period_attr import cached_attr_events, is_attr_warm
from token_telemetry.session.windows import (
    _bucket_specs,
    normalize_grain,
    period_window,
    week_monday,
)
from token_telemetry.session.scan import (
    _add_priced,
    _empty_acc,
    _file_cache,
    _lock,
    _priced_from_io_segs,
    _round_acc,
    _scan_files,
    clear_file_mem,
)
from token_telemetry.session.peel_period import (  # noqa: F401
    _acc_from_session_row,
    _add_cat_list,
    _apply_acc_to_session_row,
    _cats_out,
    _merge_resume_chain,
    _order_sessions,
    _parent_covers_kids,
    _peel_parent_rows,
    _place,
    _scale_turn_to_peeled,
    _sub_acc,
)

RATE_CTX_CAP = 190_000


def build_aggregate(
    period: str = "daily",
    offset: int = 0,
    grain: str = "day",
    now: Optional[datetime] = None,
    stack: str = "io",
    rate: bool = False,
    on_progress: Optional[Callable[[int, int], None]] = None,
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
    session_grain = grain == "session"
    specs = _bucket_specs(period, start, end, grain)
    buckets = (
        []
        if session_grain
        else [{**s, **_empty_acc(), "_parts": {}, "_tools": {}} for s in specs]
    )
    totals = _empty_acc()
    tot_parts: dict[str, dict[str, Any]] = {}
    tot_tools: dict[str, dict[str, Any]] = {}
    sessions: list[dict[str, Any]] = []
    sess_parts: dict[str, dict[str, dict[str, Any]]] = {}
    sess_tools: dict[str, dict[str, dict[str, Any]]] = {}
    # Recap/compact I/O sides (not in turn_completed) — applied after peel.
    sess_side_io: dict[str, list[dict[str, Any]]] = {}
    # In-window turns kept until after parent peel (I/O includes sub bills).
    sess_turns: dict[str, list[dict[str, Any]]] = {}
    raw_sess_acc: dict[str, dict[str, float]] = {}
    want_rate = bool(rate)
    # `stack` is kept for API compat; Parts/Tools cats are always filled when
    # attr runs so the client can switch I/O↔Parts↔Tools without a refetch.
    # Attr = HierarchyBuilder (cold) or calc-cache hit. I/O needs recap/compact
    # sides. Subs are attr'd only when rate=1 or session grain — lighter D/W/M entry.

    with _lock:
        files = _scan_files()

    # First parent (by session start) that authoritatively spawned/waited a
    # child wins — never overwrite (later chats/skill dumps re-mention UUIDs).
    child_to_parent: dict[str, str] = {}
    mains_for_link = [
        r for r in files if not is_subagent_kind(r.get("kind"))
    ]
    mains_for_link.sort(
        key=lambda r: float(r.get("first_all") or r.get("last_all") or 0)
    )
    for row in mains_for_link:
        pid = str(row.get("session_id") or "").lower()
        if not pid:
            continue
        for cid in row.get("child_ids") or []:
            c = str(cid).lower()
            if c and c not in child_to_parent:
                child_to_parent[c] = pid

    def _turns_in_window(row: dict[str, Any]) -> int:
        n = 0
        for t in row.get("turns") or []:
            ep = t.get("epoch")
            if ep is None or ep < start_e or ep >= end_e:
                continue
            n += 1
        return n

    progress_total = sum(1 for row in files if _turns_in_window(row) > 0)
    progress_done = 0

    def _session_dir(row: dict[str, Any]) -> Optional[Path]:
        d = Path(row.get("path") or "")
        if not d.is_dir():
            d = Path(str(row.get("updates") or ""))
            if d.is_file():
                d = d.parent
        return d if d.is_dir() else None

    # How many sessions will rebuild attr (cache miss → HierarchyBuilder + save).
    cold = 0
    for row in files:
        if _turns_in_window(row) <= 0:
            continue
        billed = not is_subagent_kind(row.get("kind"))
        if not (billed or want_rate or session_grain):
            continue
        d = _session_dir(row)
        if d is not None and not is_attr_warm(d):
            cold += 1

    def _emit_progress(done: int, total: int, *, cold_n: Optional[int] = None) -> None:
        if on_progress is None:
            return
        try:
            if cold_n is None:
                on_progress(done, total)
            else:
                on_progress(done, total, cold=cold_n)
        except TypeError:
            try:
                on_progress(done, total)
            except Exception:
                pass
        except Exception:
            pass

    _emit_progress(0, progress_total, cold_n=cold)

    for row in files:
        kind = row.get("kind")
        billed = not is_subagent_kind(kind)
        sess_acc = _empty_acc()
        last_ep = None
        first_ep = None
        sid = str(row["session_id"])
        sid_l = sid.lower()
        in_turns: list[dict[str, Any]] = []
        for t in row.get("turns") or []:
            ep = t.get("epoch")
            if ep is None or ep < start_e or ep >= end_e:
                continue
            _add_priced(sess_acc, t)
            in_turns.append(t)
            if last_ep is None or ep > last_ep:
                last_ep = ep
            if first_ep is None or ep < first_ep:
                first_ep = ep
        # Drop title-only / no-bill sessions before attr — previously every
        # main paid HierarchyBuilder/disk attr even when outside the window.
        if int(sess_acc.get("turns") or 0) <= 0:
            continue
        sess_round_tps: list[dict[str, Any]] = []
        # Mains always (I/O sides + cats); subs when tok/s or session grain
        # (session bars need per-row Parts/Tools without a second fetch).
        need_attr = billed or want_rate or session_grain
        if need_attr:
            try:
                d = _session_dir(row)
                if d is not None:
                    for ev in cached_attr_events(d):
                        ep = ev.get("epoch")
                        if ep is None or ep < start_e or ep >= end_e:
                            continue
                        # tok/s points (mains always when attr'd; subs when rate).
                        if ev.get("tps") is not None:
                            sess_round_tps.append({
                                "epoch": float(ep),
                                "v": float(ev["tps"]),
                                "round": ev.get("round"),
                                "gen_ms": ev.get("gen_ms"),
                                "gen_out_tokens": ev.get("gen_out_tokens"),
                            })
                        io_segs = ev.get("io") or []
                        if billed and io_segs:
                            priced_io = _priced_from_io_segs(io_segs)
                            if (
                                int(priced_io.get("tokens_in") or 0)
                                or int(priced_io.get("tokens_cached") or 0)
                                or int(priced_io.get("tokens_out") or 0)
                                or float(priced_io.get("estimate_usd") or 0)
                            ):
                                sess_side_io.setdefault(sid_l, []).append({
                                    "epoch": float(ep),
                                    "priced": priced_io,
                                })
                        # Always fold Parts+Tools once attr is paid (client stack switch).
                        parts = ev.get("parts") or []
                        tools = ev.get("tools") or []
                        if session_grain:
                            _add_cat_list(
                                sess_parts.setdefault(sid_l, {}), parts
                            )
                            _add_cat_list(
                                sess_tools.setdefault(sid_l, {}), tools
                            )
                        else:
                            if not billed:
                                continue
                            idx = _place(float(ep), specs)
                            if idx is None:
                                continue
                            _add_cat_list(buckets[idx]["_parts"], parts)
                            _add_cat_list(buckets[idx]["_tools"], tools)
                            _add_cat_list(tot_parts, parts)
                            _add_cat_list(tot_tools, tools)
            except Exception:
                pass
        life0 = row.get("first_all")
        life1 = row.get("last_all")
        if life0 is None:
            life0 = first_ep
        if life1 is None:
            life1 = last_ep
        clip0 = max(float(life0), start_e) if life0 is not None else start_e
        clip1 = min(float(life1), end_e) if life1 is not None else end_e
        if clip1 < clip0:
            clip1 = clip0
        spans_out: list[dict[str, Any]] = []
        for sp in row.get("spans") or []:
            a = max(float(sp.get("start") or 0), start_e)
            b = min(float(sp.get("end") or 0), end_e)
            if b > a:
                spans_out.append({"start": a, "end": b, "kind": sp.get("kind") or "work"})
        if not spans_out and clip1 > clip0:
            spans_out.append({"start": clip0, "end": clip1, "kind": "work"})
        title = row.get("title") or sid[:8]
        # Mains keep summary parent only (never scrape — UUID chatter is noise).
        parent_id = row.get("parent_id")
        root_id = None
        if is_subagent_kind(kind):
            raw_path = str(row.get("path") or "")
            root_id = root_subagent_id(
                sid.lower(),
                parent_dir=Path(raw_path) if raw_path else None,
            )
            orch = child_to_parent.get(root_id) or child_to_parent.get(sid_l)
            if orch:
                parent_id = orch
            elif not parent_id:
                parent_id = child_to_parent.get(sid_l)
        role = (row.get("agent_name") or "").strip()
        if (
            is_subagent_kind(kind)
            and role
            and role.lower() not in ("general-purpose", "general purpose")
            and role not in title
        ):
            title = f"{role} · {title}"
        sess_tps = None
        if sess_round_tps:
            sess_tps = round(
                sum(x["v"] for x in sess_round_tps) / len(sess_round_tps),
                3,
            )
        sess_turns[sid_l] = in_turns
        raw_sess_acc[sid_l] = dict(sess_acc)
        model_family = row.get("model_family")
        if not model_family:
            model_family = normalize_model_id(
                (_read_session_summary(Path(row.get("path") or "")) or {}).get(
                    "current_model_id"
                )
            )
        sessions.append(
            {
                "session_id": sid,
                "title": title,
                "session_kind": kind or "main",
                "agent_name": row.get("agent_name"),
                "parent_id": parent_id,
                "root_session_id": root_id,
                "model_family": model_family,
                "first_epoch": clip0,
                "last_epoch": clip1,
                "spans": spans_out,
                "gen_tokens_per_sec": sess_tps,
                "_tps_rounds": sess_round_tps,
                **_round_acc(sess_acc),
            }
        )
        progress_done += 1
        _emit_progress(progress_done, progress_total, cold_n=cold)

    sessions = _order_sessions(sessions)
    # Parent API bill includes subs — peel latest linked kids so I/O matches
    # Parts/Tools (hierarchy already parent-only) and session rows.
    _peel_parent_rows(sessions)

    # Rebuild period totals / time buckets from peeled parent turns only.
    totals = _empty_acc()
    if not session_grain:
        for b in buckets:
            for k in _empty_acc():
                b[k] = 0 if k == "turns" or k.startswith("tokens_") else 0.0
    by_sid = {str(s.get("session_id") or "").lower(): s for s in sessions}
    fold_parents = {
        str(s.get("session_id") or "").lower()
        for s in sessions
        if s.get("_fold_kids")
    }
    for sid_l, turns in sess_turns.items():
        s = by_sid.get(sid_l)
        if s is None:
            continue
        is_sub = is_subagent_kind(s.get("session_kind")) or int(s.get("depth") or 0) > 0
        if is_sub:
            pid = str(s.get("parent_id") or "").lower()
            if pid not in fold_parents:
                continue
            for t in turns:
                _add_priced(totals, t)
                if not session_grain:
                    ep = t.get("epoch")
                    if ep is None:
                        continue
                    idx = _place(float(ep), specs)
                    if idx is not None:
                        _add_priced(buckets[idx], t)
            continue
        raw = raw_sess_acc.get(sid_l) or _empty_acc()
        peeled = _acc_from_session_row(s)
        for t in turns:
            pt = _scale_turn_to_peeled(t, raw, peeled)
            _add_priced(totals, pt)
            if not session_grain:
                ep = t.get("epoch")
                if ep is None:
                    continue
                idx = _place(float(ep), specs)
                if idx is not None:
                    _add_priced(buckets[idx], pt)

    # Stamp API-only tok (turn_completed, peeled) before recap/compact sides
    # inflate tokens_* — used by I/O $/M Official reverse-engineer.
    for s in sessions:
        if is_subagent_kind(s.get("session_kind")) or int(s.get("depth") or 0) > 0:
            continue
        s["api_tokens_in"] = int(s.get("tokens_in") or 0)
        s["api_tokens_cached"] = int(s.get("tokens_cached") or 0)
        s["api_tokens_out"] = int(s.get("tokens_out") or 0)

    # I/O $/M rate pool: Official + API tok from turns with ctx ≤190k only
    # (aligns with Session cost Official subline; avoids high-tier mix).
    for sid_l, turns in sess_turns.items():
        s = by_sid.get(sid_l)
        if s is None:
            continue
        if is_subagent_kind(s.get("session_kind")) or int(s.get("depth") or 0) > 0:
            continue
        raw = raw_sess_acc.get(sid_l) or _empty_acc()
        peeled = {
            "tokens_in": int(s.get("api_tokens_in") or 0),
            "tokens_cached": int(s.get("api_tokens_cached") or 0),
            "tokens_out": int(s.get("api_tokens_out") or 0),
            "tokens_reason": int(s.get("tokens_reason") or 0),
            "cost_in_usd": float(s.get("cost_in_usd") or 0),
            "cost_cached_usd": float(s.get("cost_cached_usd") or 0),
            "cost_out_usd": float(s.get("cost_out_usd") or 0),
            "cost_reason_usd": float(s.get("cost_reason_usd") or 0),
            "official_usd": float(s.get("official_usd") or 0),
            "estimate_usd": float(s.get("estimate_usd") or 0),
            "turns": int(s.get("turns") or 0),
        }
        rate_acc = _empty_acc()
        n_rate = 0
        for t in turns:
            pt = _scale_turn_to_peeled(t, raw, peeled)
            try:
                ctx = int(
                    pt.get("context_tokens_for_tier")
                    or t.get("context_tokens_for_tier")
                    or 0
                )
            except (TypeError, ValueError):
                ctx = 0
            if ctx > RATE_CTX_CAP:
                continue
            if not (float(pt.get("official_usd") or 0) > 0):
                continue
            if not (
                int(pt.get("tokens_in") or 0)
                or int(pt.get("tokens_cached") or 0)
                or int(pt.get("tokens_out") or 0)
            ):
                continue
            _add_priced(rate_acc, pt, count_turn=False)
            n_rate += 1
        if n_rate > 0:
            s["rate_official_usd"] = round(float(rate_acc.get("official_usd") or 0), 6)
            s["rate_tokens_in"] = int(rate_acc.get("tokens_in") or 0)
            s["rate_tokens_cached"] = int(rate_acc.get("tokens_cached") or 0)
            s["rate_tokens_out"] = int(rate_acc.get("tokens_out") or 0)
            s["rate_turns"] = n_rate
        else:
            # No low-ctx turns — fall back to full peeled API bill.
            s["rate_official_usd"] = float(s.get("official_usd") or 0)
            s["rate_tokens_in"] = int(s.get("api_tokens_in") or 0)
            s["rate_tokens_cached"] = int(s.get("api_tokens_cached") or 0)
            s["rate_tokens_out"] = int(s.get("api_tokens_out") or 0)
            s["rate_turns"] = 0

    # Recap/compact I/O after peel (keep turn scaling turn-only).
    for sid_l, sides in sess_side_io.items():
        s = by_sid.get(sid_l)
        if s is None:
            continue
        if is_subagent_kind(s.get("session_kind")) or int(s.get("depth") or 0) > 0:
            continue
        sac = _acc_from_session_row(s)
        for side in sides:
            priced = side.get("priced") or {}
            _add_priced(sac, priced, count_turn=False)
            _add_priced(totals, priced, count_turn=False)
            if not session_grain:
                ep = side.get("epoch")
                if ep is None:
                    continue
                idx = _place(float(ep), specs)
                if idx is not None:
                    _add_priced(buckets[idx], priced, count_turn=False)
        _apply_acc_to_session_row(s, sac)

    # Session-grain parent parts/tools → period totals (subs listed, not totaled).
    if session_grain:
        tot_parts = {}
        tot_tools = {}
        for s in sessions:
            if is_subagent_kind(s.get("session_kind")) or int(s.get("depth") or 0) > 0:
                continue
            sid_l = str(s.get("session_id") or "").lower()
            for cat in _cats_out(sess_parts.get(sid_l) or {}):
                _add_cat_list(tot_parts, [cat])
            for cat in _cats_out(sess_tools.get(sid_l) or {}):
                _add_cat_list(tot_tools, [cat])

    if session_grain:
        buckets = []
        for i, s in enumerate(sessions, start=1):
            sid_l = str(s.get("session_id") or "").lower()
            # X labels: main = N, sub = N.M (parent session · agent). Order unchanged.
            n_parent = s.get("n")
            child_n = s.get("child_n")
            is_sub = int(s.get("depth") or 0) > 0 or is_subagent_kind(
                s.get("session_kind")
            )
            if is_sub and n_parent is not None and child_n is not None:
                x_label = f"{n_parent}.{child_n}"
            elif n_parent is not None:
                x_label = str(n_parent)
            else:
                x_label = str(i)
            buckets.append(
                {
                    "key": f"sess:{sid_l or i}",
                    "label": x_label,
                    "start_epoch": s.get("first_epoch"),
                    "end_epoch": s.get("last_epoch"),
                    "session_id": s.get("session_id"),
                    "session_label": s.get("label") or s.get("title"),
                    "tokens_in": int(s.get("tokens_in") or 0),
                    "tokens_cached": int(s.get("tokens_cached") or 0),
                    "tokens_out": int(s.get("tokens_out") or 0),
                    "tokens_reason": int(s.get("tokens_reason") or 0),
                    "tokens_all": int(s.get("tokens_all") or 0),
                    "cost_in_usd": float(s.get("cost_in_usd") or 0),
                    "cost_cached_usd": float(s.get("cost_cached_usd") or 0),
                    "cost_out_usd": float(s.get("cost_out_usd") or 0),
                    "cost_reason_usd": float(s.get("cost_reason_usd") or 0),
                    "official_usd": float(s.get("official_usd") or 0),
                    "estimate_usd": float(s.get("estimate_usd") or 0),
                    "turns": int(s.get("turns") or 0),
                    "parts": _cats_out(sess_parts.get(sid_l) or {}),
                    "tools": _cats_out(sess_tools.get(sid_l) or {}),
                }
            )

    tot_rounded = _round_acc(totals)
    tot_parts_out = _cats_out(tot_parts)
    tot_tools_out = _cats_out(tot_tools)

    tps_rounds: list[dict[str, Any]] = []
    tps_sessions: list[dict[str, Any]] = []
    for s in sessions:
        rounds = s.pop("_tps_rounds", None) or []
        # X labels match Session grain: main = N, sub = N.M (no "Session"/"Round" words).
        n_parent = s.get("n")
        child_n = s.get("child_n")
        is_sub = int(s.get("depth") or 0) > 0 or is_subagent_kind(
            s.get("session_kind")
        )
        if is_sub and n_parent is not None and child_n is not None:
            x_sess = f"{n_parent}.{child_n}"
        elif n_parent is not None:
            x_sess = str(n_parent)
        else:
            x_sess = str(s.get("label") or s.get("title") or "?")
        for rp in rounds:
            rn = rp.get("round") or "?"
            tps_rounds.append({
                "epoch": rp["epoch"],
                "v": rp["v"],
                "round": rp.get("round"),
                "gen_ms": rp.get("gen_ms"),
                "gen_out_tokens": rp.get("gen_out_tokens"),
                "session_id": s["session_id"],
                "n": n_parent,
                "child_n": child_n,
                "depth": s.get("depth") or 0,
                "label": f"{x_sess} R{rn}",
            })
        if s.get("gen_tokens_per_sec") is not None:
            tps_sessions.append({
                "epoch": s.get("last_epoch") or s.get("first_epoch"),
                "v": s["gen_tokens_per_sec"],
                "session_id": s["session_id"],
                "n": n_parent,
                "child_n": child_n,
                "depth": s.get("depth") or 0,
                "label": x_sess,
            })
    for s in sessions:
        s.pop("_fold_kids", None)
    tps_rounds.sort(key=lambda x: (x.get("epoch") or 0, x.get("round") or 0))
    tps_sessions.sort(key=lambda x: (
        x.get("epoch") or 0,
        x.get("n") or 0,
        x.get("child_n") or 0,
    ))

    return {
        "period": period,
        "offset": offset,
        "grain": grain,
        "label": label,
        "start": start.isoformat(),
        "end": end.isoformat(),
        # cats always filled when mains were attr'd; rate_full ⇒ subs included in tps.
        "cats_ready": True,
        "rate_full": bool(want_rate),
        "totals": {**tot_rounded, "parts": tot_parts_out, "tools": tot_tools_out},
        "buckets": (
            buckets
            if session_grain
            else [
                {
                    "key": b["key"],
                    "label": b["label"],
                    "start_epoch": b.get("start_epoch"),
                    "end_epoch": b.get("end_epoch"),
                    **_round_acc(b),
                    "parts": _cats_out(b.get("_parts") or {}),
                    "tools": _cats_out(b.get("_tools") or {}),
                }
                for b in buckets
            ]
        ),
        "sessions": sessions,
        "session_count": len(sessions),
        "tps_rounds": tps_rounds,
        "tps_sessions": tps_sessions,
    }

