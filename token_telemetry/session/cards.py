"""Moved from session/subagents.py — same bodies."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Optional

from token_telemetry.pricing.rates import estimate_from_usage, ticks_to_usd
from token_telemetry.session.ids import (
    SUBAGENT_ID_RE,
    TASK_LINE_RE,
    UUID_RE,
    add_uuid as _add_uuid,
    child_ids_from_session_update,
    extract_ids_from_text,
    extract_task_ids,
)
from token_telemetry.session.kind import (
    SUBAGENT_KINDS,
    is_subagent_kind,
    session_kind_of,
)
from token_telemetry.util import as_int as _int, as_opt_int

from token_telemetry.session.usage import (
    child_ids_to_peel,
    collect_child_ids_from_round,
    collect_resume_alias_from_round,
    latest_in_resume_chain,
    load_session_official_turns,
    load_session_official_usage,
    peel_round_usage,
    price_child_usage,
    read_session_summary,
    root_subagent_id,
    sibling_session_dir,
    spawn_session_ids,
    tool_resume_from,
)

def _card_label(n: int, resume_index: int, *, multi: bool, is_sys: bool = False) -> str:
    if is_sys:
        return f"Sub Agent {n} Sys"
    k = int(resume_index or 0)
    if multi and k >= 1:
        return f"Sub Agent {n} R{k}"
    return f"Sub Agent {n}"

def _ordinal_n(
    hb: Any,
    uid: str,
    *,
    parent_dir: Optional[Path],
    alias: dict[str, str],
) -> tuple[str, int]:
    root = root_subagent_id(uid, parent_dir=parent_dir, alias=alias)
    if hb is None:
        return root, 1
    ordinal = getattr(hb, "_subagent_ordinal", None)
    if not isinstance(ordinal, dict):
        ordinal = {}
        hb._subagent_ordinal = ordinal
    if root not in ordinal:
        nxt = int(getattr(hb, "_subagent_next_n", 0) or 0) + 1
        hb._subagent_next_n = nxt
        ordinal[root] = nxt
    return root, int(ordinal[root])

def system_box_total(sp: Optional[dict[str, Any]]) -> int:
    """Same number as the System card header In (dashboard ``renderSystemBootstrap`` tot).

    Copy that line — do not re-derive from official Input or first-turn usage.
    """
    if not isinstance(sp, dict):
        return 0
    raw: list[dict[str, Any]] = [
        p
        for p in (sp.get("parts") or [])
        if isinstance(p, dict) and p.get("kind") != "hooks"
    ]
    residual = _int(sp.get("message_residual_tokens"))
    kinds = {p.get("kind") for p in raw}
    if residual > 0 and "tool_definitions" not in kinds and "tool_defs_message" not in kinds:
        raw.append({"tokens": residual})
    parts_tok = 0
    for p in raw:
        if p.get("tokens") is not None:
            parts_tok += _int(p.get("tokens"))
        else:
            parts_tok += _int(p.get("tokens_in"))
    if sp.get("message_residual_tokens") is not None or raw:
        return int(parts_tok)
    return _int(sp.get("tokens_in") or sp.get("logical_tokens") or sp.get("uncached_est"))

def _sys_dict_from_round(rr: dict[str, Any]) -> Optional[dict[str, Any]]:
    if not isinstance(rr, dict):
        return None
    sp = rr.get("system_prompt")
    if isinstance(sp, dict) and (
        sp.get("kind") == "system_prompt"
        or system_box_total(sp) > 0
        or _int(sp.get("tokens_in") or sp.get("logical_tokens")) > 0
    ):
        return sp
    bd = rr.get("breakdown") if isinstance(rr.get("breakdown"), dict) else {}
    tok = _int(bd.get("system_in_tokens"))
    if tok > 0:
        return {
            "tokens_in": tok,
            "cost_in_usd": float(bd.get("system_in_usd") or 0),
            "estimate_usd": float(bd.get("system_in_usd") or 0),
        }
    return None

def capture_child_sys(hb: Any, root: str, rounds: Optional[list]) -> None:
    """Keep the original spawn System card (never a resume dir). Refresh from live R1."""
    if hb is None:
        return
    key = str(root or "").strip().lower()
    if not key:
        return
    cache = getattr(hb, "_child_sys", None)
    if not isinstance(cache, dict):
        cache = {}
        hb._child_sys = cache
    for rr in rounds or []:
        sp = _sys_dict_from_round(rr) if isinstance(rr, dict) else None
        if sp and system_box_total(sp) > 0:
            cache[key] = sp
            return

def _sys_from_snaps(hb: Any, uid: str, root: str) -> Optional[dict[str, Any]]:
    """Prefer live child System card; ``_child_sys`` only if R1 was pruned."""
    snaps = getattr(hb, "_child_round_snaps", None) if hb is not None else None
    if isinstance(snaps, dict):
        for key in (root, uid):
            for rr in snaps.get(key) or []:
                sp = _sys_dict_from_round(rr) if isinstance(rr, dict) else None
                if sp and system_box_total(sp) > 0:
                    return sp
    cache = getattr(hb, "_child_sys", None) if hb is not None else None
    if isinstance(cache, dict):
        for key in (root, uid):
            sp = cache.get(str(key or "").strip().lower())
            if isinstance(sp, dict) and system_box_total(sp) > 0:
                return sp
    return None

def _price_sys_card(sp: dict[str, Any]) -> dict[str, Any]:
    tok = system_box_total(sp)
    usd = float(sp.get("cost_in_usd") or 0)
    try:
        est_usd = float(sp.get("estimate_usd") if sp.get("estimate_usd") is not None else usd)
    except (TypeError, ValueError):
        est_usd = usd
    return {
        "tokens_in": tok,
        "tokens_cached": 0,
        "tokens_out": 0,
        "cost_in_usd": usd,
        "cost_cached_usd": 0.0,
        "cost_out_usd": 0.0,
        "estimate_usd": est_usd,
        "official_usd": usd,
        "is_sys": True,
        "resume_index": 0,
    }

def _prior_round_extracts(
    hb: Any, root: str, current: dict[str, Any]
) -> int:
    n = 0
    if hb is None:
        return 0
    for r in list(getattr(hb, "rounds", None) or []):
        if r is current:
            break
        if not isinstance(r, dict):
            continue
        for step in r.get("model_steps") or []:
            if not isinstance(step, dict):
                continue
            for sa in step.get("subagents_after") or []:
                if not isinstance(sa, dict) or sa.get("is_sys"):
                    continue
                rid = str(sa.get("root_session_id") or sa.get("session_id") or "")
                if rid == root:
                    n += 1
    return n

def _sys_already_shown(
    hb: Any,
    uid: str,
    current: dict[str, Any],
    *,
    root: Optional[str] = None,
) -> bool:
    """One Sys line per original spawn (root), never again on resume."""
    if hb is None:
        return False
    want_uid = str(uid or "").strip().lower()
    want_root = str(root or uid or "").strip().lower()
    if not want_uid and not want_root:
        return False
    for r in list(getattr(hb, "rounds", None) or []):
        if r is current:
            break
        if not isinstance(r, dict):
            continue
        for step in r.get("model_steps") or []:
            if not isinstance(step, dict):
                continue
            for sa in step.get("subagents_after") or []:
                if not isinstance(sa, dict) or not sa.get("is_sys"):
                    continue
                sid = str(sa.get("session_id") or "").strip().lower()
                rid = str(sa.get("root_session_id") or "").strip().lower()
                if sid == want_uid or (want_root and want_root in (sid, rid)):
                    return True
    return False

def is_spawn_tool(t: Optional[dict[str, Any]]) -> bool:
    if not isinstance(t, dict):
        return False
    name = str(t.get("name") or "").lower()
    title = str(t.get("title") or "").lower()
    if "get_command" in name or "get_command" in title:
        return False
    if name == "spawn_subagent" or "spawn_subagent" in name or "spawn_subagent" in title:
        return True
    if "spawn" in name and "sub" in name:
        return True
    blob = str(t.get("result_preview") or "")
    if "subagent started" in blob.lower():
        return True
    return False

def _child_turn_list(
    hb: Any,
    base: dict[str, Any],
    uid: str,
    root: str,
) -> list[dict[str, Any]]:
    snaps = getattr(hb, "_child_round_snaps", None) if hb is not None else None
    snap_turns: list[dict[str, Any]] = []
    if isinstance(snaps, dict):
        for key in (uid, root):
            for cr in snaps.get(key) or []:
                if not isinstance(cr, dict):
                    continue
                u = cr.get("usage_raw") or cr.get("usage")
                if not isinstance(u, dict):
                    continue
                if _int(u.get("modelCalls") or u.get("inputTokens") or u.get("outputTokens")) <= 0:
                    continue
                snap_turns.append(u)
            if snap_turns:
                break
    turns = snap_turns or list(base.get("turns") or [])
    if not turns and isinstance(base.get("usage"), dict) and base.get("usage"):
        turns = [dict(base["usage"])]
    usable = [
        tu
        for tu in turns
        if _int(tu.get("inputTokens")) > 0
        or _int(tu.get("outputTokens")) > 0
        or _int(tu.get("modelCalls")) > 0
        or _int(tu.get("cachedReadTokens")) > 0
    ]
    return usable

def _child_round_rows(
    hb: Any, uid: str, root: str
) -> list[dict[str, Any]]:
    snaps = getattr(hb, "_child_round_snaps", None) if hb is not None else None
    if not isinstance(snaps, dict):
        return []
    for key in (uid, root):
        rows = [
            r
            for r in (snaps.get(key) or [])
            if isinstance(r, dict)
            and (r.get("completed") or isinstance(r.get("usage_raw"), dict))
        ]
        if rows:
            return rows
    return []

def _round_display_usage(rr: dict[str, Any]) -> dict[str, Any]:
    """Parent Agent line = this round's I/O, never System (Sys card already)."""
    bd = rr.get("breakdown") if isinstance(rr.get("breakdown"), dict) else {}
    up = rr.get("user_prompt") if isinstance(rr.get("user_prompt"), dict) else {}
    usage = rr.get("usage_raw") if isinstance(rr.get("usage_raw"), dict) else {}
    steps = [s for s in (rr.get("model_steps") or []) if isinstance(s, dict)]

    # First path: child ledger (breakdown / dual-write round fields).
    tree = as_opt_int(bd.get("tree_in_tokens"))
    if tree is None:
        tree = as_opt_int(rr.get("tree_in_tokens"))

    cache = as_opt_int(bd.get("cached_tokens"))
    if cache is None:
        cache = _int(
            rr.get("tokens_cached") or usage.get("cachedReadTokens")
        )

    out = as_opt_int(rr.get("output_tokens"))
    if out is None:
        out = as_opt_int(usage.get("outputTokens"))
    if out is None:
        out = as_opt_int(bd.get("output_tokens"))
    if out is None:
        out = sum(_int(s.get("tokens_out")) for s in steps)

    if tree is not None:
        tin = int(tree)
    else:
        # legacy no-snap: payload without breakdown.tree_in
        user = _int(
            up.get("tokens_in") or up.get("uncached_est") or bd.get("user_in_tokens")
        )
        call_in = sum(_int(s.get("tokens_in")) for s in steps)
        tin = user + call_in
        sys_tok = 0
        sp = rr.get("system_prompt")
        if isinstance(sp, dict):
            sys_tok = system_box_total(sp)
        off_in = _int(usage.get("inputTokens"))
        off_cache = _int(usage.get("cachedReadTokens"))
        if tin <= 0 and off_in > 0:
            off_unc = max(0, off_in - min(off_cache, off_in))
            tin = max(0, off_unc - sys_tok)
            if cache <= 0:
                cache = off_cache

    synth = {
        "inputTokens": int(tin) + int(cache),
        "cachedReadTokens": int(cache),
        "outputTokens": int(out),
        "reasoningTokens": _int(usage.get("reasoningTokens")),
        "modelCalls": _int(usage.get("modelCalls")) or len(steps),
    }
    priced = price_child_usage(synth)
    tree_usd = bd.get("tree_in_usd")
    if tree_usd is None:
        tree_usd = rr.get("cost_tree_in_usd")
    if tree_usd is not None:
        try:
            priced["cost_in_usd"] = float(tree_usd)
        except (TypeError, ValueError):
            pass
    cout = bd.get("output_usd")
    if cout is None:
        cout = rr.get("cost_out_usd")
    if cout is not None:
        try:
            priced["cost_out_usd"] = float(cout)
        except (TypeError, ValueError):
            pass
    ccache = bd.get("cached_usd")
    if ccache is None:
        ccache = rr.get("cost_cached_usd")
    if ccache is not None:
        try:
            priced["cost_cached_usd"] = float(ccache)
        except (TypeError, ValueError):
            pass
    priced["estimate_usd"] = (
        float(priced.get("cost_in_usd") or 0)
        + float(priced.get("cost_cached_usd") or 0)
        + float(priced.get("cost_out_usd") or 0)
    )
    priced["official_usd"] = None
    return priced

def _peel_system_from_turn(
    tu: dict[str, Any], sys_tok: int
) -> dict[str, Any]:
    """Drop System In from the first official child turn (already on Sys card)."""
    if sys_tok <= 0 or not isinstance(tu, dict):
        return dict(tu)
    out = dict(tu)
    inn = _int(out.get("inputTokens"))
    cache = _int(out.get("cachedReadTokens"))
    unc = max(0, inn - min(cache, inn))
    new_unc = max(0, unc - int(sys_tok))
    out["inputTokens"] = int(new_unc) + int(cache)
    out["costUsdTicks"] = 0
    return out

def _extract_one(
    hb: Any,
    base: dict[str, Any],
    uid: str,
    *,
    parent_dir: Optional[Path],
    alias: dict[str, str],
    index: int,
) -> Optional[dict[str, Any]]:
    """One child-round extract for the Nth parent get (0-based index).

    The Agent line is that round's In/Cached/Out — never the session total
    and never System (Sub Agent N Sys already showed the System card).
    """
    root, n = _ordinal_n(hb, uid, parent_dir=parent_dir, alias=alias)
    rows = _child_round_rows(hb, uid, root)
    if rows:
        if index < 0 or index >= len(rows):
            return None
        rr = rows[index]
        priced = _round_display_usage(rr)
        usage_d = dict(rr.get("usage_raw") or {})
    else:
        usable = _child_turn_list(hb, base, uid, root)
        if usable:
            if index < 0 or index >= len(usable):
                return None
            tu = dict(usable[index])
            if index == 0:
                tu = _peel_system_from_turn(
                    tu, system_box_total(_sys_from_snaps(hb, uid, root))
                )
            priced = price_child_usage(tu)
            usage_d = dict(tu)
        else:
            priced = {
                k: base.get(k)
                for k in (
                    "tokens_in",
                    "tokens_cached",
                    "tokens_out",
                    "cost_in_usd",
                    "cost_cached_usd",
                    "cost_out_usd",
                    "estimate_usd",
                    "official_usd",
                )
                if k in base
            }
            if not priced:
                priced = price_child_usage(base.get("usage") or {})
            if index == 0 and priced:
                sys_tok = system_box_total(_sys_from_snaps(hb, uid, root))
                if sys_tok > 0:
                    priced = dict(priced)
                    priced["tokens_in"] = max(
                        0, _int(priced.get("tokens_in")) - sys_tok
                    )
            usage_d = dict(base.get("usage") or {})
    k = index + 1
    card = dict(base)
    card.update(priced)
    card["n"] = n
    card["resume_index"] = k
    card["root_session_id"] = root
    card["is_sys"] = False
    card["usage"] = usage_d
    card["label"] = _card_label(n, k, multi=False)
    return card

def _sys_card(
    hb: Any,
    base: dict[str, Any],
    uid: str,
    *,
    parent_dir: Optional[Path],
    alias: dict[str, str],
    current_round: dict[str, Any],
    already_this_round: bool = False,
) -> Optional[dict[str, Any]]:
    root, n = _ordinal_n(hb, uid, parent_dir=parent_dir, alias=alias)
    if already_this_round or _sys_already_shown(
        hb, uid, current_round, root=root
    ):
        return None
    sp = _sys_from_snaps(hb, uid, root)
    if not sp:
        sp = {"tokens_in": 0, "cost_in_usd": 0.0}
    card = dict(base)
    card.update(_price_sys_card(sp))
    card["n"] = n
    card["root_session_id"] = root
    card["session_id"] = uid
    card["title"] = card.get("title") or "Sys"
    card["label"] = _card_label(n, 0, multi=False, is_sys=True)
    return card

def _stub_child(
    uid: str,
    *,
    parent_dir: Optional[Path],
    hb: Any,
) -> dict[str, Any]:
    d = sibling_session_dir(parent_dir, uid)
    cache = getattr(hb, "_child_usage_cache", None) if hb is not None else None
    turns = load_session_official_turns(d, cache=cache, cache_key=str(uid)) if d else []
    summary = read_session_summary(d) if d else {}
    title = summary.get("session_summary") or summary.get("generated_title")
    return {
        "session_id": uid,
        "peeled": False,
        "usage": {},
        "turns": turns,
        "title": title,
        "agent_name": summary.get("agent_name"),
        "session_kind": summary.get("session_kind") or "subagent",
        **price_child_usage(turns[0] if turns else {}),
    }

def relabel_subagent_cards(hb: Any, extra_rounds: Optional[list] = None) -> None:
    """R1 iff this agent has 2+ parent returns (session-wide), not per wait."""
    rounds = []
    if hb is not None:
        rounds.extend(getattr(hb, "rounds", None) or [])
        open_r = getattr(hb, "_open", None)
        if isinstance(open_r, dict) and open_r not in rounds:
            rounds.append(open_r)
    for extra in extra_rounds or []:
        if isinstance(extra, dict) and extra not in rounds:
            rounds.append(extra)
    maxk: dict[int, int] = {}
    cards: list[dict[str, Any]] = []
    for r in rounds:
        if not isinstance(r, dict):
            continue
        for step in r.get("model_steps") or []:
            if not isinstance(step, dict):
                continue
            for sa in step.get("subagents_after") or []:
                if not isinstance(sa, dict):
                    continue
                n = int(sa.get("n") or 0) or 1
                if sa.get("is_sys"):
                    sa["label"] = _card_label(n, 0, multi=False, is_sys=True)
                    continue
                cards.append(sa)
                k = int(sa.get("resume_index") or 0)
                maxk[n] = max(maxk.get(n, 0), k)
    for sa in cards:
        n = int(sa.get("n") or 0) or 1
        k = int(sa.get("resume_index") or 0)
        sa["resume_max"] = maxk.get(n, 0)
        sa["label"] = _card_label(n, k, multi=maxk.get(n, 0) > 1)

def attach_subagents_after_steps(
    round_: dict[str, Any],
    peel_meta: dict[str, Any],
    *,
    hb: Any = None,
) -> None:
    """
    Spawn → Sub Agent N Sys after that LLM call (not a round).
    Each parent get_command return → the next child-round extract only
    (R2 is later, not glued on the first wait).
    """
    children = [
        c
        for c in (peel_meta.get("children") or [])
        if isinstance(c, dict) and c.get("session_id")
    ]
    by_id = {str(c["session_id"]).lower(): c for c in children}
    parent_dir = getattr(hb, "_session_dir", None) if hb is not None else None
    alias = collect_resume_alias_from_round(round_)
    if hb is not None:
        stored = getattr(hb, "_resume_alias", None)
        if not isinstance(stored, dict):
            stored = {}
            hb._resume_alias = stored
        for r in list(getattr(hb, "rounds", None) or []):
            if isinstance(r, dict):
                stored.update(collect_resume_alias_from_round(r))
        stored.update(alias)
        alias = dict(stored)
    steps = [s for s in (round_.get("model_steps") or []) if isinstance(s, dict)]
    for step in steps:
        step["subagents_after"] = []

    def _resolve(uid: str, *, spawn: bool = False) -> Optional[dict[str, Any]]:
        uid = str(uid or "").strip().lower()
        if not uid:
            return None
        if uid in by_id:
            return by_id[uid]
        d = sibling_session_dir(parent_dir, uid)
        if d is None and not spawn:
            return None
        stub = _stub_child(uid, parent_dir=parent_dir, hb=hb)
        by_id[uid] = stub
        return stub

    extract_n: dict[str, int] = {}
    sys_this: set[str] = set()

    def _root_of(uid: str, amap: dict[str, str]) -> str:
        return root_subagent_id(uid, parent_dir=parent_dir, alias=amap)

    for step in steps:
        after: list[dict[str, Any]] = []
        for t in step.get("tools") or []:
            if not isinstance(t, dict):
                continue
            name = str(t.get("name") or "")
            ids: list[str] = []
            if t.get("subagent_id"):
                ids.append(str(t["subagent_id"]).lower())
            for x in t.get("subagent_ids") or []:
                ids.append(str(x).lower())
            if is_spawn_tool(t) or name == "spawn_subagent":
                ids.extend(spawn_session_ids(t))
                seen_sp: set[str] = set()
                for uid in ids:
                    uid = str(uid or "").strip().lower()
                    if not uid or uid in seen_sp or not UUID_RE.fullmatch(uid):
                        continue
                    seen_sp.add(uid)
                    base = _resolve(uid, spawn=True)
                    if not base:
                        continue
                    rf = tool_resume_from(t)
                    amap = dict(alias)
                    if rf and UUID_RE.fullmatch(rf) and rf != uid:
                        amap[uid] = rf
                        alias[uid] = rf
                    # Resume is the same agent — Sys only on the original spawn.
                    if rf:
                        _ordinal_n(hb, uid, parent_dir=parent_dir, alias=amap)
                        continue
                    kind = None
                    d = sibling_session_dir(parent_dir, uid)
                    if d is not None:
                        kind = session_kind_of(d)
                    if kind == "subagent_resume":
                        _ordinal_n(hb, uid, parent_dir=parent_dir, alias=amap)
                        continue
                    sys_c = _sys_card(
                        hb,
                        base,
                        uid,
                        parent_dir=parent_dir,
                        alias=amap,
                        current_round=round_,
                        already_this_round=uid in sys_this or root_subagent_id(
                            uid, parent_dir=parent_dir, alias=amap
                        ) in sys_this,
                    )
                    if sys_c:
                        sys_this.add(uid)
                        sys_this.add(str(sys_c.get("root_session_id") or uid))
                        after.append(sys_c)
                continue
            if name != "get_command_or_subagent_output":
                continue
            ids.extend(
                extract_ids_from_text(t.get("result_preview"), t.get("title"))
            )
            seen_g: set[str] = set()
            for uid in ids:
                uid = str(uid or "").strip().lower()
                if not uid or uid in seen_g or not UUID_RE.fullmatch(uid):
                    continue
                seen_g.add(uid)
                base = _resolve(uid)
                if not base:
                    continue
                rf = tool_resume_from(t)
                amap = dict(alias)
                if rf and UUID_RE.fullmatch(rf) and rf != uid:
                    amap[uid] = rf
                root = _root_of(uid, amap)
                if root not in extract_n:
                    extract_n[root] = _prior_round_extracts(hb, root, round_)
                card = _extract_one(
                    hb,
                    base,
                    uid,
                    parent_dir=parent_dir,
                    alias=amap,
                    index=extract_n[root],
                )
                if card:
                    extract_n[root] = extract_n[root] + 1
                    after.append(card)
        if after:
            step["subagents_after"] = after
    relabel_subagent_cards(hb, [round_])

def ensure_lineage_subagent_cards(
    rounds: list[dict[str, Any]],
    extra_ids: Iterable[str],
    *,
    hb: Any = None,
) -> None:
    """Paint Sub Agent cards for spawn events that never landed on a tool step."""
    shown: set[str] = set()
    last_step: Optional[dict[str, Any]] = None
    last_round: Optional[dict[str, Any]] = None
    for rr in rounds:
        if not isinstance(rr, dict):
            continue
        last_round = rr
        for step in rr.get("model_steps") or []:
            if not isinstance(step, dict):
                continue
            last_step = step
            for sa in step.get("subagents_after") or []:
                if not isinstance(sa, dict):
                    continue
                for key in (sa.get("session_id"), sa.get("root_session_id")):
                    s = str(key or "").strip().lower()
                    if s:
                        shown.add(s)
    parent_dir = getattr(hb, "_session_dir", None) if hb is not None else None
    alias = getattr(hb, "_resume_alias", None) if hb is not None else None
    if not isinstance(alias, dict):
        alias = {}
    if last_step is None or last_round is None:
        return
    after = list(last_step.get("subagents_after") or [])
    added = False
    for raw in extra_ids:
        uid = str(raw or "").strip().lower()
        if not uid or not UUID_RE.fullmatch(uid) or uid in shown:
            continue
        root = root_subagent_id(uid, parent_dir=parent_dir, alias=alias)
        if root in shown or uid in shown:
            continue
        d = sibling_session_dir(parent_dir, uid)
        if d is None:
            continue
        stub = _stub_child(uid, parent_dir=parent_dir, hb=hb)
        sys_c = _sys_card(
            hb,
            stub,
            uid,
            parent_dir=parent_dir,
            alias=alias,
            current_round=last_round,
            already_this_round=False,
        )
        card = sys_c or _extract_one(
            hb, stub, uid, parent_dir=parent_dir, alias=alias, index=0
        )
        if not card:
            continue
        after.append(card)
        shown.add(uid)
        shown.add(root)
        added = True
    if added:
        last_step["subagents_after"] = after
        relabel_subagent_cards(hb, rounds)
