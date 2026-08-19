"""Link parent sessions to sub-agent sessions and peel their official usage.

Grok Build writes each spawn_subagent as its own session dir
(``session_kind: subagent``). The parent's ``turn_completed.usage`` for the
round that waited on ``get_command_or_subagent_output`` includes the
children's full API bill (input / cache / output / modelCalls / ticks).

That extra mass is then pro-rated across the *parent* LLM calls and
destroys per-call Input / cache / context math.

Peel children out of the parent official dict before reconstruct. Keep the
unpeeled bill for the session-level "general" total.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Iterable, Optional

from token_telemetry.pricing.rates import estimate_from_usage, ticks_to_usd

UUID_RE = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
    re.IGNORECASE,
)
SUBAGENT_ID_RE = re.compile(
    r"subagent_id:\s*(" + UUID_RE.pattern + r")",
    re.IGNORECASE,
)
TASK_LINE_RE = re.compile(
    r"(?:---\s*)?Task\s+(" + UUID_RE.pattern + r")",
    re.IGNORECASE,
)
TYPE_RE = re.compile(r"(?:^|\n)\s*type:\s*(\S+)", re.IGNORECASE)
DESC_RE = re.compile(r"(?:^|\n)\s*description:\s*(.+)", re.IGNORECASE)

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


def read_session_summary(session_dir: Optional[Path]) -> dict[str, Any]:
    from token_telemetry.session.discover import _read_session_summary

    if session_dir is None:
        return {}
    return _read_session_summary(Path(session_dir))


def session_kind_of(session_dir: Optional[Path]) -> Optional[str]:
    kind = read_session_summary(session_dir).get("session_kind")
    if isinstance(kind, str) and kind.strip():
        return kind.strip().lower()
    return None


def is_subagent_session(session_dir: Optional[Path]) -> bool:
    return session_kind_of(session_dir) == "subagent"


def parse_subagent_meta(text: Any) -> dict[str, Any]:
    """Pull id / type / description from a spawn or get_command result body."""
    if not isinstance(text, str) or not text.strip():
        return {}
    out: dict[str, Any] = {}
    m = SUBAGENT_ID_RE.search(text)
    if m:
        out["subagent_id"] = m.group(1).lower()
    else:
        tm = TASK_LINE_RE.search(text)
        if tm:
            out["subagent_id"] = tm.group(1).lower()
    t = TYPE_RE.search(text)
    if t:
        out["subagent_type"] = t.group(1).strip()
    d = DESC_RE.search(text)
    if d:
        desc = d.group(1).strip()
        if desc:
            out["subagent_description"] = desc[:120]
    return out


def extract_task_ids(raw_in: Any) -> list[str]:
    if not isinstance(raw_in, dict):
        return []
    ids = raw_in.get("task_ids") or raw_in.get("taskIds")
    if not isinstance(ids, list):
        return []
    out: list[str] = []
    for x in ids:
        s = str(x or "").strip().lower()
        if UUID_RE.fullmatch(s):
            out.append(s)
    return out


def extract_ids_from_text(*parts: Any) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for part in parts:
        if part is None:
            continue
        if not isinstance(part, str):
            try:
                part = json.dumps(part, ensure_ascii=False)
            except (TypeError, ValueError):
                part = str(part)
        for m in SUBAGENT_ID_RE.finditer(part):
            uid = m.group(1).lower()
            if uid not in seen:
                seen.add(uid)
                found.append(uid)
        for m in TASK_LINE_RE.finditer(part):
            uid = m.group(1).lower()
            if uid not in seen:
                seen.add(uid)
                found.append(uid)
    return found


def _int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


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
    if official is not None and official > 0 and est_tot > 0:
        scale = official / est_tot
        cin *= scale
        ccache *= scale
        cout *= scale
        est_tot = official
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
        "official_usd": official if official is not None else est_tot,
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


def load_session_official_usage(
    session_dir: Optional[Path],
    cache: Optional[dict[str, Any]] = None,
    *,
    cache_key: Optional[str] = None,
) -> dict[str, int]:
    """Sum every turn_completed.usage in a session's updates.jsonl.

    ``cache`` (if given) is keyed by ``str(session_dir)`` or ``cache_key``
    (typically the session uid). Entries are
    ``{"usage": acc, "mtime": float, "size": int}``. The file is re-read
    only when mtime or size changed. Missing files are not stored, so a
    later-created ``updates.jsonl`` is not stuck at an empty peel.
    """
    acc = empty_usage()
    if session_dir is None:
        return acc
    root = Path(session_dir)
    p = root / "updates.jsonl"
    if not p.is_file():
        return acc
    stat = _updates_jsonl_stat(p)
    if stat is None:
        return acc
    mtime, size = stat
    key = _usage_cache_key(root, cache_key)
    if cache is not None:
        hit = _cache_usage_hit(cache.get(key), mtime, size)
        if hit is not None:
            return hit
    try:
        raw = p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return acc
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
        add_usage(acc, upd.get("usage") or {})
    if cache is not None:
        cache[key] = {"usage": acc, "mtime": mtime, "size": size}
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
        cu = load_session_official_usage(d, cache=cache, cache_key=str(uid))
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
        priced = price_child_usage(cu)
        if _int(delta.get("inputTokens")) <= 0 and _int(delta.get("modelCalls")) <= 0:
            children.append(
                {
                    "session_id": uid,
                    "peeled": False,
                    "usage": dict(cu),
                    **priced,
                }
            )
            continue
        add_usage(peel, delta)
        summary = read_session_summary(d)
        children.append(
            {
                "session_id": uid,
                "peeled": True,
                "usage": dict(cu),
                "title": summary.get("session_summary") or summary.get("generated_title"),
                "agent_name": summary.get("agent_name"),
                "session_kind": summary.get("session_kind") or "subagent",
                **priced,
            }
        )
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


def attach_subagents_after_steps(round_: dict[str, Any], peel_meta: dict[str, Any]) -> None:
    """
    Place a Sub Agent N card after the LLM call that finished get_command
    (or spawn, if get never ran). One card per child, first-seen order.
    """
    children = [
        c
        for c in (peel_meta.get("children") or [])
        if isinstance(c, dict) and c.get("session_id")
    ]
    if not children:
        return
    by_id = {str(c["session_id"]).lower(): c for c in children}
    placed: set[str] = set()
    n = 0
    steps = [s for s in (round_.get("model_steps") or []) if isinstance(s, dict)]
    for step in steps:
        after: list[dict[str, Any]] = []
        for t in step.get("tools") or []:
            if not isinstance(t, dict):
                continue
            name = str(t.get("name") or "")
            ids: list[str] = []
            if t.get("subagent_id"):
                ids.append(str(t["subagent_id"]).lower())
            for uid in t.get("subagent_ids") or []:
                ids.append(str(uid).lower())
            if name == "get_command_or_subagent_output":
                ids.extend(
                    extract_ids_from_text(t.get("result_preview"), t.get("title"))
                )
            # Prefer the wait tool; spawn-only cards wait until get if present
            if name != "get_command_or_subagent_output":
                continue
            for uid in ids:
                if uid in placed or uid not in by_id:
                    continue
                n += 1
                placed.add(uid)
                card = dict(by_id[uid])
                card["n"] = n
                card["label"] = card.get("title") or f"Sub Agent {n}"
                after.append(card)
        if after:
            step["subagents_after"] = after
    # Spawn finished but get never completed (still running / killed)
    leftover = [c for c in children if str(c["session_id"]).lower() not in placed]
    if leftover and steps:
        after = list(steps[-1].get("subagents_after") or [])
        for c in leftover:
            n += 1
            card = dict(c)
            card["n"] = n
            card["label"] = card.get("title") or f"Sub Agent {n}"
            after.append(card)
        steps[-1]["subagents_after"] = after
