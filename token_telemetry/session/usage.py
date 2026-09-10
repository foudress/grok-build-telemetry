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
from token_telemetry.util import as_int as _int

_USAGE_KEYS = (
    "inputTokens",
    "outputTokens",
    "reasoningTokens",
    "cachedReadTokens",
    "totalTokens",
    "costUsdTicks",
    "apiDurationMs",
    "modelCalls",
)


def empty_usage() -> dict[str, int]:
    return {k: 0 for k in _USAGE_KEYS}

def child_tier_context_tokens(usage: Optional[dict[str, Any]]) -> int:
    """Single-prompt context for the ≤/>200k tier (never lifetime sums)."""
    u = usage if isinstance(usage, dict) else {}
    inn = _int(u.get("inputTokens") or u.get("input_tokens"))
    calls = _int(u.get("modelCalls") or u.get("model_calls"))
    if calls > 1:
        return inn // max(1, calls)
    return inn

def price_child_usage(usage: Optional[dict[str, Any]]) -> dict[str, Any]:
    """In / Cached / Out tokens + list-rate $ for a child session bill."""
    u = usage if isinstance(usage, dict) else {}
    inn = _int(u.get("inputTokens") or u.get("input_tokens"))
    cache = _int(u.get("cachedReadTokens") or u.get("cached_read_tokens"))
    out = _int(u.get("outputTokens") or u.get("output_tokens"))
    reason = _int(u.get("reasoningTokens") or u.get("reasoning_tokens"))
    if cache > inn:
        cache = inn
    unc = max(0, inn - cache)
    ticks = _int(u.get("costUsdTicks") or u.get("cost_usd_ticks"))
    official = ticks_to_usd(ticks) if ticks else None
    # inputTokens / totalTokens are API SUMs — do not use them as peak.
    tier_ctx = child_tier_context_tokens(u)
    est = estimate_from_usage(u, peak_context_tokens=tier_ctx or None)
    parts = est.get("cost_usd") or {}
    cin = float(parts.get("uncached_input") or 0)
    ccache = float(parts.get("cached_input") or 0)
    cout = float(parts.get("output") or 0)
    est_tot = float(parts.get("total") or 0)
    reason = min(reason, out) if out else 0
    creason = (cout * (reason / out)) if out > 0 and reason > 0 else 0.0
    return {
        "tokens_in": unc,
        "tokens_cached": cache,
        "tokens_out": out,
        "tokens_reason": reason,
        "cost_in_usd": cin,
        "cost_cached_usd": ccache,
        "cost_out_usd": cout,
        "cost_reason_usd": creason,
        "estimate_usd": est_tot,
        "official_usd": official,
        "context_tokens_for_tier": tier_ctx,
    }

def add_usage(acc: dict[str, int], usage: Optional[dict[str, Any]]) -> dict[str, int]:
    if not isinstance(usage, dict):
        return acc
    for k in _USAGE_KEYS:
        acc[k] = _int(acc.get(k)) + _int(usage.get(k))
    return acc

def sub_usage(base: dict[str, Any], peel: dict[str, Any]) -> dict[str, Any]:
    """Subtract child official counters from a parent usage dict (floor 0)."""
    out = dict(base) if isinstance(base, dict) else {}
    for k in _USAGE_KEYS:
        if k not in out and k not in peel:
            continue
        out[k] = max(0, _int(out.get(k)) - _int(peel.get(k)))
    return out

def _usage_cache_key(session_dir: Path, cache_key: Optional[str] = None) -> str:
    if cache_key:
        return str(cache_key)
    return str(session_dir)

def _updates_jsonl_stat(path: Path) -> Optional[tuple[float, int]]:
    try:
        st = path.stat()
    except OSError:
        return None
    return (float(st.st_mtime), int(st.st_size))

def _cache_usage_hit(
    entry: Any, mtime: float, size: int
) -> Optional[dict[str, int]]:
    if not isinstance(entry, dict):
        return None
    if "usage" not in entry:
        return None
    if entry.get("mtime") != mtime or entry.get("size") != size:
        return None
    usage = entry.get("usage")
    return usage if isinstance(usage, dict) else None

def load_session_official_turns(
    session_dir: Optional[Path],
    cache: Optional[dict[str, Any]] = None,
    *,
    cache_key: Optional[str] = None,
) -> list[dict[str, int]]:
    """Each ``turn_completed.usage`` in order (not a lifetime sum)."""
    if session_dir is None:
        return []
    root = Path(session_dir)
    p = root / "updates.jsonl"
    if not p.is_file():
        return []
    stat = _updates_jsonl_stat(p)
    if stat is None:
        return []
    mtime, size = stat
    key = _usage_cache_key(root, cache_key) + ":turns"
    if cache is not None:
        entry = cache.get(key)
        if (
            isinstance(entry, dict)
            and entry.get("mtime") == mtime
            and entry.get("size") == size
            and isinstance(entry.get("turns"), list)
        ):
            return list(entry["turns"])
    try:
        raw = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    turns: list[dict[str, int]] = []
    for line in raw.splitlines():
        if "turn_completed" not in line:
            continue
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        upd = ((o.get("params") or {}).get("update") or {})
        if upd.get("sessionUpdate") != "turn_completed":
            continue
        u = empty_usage()
        add_usage(u, upd.get("usage") or {})
        turns.append(u)
    if cache is not None:
        cache[key] = {"turns": turns, "mtime": mtime, "size": size}
    return turns

def load_session_official_usage(
    session_dir: Optional[Path],
    cache: Optional[dict[str, Any]] = None,
    *,
    cache_key: Optional[str] = None,
) -> dict[str, int]:
    """Sum every turn_completed.usage in a session's updates.jsonl."""
    if session_dir is None:
        return empty_usage()
    root = Path(session_dir)
    p = root / "updates.jsonl"
    stat = _updates_jsonl_stat(p) if p.is_file() else None
    key = _usage_cache_key(root, cache_key)
    if cache is not None and stat is not None:
        hit = _cache_usage_hit(cache.get(key), stat[0], stat[1])
        if hit is not None:
            return hit
    acc = empty_usage()
    for u in load_session_official_turns(
        session_dir, cache=cache, cache_key=cache_key
    ):
        add_usage(acc, u)
    if cache is not None and stat is not None:
        cache[key] = {"usage": acc, "mtime": stat[0], "size": stat[1]}
    return acc

def sibling_session_dir(parent_dir: Optional[Path], session_id: str) -> Optional[Path]:
    """Resolve a child session dir.

    Grok writes each cwd under a different folder
    (``~/.grok/sessions/<encoded-cwd>/<id>``). A parent in ``C:\\Users\\…``
    can spawn children whose cwd is a project path — they are *not* next
    to the parent. Search the parent's sibling first, then every cwd
    folder under the sessions root.
    """
    if not session_id:
        return None
    sid = str(session_id).strip()
    if not sid:
        return None

    def _ok(p: Path) -> bool:
        return p.is_dir() and (p / "updates.jsonl").is_file()

    if parent_dir is not None:
        cand = Path(parent_dir).parent / sid
        if _ok(cand):
            return cand
        # Encoded-cwd folder itself named like the id (rare)
        named = Path(parent_dir).parent / sid
        if _ok(named):
            return named

    try:
        from token_telemetry.session.discover import SESSIONS_ROOT
    except ImportError:
        SESSIONS_ROOT = Path.home() / ".grok" / "sessions"
    root = SESSIONS_ROOT
    if not root.is_dir():
        return None
    try:
        kids = list(root.iterdir())
    except OSError:
        return None
    sid_l = sid.lower()
    for folder in kids:
        if not folder.is_dir():
            continue
        cand = folder / sid
        if _ok(cand):
            return cand
        if folder.name.lower() == sid_l and _ok(folder):
            return folder
    return None

def collect_child_ids_from_round(round_: dict[str, Any]) -> list[str]:
    """Unique sub-agent ids referenced by spawn / get tools in this round."""
    seen: set[str] = set()
    out: list[str] = []

    def _add(uid: Any) -> None:
        if not uid:
            return
        s = str(uid).strip().lower()
        if not UUID_RE.fullmatch(s) or s in seen:
            return
        seen.add(s)
        out.append(s)

    for step in round_.get("model_steps") or []:
        if not isinstance(step, dict):
            continue
        for t in step.get("tools") or []:
            if not isinstance(t, dict):
                continue
            _add(t.get("subagent_id"))
            for uid in t.get("subagent_ids") or []:
                _add(uid)
            name = str(t.get("name") or "")
            if name in (
                "spawn_subagent",
                "get_command_or_subagent_output",
                "kill_command_or_subagent",
            ):
                for uid in extract_ids_from_text(
                    t.get("result_preview"),
                    t.get("title"),
                ):
                    _add(uid)
    return out

def child_ids_to_peel(round_: dict[str, Any]) -> list[str]:
    """Ids billed on this parent wait-round (get_command only).

    Spawn-only rounds return [] — do not peel lifetime child usage before
    the parent turn that includes the child's official bill.
    """
    seen: set[str] = set()
    out: list[str] = []

    def _add(uid: Any) -> None:
        if not uid:
            return
        s = str(uid).strip().lower()
        if not UUID_RE.fullmatch(s) or s in seen:
            return
        seen.add(s)
        out.append(s)

    for step in round_.get("model_steps") or []:
        if not isinstance(step, dict):
            continue
        for t in step.get("tools") or []:
            if not isinstance(t, dict):
                continue
            name = str(t.get("name") or "")
            if name != "get_command_or_subagent_output":
                continue
            _add(t.get("subagent_id"))
            for uid in t.get("subagent_ids") or []:
                _add(uid)
            for uid in extract_task_ids(t.get("raw_input") or t.get("rawInput")):
                _add(uid)
            for uid in extract_ids_from_text(
                t.get("result_preview"),
                t.get("title"),
            ):
                _add(uid)
    return out

def peel_round_usage(
    usage: Optional[dict[str, Any]],
    *,
    parent_dir: Optional[Path],
    child_ids: Iterable[str],
    cache: Optional[dict[str, Any]] = None,
    already_peeled: Optional[dict[str, dict]] = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    Return (peeled_usage, peel_meta).

    Subtract only the increment since ``already_peeled[uid]`` (lifetime
    snapshot from an earlier parent wait-round). After a peel, the caller
    should record ``already_peeled[uid] = current_child_usage``.

    ``cache`` is mtime-aware (see ``load_session_official_usage``).

    ``peel_meta`` lists each child and the subtracted counters. Empty peel
    leaves usage unchanged.
    """
    src = dict(usage) if isinstance(usage, dict) else {}
    peel = empty_usage()
    children: list[dict[str, Any]] = []
    for uid in child_ids:
        d = sibling_session_dir(parent_dir, uid)
        if d is None:
            # get_command also waits on background shell tasks. Those ids are
            # not spawn_subagent sessions — do not paint empty Sub Agent cards.
            continue
        turns = load_session_official_turns(d, cache=cache, cache_key=str(uid))
        cu = empty_usage()
        for tu in turns:
            add_usage(cu, tu)
        prior = empty_usage()
        if already_peeled is not None:
            prev = already_peeled.get(uid)
            if isinstance(prev, dict):
                if isinstance(prev.get("usage"), dict) and (
                    "mtime" in prev or "size" in prev
                ):
                    prior = prev["usage"]
                else:
                    prior = prev
            already_peeled[uid] = dict(cu)
        delta = sub_usage(cu, prior)
        priced = price_child_usage(delta)
        summary = read_session_summary(d)
        title = summary.get("session_summary") or summary.get("generated_title")
        row = {
            "session_id": uid,
            "peeled": _int(delta.get("inputTokens")) > 0
            or _int(delta.get("modelCalls")) > 0,
            "usage": dict(cu),
            "turns": turns,
            "title": title,
            "agent_name": summary.get("agent_name"),
            "session_kind": summary.get("session_kind") or "subagent",
            **priced,
        }
        if row["peeled"]:
            add_usage(peel, delta)
        children.append(row)
    if _int(peel.get("inputTokens")) <= 0 and _int(peel.get("modelCalls")) <= 0:
        return src, {
            "peeled": False,
            "children": children,
            "usage": peel,
        }
    peeled = sub_usage(src, peel)
    return peeled, {
        "peeled": True,
        "children": children,
        "usage": peel,
        "official_usd": ticks_to_usd(_int(peel.get("costUsdTicks"))),
    }

def read_session_summary(session_dir: Optional[Path]) -> dict[str, Any]:
    from token_telemetry.session.discover import _read_session_summary

    if session_dir is None:
        return {}
    return _read_session_summary(Path(session_dir))


def extract_resume_from(raw_in: Any) -> Optional[str]:
    """spawn_subagent(resume_from=...) → previous session id."""
    if not isinstance(raw_in, dict):
        return None
    v = raw_in.get("resume_from") or raw_in.get("resumeFrom")
    s = str(v or "").strip().lower()
    if UUID_RE.fullmatch(s):
        return s
    return None


def tool_resume_from(t: Optional[dict[str, Any]]) -> Optional[str]:
    """resume_from on the tool dict or nested rawInput (builder keeps the field)."""
    if not isinstance(t, dict):
        return None
    rf = extract_resume_from(t.get("raw_input") or t.get("rawInput"))
    if rf:
        return rf
    s = str(t.get("resume_from") or "").strip().lower()
    if s and UUID_RE.fullmatch(s):
        return s
    return None


def summary_parent_session_id(summary: Optional[dict[str, Any]]) -> Optional[str]:
    if not isinstance(summary, dict):
        return None
    for key in ("parent_session_id", "parent_id"):
        v = summary.get(key)
        if isinstance(v, str) and v.strip():
            return v.strip().lower()
    info = summary.get("info")
    if isinstance(info, dict):
        for key in ("parent_session_id", "parent_id"):
            v = info.get(key)
            if isinstance(v, str) and v.strip():
                return v.strip().lower()
    return None


def root_subagent_id(
    uid: str,
    *,
    parent_dir: Optional[Path] = None,
    alias: Optional[dict[str, str]] = None,
) -> str:
    """Walk resume_from / summary parent_session_id to the original spawn."""
    cur = str(uid or "").strip().lower()
    if not cur:
        return cur
    seen: set[str] = set()
    while cur and cur not in seen:
        seen.add(cur)
        nxt = (alias or {}).get(cur)
        if nxt and nxt != cur:
            cur = nxt
            continue
        d = sibling_session_dir(parent_dir, cur)
        if d is None:
            break
        if session_kind_of(d) != "subagent_resume":
            break
        pid = summary_parent_session_id(read_session_summary(d))
        if not pid or pid == cur:
            break
        cur = pid
    return cur


def collect_resume_alias_from_round(round_: dict[str, Any]) -> dict[str, str]:
    """new_session_id → resume_from (previous session)."""
    out: dict[str, str] = {}
    for step in round_.get("model_steps") or []:
        if not isinstance(step, dict):
            continue
        for t in step.get("tools") or []:
            if not isinstance(t, dict):
                continue
            old = extract_resume_from(t.get("raw_input") or t.get("rawInput"))
            if not old:
                old = str(t.get("resume_from") or "").strip().lower() or None
                if old and not UUID_RE.fullmatch(old):
                    old = None
            if not old:
                continue
            new = str(t.get("subagent_id") or "").strip().lower()
            if new and UUID_RE.fullmatch(new) and new != old:
                out[new] = old
            for uid in t.get("subagent_ids") or []:
                s = str(uid or "").strip().lower()
                if s and UUID_RE.fullmatch(s) and s != old:
                    out.setdefault(s, old)
    return out


def latest_in_resume_chain(
    root: str,
    ids: list[str],
    *,
    parent_dir: Optional[Path] = None,
    alias: Optional[dict[str, str]] = None,
) -> str:
    """Prefer the newest resume session; fall back to the original spawn."""
    root_l = str(root or "").strip().lower()
    members: list[str] = []
    seen: set[str] = set()
    for uid in ids:
        s = str(uid or "").strip().lower()
        if not s or s in seen:
            continue
        if root_subagent_id(s, parent_dir=parent_dir, alias=alias) != root_l:
            continue
        seen.add(s)
        members.append(s)
    if not members:
        return root_l
    pointed: set[str] = set()
    amap = dict(alias or {})
    for m in members:
        d = sibling_session_dir(parent_dir, m)
        pid = amap.get(m)
        if not pid and d is not None:
            pid = summary_parent_session_id(read_session_summary(d))
        if pid:
            pointed.add(pid)
    leaves = [m for m in members if m not in pointed]
    return (leaves[-1] if leaves else members[-1])


def spawn_session_ids(t: Optional[dict[str, Any]]) -> list[str]:
    """Session UUIDs for a spawn_subagent tool. ACP toolCallId is not one."""
    if not isinstance(t, dict):
        return []
    seen: set[str] = set()
    ids: list[str] = []
    _add_uuid(ids, seen, t.get("subagent_id"))
    for x in t.get("subagent_ids") or []:
        _add_uuid(ids, seen, x)
    for uid in extract_ids_from_text(t.get("result_preview"), t.get("title")):
        _add_uuid(ids, seen, uid)
    return ids

