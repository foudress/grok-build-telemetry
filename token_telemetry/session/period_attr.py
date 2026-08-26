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


def _first_present(d: dict[str, Any], *keys: str) -> Optional[float]:
    if not isinstance(d, dict):
        return None
    for k in keys:
        if k in d and d[k] is not None:
            return _f(d[k])
    return None


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
    thought_usd = _f(bd.get("llm_thought_summary_usd"))
    thought_tok = _f(bd.get("llm_thought_summary_tokens"))
    toolreq_usd = _f(bd.get("llm_out_to_harness_usd"))
    toolreq_tok = _f(bd.get("llm_out_to_harness_tokens"))
    msg_usd = _f(bd.get("llm_out_to_user_usd"))
    msg_tok = _f(bd.get("llm_out_to_user_tokens"))
    reason_usd = _first_present(bd, "llm_reasoning_usd")
    reason_tok = _first_present(bd, "llm_reasoning_tokens")
    if reason_usd is None and reason_tok is None:
        reason_usd = _first_present(bd, "llm_reasoning_encrypted_usd")
        reason_tok = _first_present(bd, "llm_reasoning_encrypted_tokens")
    reason_usd = 0.0 if reason_usd is None else reason_usd
    reason_tok = 0.0 if reason_tok is None else reason_tok
    # Compact Out → User in Parts (never a Harness sub-cat).
    # Between-rounds (user.compact_out): already excluded from harness_in / Call In.
    # Mid-round (attribution=harness): still inside harness_in — peel only that.
    co_usd = 0.0
    co_tok = 0.0
    co = up.get("compact_out") if isinstance(up.get("compact_out"), dict) else {}
    co_user_usd = _f(co.get("cost_in_usd"))
    co_user_tok = _f(co.get("tokens_in"))
    co_mid_usd = 0.0
    co_mid_tok = 0.0
    for step in r.get("model_steps") or []:
        if not isinstance(step, dict):
            continue
        for ch in step.get("children") or []:
            if not isinstance(ch, dict) or ch.get("kind") != "phase_harness":
                continue
            for sub in ch.get("children") or []:
                if not isinstance(sub, dict) or sub.get("kind") != "compact_out_in":
                    continue
                if str(sub.get("attribution") or "") == "user":
                    continue
                co_mid_usd += _f(sub.get("cost_in_usd"))
                co_mid_tok += _f(sub.get("tokens_in") or sub.get("context_delta"))
    co_usd = co_user_usd + co_mid_usd
    co_tok = co_user_tok + co_mid_tok
    h_usd = max(0.0, _f(bd.get("harness_in_usd")) - co_mid_usd)
    h_tok = max(0.0, _f(bd.get("harness_in_tokens")) - co_mid_tok)
    # Parts User = same fold as Tools (prompt + LLM Answer + Compact Out),
    # plus mid-round Compact Out. Prefer Tools-aligned fields over billed user_in
    # alone (prompt TokZ vs raw tokens_in drifted the Session Parts bar).
    prompt_tok = _f(up.get("prompt_tokens_in"))
    prompt_usd = _f(up.get("prompt_cost_in_usd"))
    if not (prompt_tok > 0 or prompt_usd > 0):
        prompt_tok = _f(bd.get("user_in_tokens") or up.get("tokens_in") or up.get("uncached_est"))
        prompt_usd = _f(bd.get("user_in_usd") or up.get("cost_in_usd"))
    prev = up.get("prev_llm_answer") if isinstance(up.get("prev_llm_answer"), dict) else {}
    ans_tok = _f(prev.get("tokens_in"))
    ans_usd = _f(prev.get("cost_in_usd"))
    user_usd = prompt_usd + ans_usd + co_usd
    user_tok = prompt_tok + ans_tok + co_tok
    if not (user_usd > 0 or user_tok > 0):
        user_usd = _f(bd.get("user_in_usd") or up.get("cost_in_usd")) + co_usd
        user_tok = _f(bd.get("user_in_tokens") or up.get("tokens_in") or up.get("uncached_est")) + co_tok
    pairs = [
        ("user", "user", user_usd, user_tok),
        ("cache_miss", "cache miss",
         _f(bd.get("cache_miss_in_usd") or r.get("cache_miss_in_usd")),
         _f(bd.get("cache_miss_in_tokens") or r.get("cache_miss_in_tokens"))),
        ("harness", "harness", h_usd, h_tok),
        ("thought", "thought", thought_usd, thought_tok),
        ("reasoning", "reasoning", reason_usd, reason_tok),
        ("toolreq", "tool req", toolreq_usd, toolreq_tok),
        ("message", "message", msg_usd, msg_tok),
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
    """Drill-style cats from priced model_steps (+ round-level cache miss)."""
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

    bd = r.get("breakdown") if isinstance(r.get("breakdown"), dict) else {}
    # Same round-level miss cat as parts / session tools (not buried in tools).
    add(
        "cache_miss",
        "cache miss",
        _f(bd.get("cache_miss_in_usd") or r.get("cache_miss_in_usd")),
        _f(bd.get("cache_miss_in_tokens") or r.get("cache_miss_in_tokens")),
    )

    steps = r.get("model_steps") if isinstance(r.get("model_steps"), list) else []
    for si, step in enumerate(steps):
        if not isinstance(step, dict):
            continue
        if si == 0:
            up = r.get("user_prompt") if isinstance(r.get("user_prompt"), dict) else {}
            # User Tools breakdown: prompt / LLM Answer / Compact Out (not harness).
            prompt_tok = _f(up.get("prompt_tokens_in"))
            prompt_usd = _f(up.get("prompt_cost_in_usd"))
            if not (prompt_tok > 0 or prompt_usd > 0):
                prompt_tok = _f(up.get("tokens_in") or up.get("uncached_est"))
                prompt_usd = _f(up.get("cost_in_usd"))
            prev = up.get("prev_llm_answer") if isinstance(up.get("prev_llm_answer"), dict) else {}
            ans_tok = _f(prev.get("tokens_in"))
            ans_usd = _f(prev.get("cost_in_usd"))
            if ans_tok > 0 or ans_usd > 0:
                add("llm_answer", "LLM Answer", ans_usd, ans_tok)
            co = up.get("compact_out") if isinstance(up.get("compact_out"), dict) else {}
            co_tok = _f(co.get("tokens_in"))
            co_usd = _f(co.get("cost_in_usd"))
            if co_tok > 0 or co_usd > 0:
                add("compact_out", "Compact Out", co_usd, co_tok)
            if prompt_tok > 0 or prompt_usd > 0:
                add("prompt", "prompt", prompt_usd, prompt_tok)
            elif not (ans_tok > 0 or co_tok > 0):
                add("user", "user", _f(up.get("cost_in_usd")),
                    _f(up.get("tokens_in") or up.get("uncached_est")))
        # Cached: official round only — Σ call prefixes ≠ billed when cache miss.
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
                    elif sub.get("kind") == "compact_out_in":
                        # Between-rounds: owned by User.compact_out. Mid-round: own cat.
                        if str(sub.get("attribution") or "") == "user":
                            continue
                        add("compact_out", "Compact Out", usd, tok)
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
    add(
        "cached",
        "Cached",
        _f(bd.get("cached_usd") or r.get("cost_cached_usd")),
        _f(bd.get("cached_tokens") or r.get("cached_read_tokens")),
    )
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
        # I/O sides: period I/O mode must include recap/compact In/Cached/Out
        # (turn_completed alone omits them → totals drift vs Parts/Tools).
        "io": _recap_io_segs(ev) if recap else _compact_io_segs(ev),
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

    # Same path as the session UI: enrich chat_history stamps then reprice so
    # cache_miss_in_* is populated (raw hb.rounds often still have miss=0).
    try:
        rounds = hb.snapshot_rounds(include_open=False)
    except Exception:
        rounds = list(hb.rounds)

    events: list[dict[str, Any]] = []
    for r in rounds:
        if not isinstance(r, dict):
            continue
        ep = _round_epoch(r)
        if ep is None:
            continue
        tps = r.get("gen_tokens_per_sec")
        calls: list[dict[str, Any]] = []
        for s in r.get("model_steps") or []:
            if not isinstance(s, dict) or s.get("gen_tokens_per_sec") is None:
                continue
            c_ep = _ms_epoch(s.get("prompt_start_ms") or s.get("started_ms") or s.get("stream_start_ms"))
            calls.append({
                "i": s.get("index"),
                "v": s.get("gen_tokens_per_sec"),
                "epoch": c_ep if c_ep is not None else ep,
                "out": s.get("gen_out_tokens"),
                "gen_ms": s.get("gen_ms"),
            })
        events.append({
            "epoch": ep,
            "parts": parts_from_round(r),
            "tools": tools_from_round(r),
            "tps": tps,
            "round": r.get("index"),
            "gen_ms": r.get("gen_ms"),
            "gen_out_tokens": r.get("gen_out_tokens"),
            "calls": calls,
        })
        for rec in r.get("recaps_after") or []:
            if isinstance(rec, dict) and rec.get("kind") == "session_recap":
                _push_side(events, rec, recap=True)
        seen_ms: set[Any] = set()
        ca = r.get("compact_after")
        if isinstance(ca, dict) and ca.get("kind") == "compaction":
            _push_side(events, ca, recap=False)
            if ca.get("agent_ms") is not None:
                seen_ms.add(ca.get("agent_ms"))
        for s in r.get("model_steps") or []:
            if not isinstance(s, dict):
                continue
            for c in s.get("compacts_after") or []:
                if not isinstance(c, dict) or c.get("kind") != "compaction":
                    continue
                ms = c.get("agent_ms")
                if ms is not None and ms in seen_ms:
                    continue
                _push_side(events, c, recap=False)
                if ms is not None:
                    seen_ms.add(ms)
    return events


def cached_attr_events(session_dir: Path) -> list[dict[str, Any]]:
    from token_telemetry.session.calc_cache import code_sig

    path = session_dir / "updates.jsonl"
    key = str(path)
    try:
        st = path.stat()
        mtime, size = st.st_mtime, st.st_size
    except OSError:
        return []
    sig = code_sig()
    with _lock:
        hit = _attr_cache.get(key)
        if (
            hit
            and hit.get("mtime") == mtime
            and hit.get("size") == size
            and hit.get("code") == sig
        ):
            return hit.get("events") or []
    blob = load_calc(session_dir)
    if blob is not None and "events" in blob:
        events = blob.get("events") or []
        with _lock:
            _attr_cache[key] = {
                "mtime": mtime, "size": size, "code": sig, "events": events,
            }
        return events
    try:
        events = extract_session_events(session_dir)
    except Exception:
        events = []
    save_calc(session_dir, events=events)
    with _lock:
        _attr_cache[key] = {
            "mtime": mtime, "size": size, "code": sig, "events": events,
        }
    return events


def clear_attr_mem() -> None:
    with _lock:
        _attr_cache.clear()
