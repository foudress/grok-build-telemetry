"""Moved from session/aggregate.py — same bodies."""
from __future__ import annotations

import json
import threading
from calendar import monthrange
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Optional

from token_telemetry.session.discover import (
    _parse_iso_to_epoch,
    _read_session_summary,
    list_session_dirs,
    pick_session_title,
)
from token_telemetry.session.calc_cache import code_sig, load_calc, save_calc
from token_telemetry.session.period_attr import (
    cached_attr_events,
    clear_attr_mem,
    is_attr_warm,
)
from token_telemetry.pricing.rates import normalize_model_id
from token_telemetry.session.ids import add_uuid, child_ids_from_update
from token_telemetry.session.scan import _empty_acc, _round_acc

_PEEL_KEYS = (
    "tokens_in",
    "tokens_cached",
    "tokens_out",
    "tokens_reason",
    "cost_in_usd",
    "cost_cached_usd",
    "cost_out_usd",
    "cost_reason_usd",
    "official_usd",
    "estimate_usd",
)
from token_telemetry.session.kind import is_subagent_kind
from token_telemetry.session.usage import (
    price_child_usage,
    root_subagent_id,
    summary_parent_session_id,
)

def _merge_resume_chain(chain: list[dict[str, Any]]) -> dict[str, Any]:
    """One daily row per agent: keep the latest resume/spawn only (no sum).

    Harness wake/resume creates a new session dir per round. Listing or summing
    every dir duplicates Sub Agent rows and double-counts In/Cached/Out. Parent
    stays the orchestrator (already remapped on each row before merge).
    """
    if not chain:
        return {}
    if len(chain) == 1:
        row = dict(chain[0])
        row["session_kind"] = "subagent"
        return row
    chain = sorted(
        chain, key=lambda c: float(c.get("first_epoch") or c.get("last_epoch") or 0)
    )
    latest = dict(chain[-1])
    # Prefer orchestrator parent_id / root from any earlier node if latest lacks them.
    for c in chain:
        if not latest.get("parent_id") and c.get("parent_id"):
            latest["parent_id"] = c["parent_id"]
        rid = c.get("root_session_id")
        if rid:
            latest["root_session_id"] = rid
            break
    else:
        if not latest.get("root_session_id"):
            latest["root_session_id"] = chain[0].get("session_id")
    latest["session_kind"] = "subagent"
    latest["resume_index"] = 0
    return latest

def _order_sessions(sessions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Parents first; one Sub Agent row per root (latest resume), indented under parent."""
    by_id = {str(s["session_id"]).lower(): s for s in sessions}

    def _first(s: dict[str, Any]) -> float:
        return float(s.get("first_epoch") or s.get("last_epoch") or 0)

    def _root_id(s: dict[str, Any]) -> str:
        sid = str(s.get("session_id") or "").lower()
        if s.get("root_session_id"):
            return str(s["root_session_id"]).lower()
        if not is_subagent_kind(s.get("session_kind")):
            return sid
        pid = (s.get("parent_id") or "").lower()
        # Walk resume → original spawn while parent is also a sub-agent
        cur = sid
        seen: set[str] = set()
        while cur and cur not in seen:
            seen.add(cur)
            node = by_id.get(cur)
            if node is None:
                break
            if str(node.get("session_kind") or "") != "subagent_resume":
                return cur
            nxt = (node.get("parent_id") or "").lower()
            if not nxt or nxt == cur:
                return cur
            # parent_id is orchestrator (not a sub) → this node is the spawn
            pnode = by_id.get(nxt)
            if pnode is not None and not is_subagent_kind(pnode.get("session_kind")):
                return cur
            cur = nxt
        return cur or sid

    roots: list[dict[str, Any]] = []
    children: dict[str, list[dict[str, Any]]] = {}
    orphans: list[dict[str, Any]] = []
    for s in sessions:
        if is_subagent_kind(s.get("session_kind")):
            pid = (s.get("parent_id") or "").lower() or None
            if pid and pid in by_id and not is_subagent_kind(
                (by_id[pid] or {}).get("session_kind")
            ):
                children.setdefault(pid, []).append(s)
            else:
                orphans.append(s)
        else:
            roots.append(s)

    roots.sort(key=_first)
    orphans.sort(key=_first)
    for kids in children.values():
        kids.sort(key=_first)

    def _label_kids(kids: list[dict[str, Any]], n_parent: int) -> list[dict[str, Any]]:
        groups: dict[str, list[dict[str, Any]]] = {}
        order: list[str] = []
        for c in kids:
            rid = _root_id(c)
            c["root_session_id"] = rid
            if rid not in groups:
                groups[rid] = []
                order.append(rid)
            groups[rid].append(c)
        out: list[dict[str, Any]] = []
        for i, rid in enumerate(order, start=1):
            chain = groups[rid]
            chain.sort(key=_first)
            merged = _merge_resume_chain(chain)
            merged["n"] = n_parent
            merged["child_n"] = i
            merged["resume_index"] = 0
            merged["depth"] = 1
            merged["label"] = f"Sub Agent {i}"
            out.append(merged)
        return out

    ordered: list[dict[str, Any]] = []
    n_parent = 0
    for p in roots:
        n_parent += 1
        p["n"] = n_parent
        p["child_n"] = None
        p["depth"] = 0
        p["label"] = f"Session {n_parent}"
        ordered.append(p)
        pid = str(p["session_id"]).lower()
        ordered.extend(_label_kids(children.get(pid) or [], n_parent))
    if orphans:
        n_parent = n_parent or 1
        ordered.extend(_label_kids(orphans, n_parent))
    return ordered

def _place(epoch: float, specs: list[dict[str, Any]]) -> Optional[int]:
    for i, s in enumerate(specs):
        if s["start_epoch"] <= epoch < s["end_epoch"]:
            return i
    return None

def _add_cat_list(dest: dict[str, dict[str, Any]], segs: list[dict[str, Any]]) -> None:
    for s in segs or []:
        key = str(s.get("key") or "")
        if not key:
            continue
        prev = dest.get(key)
        if prev:
            prev["usd"] += float(s.get("usd") or 0)
            prev["tok"] += float(s.get("tok") or 0)
        else:
            dest[key] = {
                "key": key,
                "k": s.get("k") or key,
                "label": s.get("label") or key,
                "usd": float(s.get("usd") or 0),
                "tok": float(s.get("tok") or 0),
            }

def _cats_out(acc: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    rows = [v for v in acc.values() if (v.get("usd") or 0) > 0 or (v.get("tok") or 0) > 0]
    rows.sort(key=lambda x: str(x.get("key") or ""))
    return [
        {
            "key": r["key"],
            "k": r["k"],
            "label": r["label"],
            "usd": round(float(r["usd"]), 8),
            "tok": round(float(r["tok"]), 4),
        }
        for r in rows
    ]

def _acc_from_session_row(s: dict[str, Any]) -> dict[str, float]:
    acc = _empty_acc()
    for k in _PEEL_KEYS:
        if k.startswith("tokens_") or k == "turns":
            acc[k] = int(s.get(k) or 0)
        else:
            acc[k] = float(s.get(k) or 0)
    acc["turns"] = int(s.get("turns") or 0)
    return acc

def _sub_acc(dest: dict[str, float], src: dict[str, float]) -> None:
    """Subtract child bill from parent (floor 0). Turns stay on the parent."""
    for k in _PEEL_KEYS:
        if k == "turns":
            continue
        if k.startswith("tokens_"):
            dest[k] = max(0, int(dest.get(k) or 0) - int(src.get(k) or 0))
        else:
            dest[k] = max(0.0, float(dest.get(k) or 0) - float(src.get(k) or 0))

def _apply_acc_to_session_row(s: dict[str, Any], acc: dict[str, float]) -> None:
    rounded = _round_acc(acc)
    for k, v in rounded.items():
        s[k] = v

def _parent_covers_kids(parent: dict[str, float], kids: dict[str, float]) -> bool:
    """True when parent turn_completed already includes the child bill."""
    for k in ("tokens_in", "tokens_cached", "tokens_out"):
        if int(parent.get(k) or 0) < int(kids.get(k) or 0):
            return False
    return True

def _peel_parent_rows(sessions: list[dict[str, Any]]) -> None:
    """Parent turn_completed includes sub bills — subtract linked latest kids.

    direct_foudres (and similar) do **not** roll child usage into the parent
    wait-turn. Peeling would floor the parent to 0 and drop kids from period
    totals. Skip peel and mark ``_fold_kids`` so totals add child I/O.
    """
    for p in sessions:
        if is_subagent_kind(p.get("session_kind")) or int(p.get("depth") or 0) > 0:
            continue
        pid = str(p.get("session_id") or "").lower()
        if not pid:
            continue
        pac = _acc_from_session_row(p)
        kid_sum = _empty_acc()
        n_kids = 0
        for c in sessions:
            if int(c.get("depth") or 0) <= 0 and not is_subagent_kind(
                c.get("session_kind")
            ):
                continue
            if str(c.get("parent_id") or "").lower() != pid:
                continue
            n_kids += 1
            kacc = _acc_from_session_row(c)
            for k in _PEEL_KEYS:
                if k == "turns":
                    continue
                if k.startswith("tokens_"):
                    kid_sum[k] = int(kid_sum.get(k) or 0) + int(kacc.get(k) or 0)
                else:
                    kid_sum[k] = float(kid_sum.get(k) or 0) + float(kacc.get(k) or 0)
        if n_kids and not _parent_covers_kids(pac, kid_sum):
            p["_fold_kids"] = True
            continue
        p["_fold_kids"] = False
        if n_kids:
            _sub_acc(pac, kid_sum)
        _apply_acc_to_session_row(p, pac)

def _scale_turn_to_peeled(
    turn: dict[str, Any],
    raw: dict[str, float],
    peeled: dict[str, float],
) -> dict[str, Any]:
    """Scale one parent turn so session-sum matches peeled parent-only totals."""
    out = dict(turn)
    for k in _PEEL_KEYS:
        if k == "turns":
            continue
        r = float(raw.get(k) or 0)
        p = float(peeled.get(k) or 0)
        if r > 0:
            out[k] = (float(turn.get(k) or 0) * p / r)
        else:
            out[k] = 0 if k.startswith("tokens_") else 0.0
    if any(k.startswith("tokens_") for k in _PEEL_KEYS):
        for k in (
            "tokens_in",
            "tokens_cached",
            "tokens_out",
            "tokens_reason",
        ):
            out[k] = int(round(float(out.get(k) or 0)))
    return out
