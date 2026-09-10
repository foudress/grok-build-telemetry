"""Moved from session/monitor.py — same bodies."""
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
from token_telemetry.session.enrich import _mean_round_tps, _sum_rounds_estimate

API_ROUNDS = MAX_ROUNDS_RETAINED

class _ChildWatch:
    """Incremental hierarchy for one sub-agent session under a parent."""

    def __init__(self, session_dir: Path) -> None:
        self.session_dir = session_dir
        self.session_id = session_dir.name
        self.root_id = session_dir.name
        self.hierarchy = HierarchyBuilder()
        self.hierarchy.set_session_dir(session_dir)
        self._updates_path = session_dir / "updates.jsonl"
        self._updates_offset = 0
        self.turns: list[dict[str, Any]] = []
        self._turn_seq: int = 0
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
                    self._turn_seq += 1
                    self.turns.append(
                        {
                            "index": self._turn_seq,
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
        self._attach_own_subagent_cards()

    def _attach_own_subagent_cards(self) -> None:
        """Sys / extract cards for workers this session spawned (mapper → workers)."""
        rounds = list(self.hierarchy.rounds)
        open_r = getattr(self.hierarchy, "_open", None)
        if isinstance(open_r, dict) and open_r not in rounds:
            rounds.append(open_r)
        lineage: list[str] = []
        for rr in rounds:
            if not isinstance(rr, dict):
                continue
            peel = rr.get("subagent_peel")
            attach_subagents_after_steps(
                rr,
                peel if isinstance(peel, dict) else {"children": []},
                hb=self.hierarchy,
            )
            lineage.extend(collect_child_ids_from_round(rr))
        ensure_lineage_subagent_cards(rounds, lineage, hb=self.hierarchy)

    def snapshot(self, enrich_round) -> dict[str, Any]:
        summary = read_session_summary(self.session_dir)
        official = sum(float(t.get("official_usd") or 0) for t in self.turns)
        rounds_all = self.hierarchy.snapshot_rounds(include_open=True)
        estimate = _sum_rounds_estimate(rounds_all)
        rounds_raw = rounds_all[-API_ROUNDS:] if len(rounds_all) > API_ROUNDS else rounds_all
        rounds = [enrich_round(r) for r in rounds_raw]
        from token_telemetry.session.discover import pick_session_title

        title = pick_session_title(
            summary,
            session_id=self.session_id,
            extra=summary.get("agent_name"),
        )
        return {
            "session_id": self.session_id,
            "root_session_id": getattr(self, "root_id", None) or self.session_id,
            "session_kind": summary.get("session_kind") or "subagent",
            "agent_name": summary.get("agent_name"),
            "title": title,
            "label": title,
            "official_usd": round(official, 6),
            "estimate_usd": round(estimate, 6),
            "turns": list(self.turns),
            "rounds": rounds,
            "live": dict(self.live),
            "error": self.error,
            "gen_tokens_per_sec": _mean_round_tps(rounds_all),
        }

def child_lineage(self) -> tuple[list[str], dict[str, str], dict[str, str]]:
        """root ids (spawn order), alias new→old, root → latest session dir id."""
        ids: list[str] = []
        seen: set[str] = set()
        alias: dict[str, str] = {}
        rounds = list(self.hierarchy.rounds)
        open_r = getattr(self.hierarchy, "_open", None)
        if isinstance(open_r, dict):
            rounds = rounds + [open_r]
        for rr in rounds:
            if not isinstance(rr, dict):
                continue
            alias.update(collect_resume_alias_from_round(rr))
            for uid in collect_child_ids_from_round(rr):
                if uid not in seen:
                    seen.add(uid)
                    ids.append(uid)
        for uid in getattr(self, "_spawned_child_ids", None) or []:
            if uid not in seen:
                seen.add(uid)
                ids.append(uid)
        parent_dir = self.session_dir
        roots: list[str] = []
        root_seen: set[str] = set()
        latest: dict[str, str] = {}
        for uid in ids:
            root = root_subagent_id(uid, parent_dir=parent_dir, alias=alias)
            if root not in root_seen:
                root_seen.add(root)
                roots.append(root)
        for root in roots:
            latest[root] = latest_in_resume_chain(
                root, ids, parent_dir=parent_dir, alias=alias
            )
        return roots, alias, latest

def sync_children(self) -> None:
        if not self.session_dir:
            self._children = {}
            return
        # Sub-agent sessions (direct_foudres mapper / workers) also spawn
        # workers — watch them like a normal parent, not mapper-spawn only.
        roots, alias, latest = self._child_lineage()
        parent_dir = self.session_dir
        wanted: dict[str, str] = {}
        # Watch every child session dir (spawn + resume) so Sys + $ use original
        # spawn rounds; UI tabs still collapse to `latest` per root.
        lineage_ids: list[str] = []
        seen_ids: set[str] = set()
        rounds_src = list(self.hierarchy.rounds)
        open_r0 = getattr(self.hierarchy, "_open", None)
        if isinstance(open_r0, dict):
            rounds_src = rounds_src + [open_r0]
        for rr in rounds_src:
            if not isinstance(rr, dict):
                continue
            for uid in collect_child_ids_from_round(rr):
                if uid not in seen_ids:
                    seen_ids.add(uid)
                    lineage_ids.append(uid)
        for uid in getattr(self, "_spawned_child_ids", None) or []:
            if uid not in seen_ids:
                seen_ids.add(uid)
                lineage_ids.append(uid)
        for uid in lineage_ids:
            root = root_subagent_id(uid, parent_dir=parent_dir, alias=alias)
            wanted[uid] = root
        for root in roots:
            wanted.setdefault(root, root)
            last = latest.get(root)
            if last:
                wanted.setdefault(last, root)
        for uid in list(self._children):
            if uid not in wanted:
                self._children.pop(uid, None)
        ordinal = getattr(self.hierarchy, "_subagent_ordinal", None)
        if ordinal is None:
            ordinal = {}
            self.hierarchy._subagent_ordinal = ordinal
        snaps = getattr(self.hierarchy, "_child_round_snaps", None)
        if not isinstance(snaps, dict):
            snaps = {}
            self.hierarchy._child_round_snaps = snaps
        nested: list[tuple[str, str]] = []
        for uid, root in list(wanted.items()):
            if root not in ordinal:
                nxt = int(getattr(self.hierarchy, "_subagent_next_n", 0) or 0) + 1
                self.hierarchy._subagent_next_n = nxt
                ordinal[root] = nxt
            watch = self._children.get(uid)
            if watch is None or watch.session_id != uid:
                d = sibling_session_dir(self.session_dir, uid)
                if d is None or not (d / "updates.jsonl").is_file():
                    continue
                watch = _ChildWatch(d)
                self._children[uid] = watch
            watch.root_id = root
            watch.tick(self._read_new_lines)
            child_rounds = watch.hierarchy.snapshot_rounds(include_open=True)
            snaps[uid] = child_rounds
            if uid == root:
                snaps[root] = child_rounds
                capture_child_sys(self.hierarchy, root, child_rounds)
            # Workers spawned inside this child (mapper → worker, not only
            # the mapper spawn on the parent wait-round).
            for rr in child_rounds or []:
                if not isinstance(rr, dict):
                    continue
                for cid in collect_child_ids_from_round(rr):
                    if cid in seen_ids:
                        continue
                    seen_ids.add(cid)
                    lineage_ids.append(cid)
                    croot = root_subagent_id(cid, parent_dir=parent_dir, alias=alias)
                    nested.append((cid, croot))
                    if croot not in roots:
                        roots.append(croot)
        for cid, croot in nested:
            wanted[cid] = croot
        # Second pass: watches for nested worker ids discovered above.
        for uid, root in list(wanted.items()):
            if uid in self._children:
                continue
            if root not in ordinal:
                nxt = int(getattr(self.hierarchy, "_subagent_next_n", 0) or 0) + 1
                self.hierarchy._subagent_next_n = nxt
                ordinal[root] = nxt
            d = sibling_session_dir(self.session_dir, uid)
            if d is None or not (d / "updates.jsonl").is_file():
                continue
            watch = _ChildWatch(d)
            self._children[uid] = watch
            watch.root_id = root
            watch.tick(self._read_new_lines)
            child_rounds = watch.hierarchy.snapshot_rounds(include_open=True)
            snaps[uid] = child_rounds
            if uid == root:
                snaps[root] = child_rounds
                capture_child_sys(self.hierarchy, root, child_rounds)
        rounds = list(self.hierarchy.rounds)
        open_r = getattr(self.hierarchy, "_open", None)
        if isinstance(open_r, dict) and open_r not in rounds:
            rounds.append(open_r)
        for rr in rounds:
            if not isinstance(rr, dict):
                continue
            attach_subagents_after_steps(
                rr,
                rr.get("subagent_peel") if isinstance(rr.get("subagent_peel"), dict) else {"children": []},
                hb=self.hierarchy,
            )
        ensure_lineage_subagent_cards(rounds, lineage_ids, hb=self.hierarchy)
