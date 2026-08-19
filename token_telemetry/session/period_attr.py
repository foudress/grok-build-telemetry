"""Per-turn Parts/Tools cats for period charts (our hierarchy, not API reasoning).

Each completed round (and recap/compact) is stamped with its own time so a
session that continues across days only bills the work produced in each bucket.
"""

from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Any, Optional

from token_telemetry.hierarchy import HierarchyBuilder
from token_telemetry.session.calc_cache import load_calc, save_calc


_lock = threading.Lock()
# path -> {mtime, size, events}
_attr_cache: dict[str, dict[str, Any]] = {}

_X_N = re.compile(r"\s*[x×]\s*\d+\s*$", re.I)


def _f(v: Any) -> float:
    try:
        return float(v or 0)
    except (TypeError, ValueError):
        return 0.0


def _i(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _ms_epoch(ms: Any) -> Optional[float]:
    if not isinstance(ms, (int, float)):
        return None
    v = float(ms)
    if v <= 0:
        return None
    if v > 1e11:
        return v / 1000.0
    return v


def _round_epoch(r: dict[str, Any]) -> Optional[float]:
    for k in ("completed_ms", "started_ms"):
        ep = _ms_epoch(r.get(k))
        if ep is not None:
            return ep
    return None


def _norm_tool(raw: Any) -> str:
    s = str(raw or "tool").strip()
    s = _X_N.sub("", s).strip()
    return s or "tool"


def _seg(k: str, label: str, usd: float, tok: float) -> Optional[dict[str, Any]]:
    if not (usd > 0 or tok > 0):
        return None
    key = k
    if k in ("tool", "toolreq"):
        key = f"{k}:{label}"
    return {
        "key": key,
        "k": k,
        "label": label,
        "usd": float(usd),
        "tok": float(tok),
    }


def parts_from_round(r: dict[str, Any]) -> list[dict[str, Any]]:
    """Detailed cats from our breakdown (thought / enc reasoning — not API)."""
    bd = r.get("breakdown") if isinstance(r.get("breakdown"), dict) else {}
    up = r.get("user_prompt") if isinstance(r.get("user_prompt"), dict) else {}
    segs: list[dict[str, Any]] = []
    pairs = [
        ("user", "user", _f(bd.get("user_in_usd") or up.get("cost_in_usd")),
         _f(bd.get("user_in_tokens") or up.get("tokens_in") or up.get("uncached_est"))),
        ("harness", "harness", _f(bd.get("harness_in_usd")), _f(bd.get("harness_in_tokens"))),
        ("thought", "thought", _f(bd.get("llm_thought_summary_usd")),
         _f(bd.get("llm_thought_summary_tokens"))),
        ("reasoning", "reasoning",
         _f(bd.get("llm_reasoning_encrypted_usd") or bd.get("llm_reasoning_usd")),
         _f(bd.get("llm_reasoning_encrypted_tokens") or bd.get("llm_reasoning_tokens"))),
        ("toolreq", "tool req", _f(bd.get("llm_out_to_harness_usd")),
         _f(bd.get("llm_out_to_harness_tokens"))),
        ("message", "message", _f(bd.get("llm_out_to_user_usd")),
         _f(bd.get("llm_out_to_user_tokens"))),
        ("cached", "Cached",
         _f(bd.get("cached_usd") or r.get("cost_cached_usd")),
         _f(bd.get("cached_tokens") or r.get("cached_read_tokens"))),
    ]
    for k, lab, usd, tok in pairs:
        s = _seg(k, lab, usd, tok)
        if s:
            segs.append(s)
    if not any(s["k"] == "thought" for s in segs):
        th_u = th_t = 0.0
        for step in r.get("model_steps") or []:
            if not isinstance(step, dict):
                continue
            for ch in step.get("children") or []:
                if not isinstance(ch, dict) or ch.get("kind") != "phase_llm":
                    continue
                for sub in ch.get("children") or []:
                    if isinstance(sub, dict) and sub.get("kind") == "thought":
                        th_u += _f(sub.get("cost_out_usd"))
                        th_t += _f(sub.get("tokens_out") or sub.get("tokenizer_tokens"))
        s = _seg("thought", "thought", th_u, th_t)
        if s:
            segs.append(s)
    return segs


def tools_from_round(r: dict[str, Any]) -> list[dict[str, Any]]:
    """Drill-style cats from priced model_steps."""
    acc: dict[str, dict[str, Any]] = {}

    def add(k: str, label: str, usd: float, tok: float) -> None:
        s = _seg(k, label, usd, tok)
        if not s:
            return
        prev = acc.get(s["key"])
        if prev:
            prev["usd"] += s["usd"]
            prev["tok"] += s["tok"]
        else:
            acc[s["key"]] = s

    steps = r.get("model_steps") if isinstance(r.get("model_steps"), list) else []
    for si, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        if si == 0:
            up = r.get("user_prompt") if isinstance(r.get("user_prompt"), dict) else {}
            add("user", "user", _f(up.get("cost_in_usd")),
                _f(up.get("tokens_in") or up.get("uncached_est")))
        add("cached", "Cached",
            _f(step.get("cost_cached_usd") or (step.get("estimate") or {}).get("cost_cached_usd")),
            _f(step.get("cached_read_tokens") or (step.get("estimate") or {}).get("cached_read_tokens")))
        for ch in step.get("children") or []:
            if not isinstance(ch, dict):
                continue
            kind = ch.get("kind")
            if kind == "phase_harness":
                for sub in ch.get("children") or []:
                    if not isinstance(sub, dict):
                        continue
                    if sub.get("kind") in ("hook", "late_context"):
                        continue
                    usd = _f(sub.get("cost_in_usd"))
                    tok = _f(sub.get("tokens_in") or sub.get("context_delta"))
                    if sub.get("kind") == "llm_to_in":
                        add("llm_out_in", "LLM Out→In", usd, tok)
                    else:
                        add("tool", _norm_tool(sub.get("name") or sub.get("title")), usd, tok)
            elif kind == "phase_llm":
                for sub in ch.get("children") or []:
                    if not isinstance(sub, dict):
                        continue
                    usd = _f(sub.get("cost_out_usd"))
                    tok = _f(sub.get("tokens_out") or sub.get("tokenizer_tokens") or sub.get("tokens"))
                    sk = sub.get("kind")
                    if sk == "thought":
                        add("thought", "thought", usd, tok)
                    elif sk == "reasoning":
                        add("reasoning", "reasoning", usd, tok)
                    elif sk == "message":
                        add("message", "message", usd, tok)
                    elif sk == "tool_request":
                        add("toolreq", _norm_tool(sub.get("name") or sub.get("title")), usd, tok)
    return list(acc.values())


def _recap_io_segs(c: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for k, lab, usd, tok in (
        ("in", "In", _f(c.get("prompt_in_usd")), _f(c.get("prompt_tokens"))),
        ("cached", "Cached", _f(c.get("pre_read_cached_usd")),
         _f(c.get("context_tokens") or c.get("context_cached_tokens"))),
        ("out", "Out", _f(c.get("out_usd")), _f(c.get("out_tokens"))),
    ):
        s = _seg(k, lab, usd, tok)
        if s:
            out.append(s)
    return out


def _recap_parts_segs(c: dict[str, Any]) -> list[dict[str, Any]]:
    usd = _f(c.get("cost_usd")) or (
        _f(c.get("prompt_in_usd")) + _f(c.get("pre_read_cached_usd")) + _f(c.get("out_usd"))
    )
    tok = _f(c.get("prompt_tokens")) + _f(c.get("context_tokens") or c.get("context_cached_tokens")) + _f(c.get("out_tokens"))
    s = _seg("recap", "recap", usd, tok)
    return [s] if s else []


def _compact_io_segs(c: dict[str, Any]) -> list[dict[str, Any]]:
    miss = bool(c.get("pre_read_cache_miss"))
    pre_unc_u = _f(c.get("pre_read_uncached_usd") or c.get("pre_read_usd"))
    pre_c_u = _f(c.get("pre_read_cached_usd") or c.get("pre_read_usd"))
    pre_unc_t = _f(c.get("pre_read_uncached_tokens") or c.get("pre_read_tokens") or c.get("tokens_before"))
    pre_c_t = _f(c.get("pre_read_cached_tokens") or c.get("pre_read_tokens") or c.get("tokens_before"))
    out: list[dict[str, Any]] = []
    if miss or (pre_unc_t > 0 and not (pre_c_t > 0)):
        s = _seg("in", "In", pre_unc_u, pre_unc_t)
    else:
        s = _seg("cached", "Cached", pre_c_u, pre_c_t)
    if s:
        out.append(s)
    s2 = _seg("out", "Out", _f(c.get("out_usd")), _f(c.get("out_tokens")))
    if s2:
        out.append(s2)
    return out


def _compact_parts_segs(c: dict[str, Any]) -> list[dict[str, Any]]:
    usd = _f(c.get("cost_usd"))
    tok = _f(c.get("tokens_before")) + _f(c.get("out_tokens"))
    s = _seg("compact", "compact", usd, tok)
    return [s] if s else _compact_io_segs(c)


def _push_side(events: list[dict[str, Any]], ev: dict[str, Any], *, recap: bool) -> None:
    ep = _ms_epoch(ev.get("agent_ms"))
    if ep is None:
        return
    events.append({
        "epoch": ep,
        "parts": _recap_parts_segs(ev) if recap else _compact_parts_segs(ev),
        "tools": _recap_parts_segs(ev) if recap else _compact_parts_segs(ev),
    })


def extract_session_events(session_dir: Path) -> list[dict[str, Any]]:
    """Replay updates through HierarchyBuilder; one event per round / recap / compact."""
    path = session_dir / "updates.jsonl"
    if not path.is_file():
        return []
    hb = HierarchyBuilder(max_rounds=4000)
    hb.set_session_dir(session_dir)
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(obj, dict):
                    try:
                        hb.feed_raw(obj)
                    except Exception:
                        continue
    except OSError:
        return []

    events: list[dict[str, Any]] = []
    for r in hb.rounds:
        if not isinstance(r, dict):
            continue
        ep = _round_epoch(r)
        if ep is None:
            continue
        events.append({
            "epoch": ep,
            "parts": parts_from_round(r),
            "tools": tools_from_round(r),
        })
        for rec in r.get("recaps_after") or []:
            if isinstance(rec, dict) and rec.get("kind") == "session_recap":
                _push_side(events, rec, recap=True)
        ca = r.get("compact_after")
        if isinstance(ca, dict) and ca.get("kind") == "compaction":
            _push_side(events, ca, recap=False)
    return events


def cached_attr_events(session_dir: Path) -> list[dict[str, Any]]:
    path = session_dir / "updates.jsonl"
    key = str(path)
    try:
        st = path.stat()
        mtime, size = st.st_mtime, st.st_size
    except OSError:
        return []
    with _lock:
        hit = _attr_cache.get(key)
        if hit and hit.get("mtime") == mtime and hit.get("size") == size:
            return hit.get("events") or []
    blob = load_calc(session_dir)
    if blob is not None and "events" in blob:
        events = blob.get("events") or []
        with _lock:
            _attr_cache[key] = {"mtime": mtime, "size": size, "events": events}
        return events
    try:
        events = extract_session_events(session_dir)
    except Exception:
        events = []
    save_calc(session_dir, events=events)
    with _lock:
        _attr_cache[key] = {"mtime": mtime, "size": size, "events": events}
    return events


def clear_attr_mem() -> None:
    with _lock:
        _attr_cache.clear()
