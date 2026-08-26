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


SUBAGENT_KINDS = frozenset({"subagent", "subagent_resume"})


def is_subagent_kind(kind: Any) -> bool:
    if not isinstance(kind, str):
        return False
    return kind.strip().lower() in SUBAGENT_KINDS


def is_subagent_session(session_dir: Optional[Path]) -> bool:
    return is_subagent_kind(session_kind_of(session_dir))


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


def _add_uuid(ids: list[str], seen: set[str], uid: Any) -> None:
    s = str(uid or "").strip().lower()
    if not s or not UUID_RE.fullmatch(s) or s in seen:
        return
    seen.add(s)
    ids.append(s)


def merge_tool_subagent_ids(tool: Optional[dict[str, Any]], *parts: Any) -> None:
    """Stamp full session UUIDs onto a spawn/wait tool (never ACP call-… ids)."""
    if not isinstance(tool, dict):
        return
    seen: set[str] = set()
    ids: list[str] = []
    _add_uuid(ids, seen, tool.get("subagent_id"))
    for x in tool.get("subagent_ids") or []:
        _add_uuid(ids, seen, x)
    for part in parts:
        if isinstance(part, (list, tuple)):
            for x in part:
                _add_uuid(ids, seen, x)
        else:
            _add_uuid(ids, seen, part)
    for uid in extract_ids_from_text(*parts):
        _add_uuid(ids, seen, uid)
    if not ids:
        return
    tool["subagent_ids"] = ids
    if not tool.get("subagent_id"):
        tool["subagent_id"] = ids[0]


def apply_history_subagent_ids(
    tool: Optional[dict[str, Any]], hit: Optional[dict[str, Any]]
) -> None:
    """chat_history tool_result.content has the full spawn UUID; preview does not."""
    if not isinstance(tool, dict) or not isinstance(hit, dict):
        return
    merge_tool_subagent_ids(
        tool,
        hit.get("subagent_id"),
        hit.get("subagent_ids"),
        hit.get("body"),
        hit.get("preview"),
    )


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


def _extract_one(
    hb: Any,
    base: dict[str, Any],
    uid: str,
    *,
    parent_dir: Optional[Path],
    alias: dict[str, str],
    index: int,
) -> Optional[dict[str, Any]]:
    """One child-round extract for the Nth parent get (0-based index)."""
    root, n = _ordinal_n(hb, uid, parent_dir=parent_dir, alias=alias)
    usable = _child_turn_list(hb, base, uid, root)
    if usable:
        if index < 0 or index >= len(usable):
            return None
        tu = usable[index]
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
