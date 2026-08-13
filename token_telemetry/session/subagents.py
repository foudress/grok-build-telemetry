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
    if session_dir is None:
        return {}
    p = Path(session_dir) / "summary.json"
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}


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


def price_child_usage(usage: Optional[dict[str, Any]]) -> dict[str, Any]:
    """In / Cached / Out tokens + list-rate $ for a child session bill."""
    u = usage if isinstance(usage, dict) else {}
    inn = _int(u.get("inputTokens") or u.get("input_tokens"))
    cache = _int(u.get("cachedReadTokens") or u.get("cached_read_tokens"))
    out = _int(u.get("outputTokens") or u.get("output_tokens"))
    if cache > inn:
        cache = inn
    unc = max(0, inn - cache)
    ticks = _int(u.get("costUsdTicks") or u.get("cost_usd_ticks"))
    official = ticks_to_usd(ticks) if ticks else None
    peak = max(inn, _int(u.get("totalTokens") or u.get("total_tokens")), 0)
    est = estimate_from_usage(u, peak_context_tokens=peak or None)
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
    return {
        "tokens_in": unc,
        "tokens_cached": cache,
        "tokens_out": out,
        "cost_in_usd": cin,
        "cost_cached_usd": ccache,
        "cost_out_usd": cout,
        "estimate_usd": est_tot,
        "official_usd": official if official is not None else est_tot,
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


def load_session_official_usage(session_dir: Optional[Path]) -> dict[str, int]:
    """Sum every turn_completed.usage in a session's updates.jsonl."""
    acc = empty_usage()
    if session_dir is None:
        return acc
    p = Path(session_dir) / "updates.jsonl"
    if not p.is_file():
        return acc
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
    return acc


def sibling_session_dir(parent_dir: Optional[Path], session_id: str) -> Optional[Path]:
    if parent_dir is None or not session_id:
        return None
    cand = Path(parent_dir).parent / session_id
    if cand.is_dir():
        return cand
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


def peel_round_usage(
    usage: Optional[dict[str, Any]],
    *,
    parent_dir: Optional[Path],
    child_ids: Iterable[str],
    cache: Optional[dict[str, dict[str, int]]] = None,
) -> tuple[dict[str, Any], dict[str, Any]]:
    """
    Return (peeled_usage, peel_meta).

    ``peel_meta`` lists each child and the subtracted counters. Empty peel
    leaves usage unchanged.
    """
    src = dict(usage) if isinstance(usage, dict) else {}
    peel = empty_usage()
    children: list[dict[str, Any]] = []
    for uid in child_ids:
        if cache is not None and uid in cache:
            cu = cache[uid]
        else:
            d = sibling_session_dir(parent_dir, uid)
            cu = load_session_official_usage(d)
            if cache is not None:
                cache[uid] = cu
        priced = price_child_usage(cu)
        if _int(cu.get("inputTokens")) <= 0 and _int(cu.get("modelCalls")) <= 0:
            children.append(
                {
                    "session_id": uid,
                    "peeled": False,
                    "usage": dict(cu),
                    **priced,
                }
            )
            continue
        add_usage(peel, cu)
        summary = read_session_summary(sibling_session_dir(parent_dir, uid))
        children.append(
            {
                "session_id": uid,
                "peeled": True,
                "usage": dict(cu),
                "title": summary.get("generated_title") or summary.get("session_summary"),
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
