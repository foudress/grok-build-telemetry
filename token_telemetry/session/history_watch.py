"""Live chat_history.jsonl watcher — detect prefix mutations vs append.

Baselines are taken the first time a file is seen after the watcher starts.
Past rewrites (before start, or while the process was down) are invisible.
"""

from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional

from token_telemetry.session.discover import (
    SESSIONS_ROOT,
    _read_session_summary,
    format_age,
    list_session_dirs,
)


PREVIEW_LEN = 160
MAX_EVENTS = 400
MAX_CHANGES_PER_EVENT = 48
MAX_SESSIONS = 80

# Same phrases as hierarchy bootstrap — do not import from that freeze zone.
_COMPACT_GLUE = (
    "this session is being continued",
    "ran out of context",
    "conversation is summarized below",
    "previous conversation that ran out",
    "compaction/segment_",
)


def content_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for p in content:
            if isinstance(p, str):
                parts.append(p)
            elif isinstance(p, dict):
                t = p.get("text")
                if t is None:
                    t = p.get("content")
                if isinstance(t, str):
                    parts.append(t)
                else:
                    try:
                        parts.append(json.dumps(p, ensure_ascii=False, sort_keys=True))
                    except (TypeError, ValueError):
                        parts.append(str(p))
            else:
                parts.append(str(p))
        return "\n".join(parts)
    try:
        return json.dumps(content, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        return str(content)


def _preview(text: str, n: int = PREVIEW_LEN) -> str:
    one = " ".join((text or "").split())
    if len(one) <= n:
        return one
    return one[: n - 1] + "…"


def is_compact_signal(text: Any, synthetic_reason: Any = None) -> bool:
    syn = str(synthetic_reason or "").lower()
    if "compact" in syn:
        return True
    low = str(text or "").lower()
    if not low.strip():
        return False
    return any(m in low for m in _COMPACT_GLUE)


def rec_type(obj: dict[str, Any]) -> str:
    return str(obj.get("type") or obj.get("role") or "?")


def rec_identity(obj: dict[str, Any]) -> Optional[str]:
    t = rec_type(obj)
    if t == "reasoning" and obj.get("id"):
        return f"reasoning:{obj['id']}"
    tid = obj.get("tool_call_id") or obj.get("toolCallId")
    if t == "tool_result" and tid:
        return f"tool_result:{tid}"
    if t == "user" and obj.get("prompt_index") is not None:
        return f"user:{obj['prompt_index']}"
    if t == "system":
        return "system"
    if t == "assistant":
        calls = obj.get("tool_calls")
        if isinstance(calls, list):
            ids = [
                str(c.get("id"))
                for c in calls
                if isinstance(c, dict) and c.get("id")
            ]
            if ids:
                return "assistant:" + ",".join(ids)
    return None


def rec_payload(obj: dict[str, Any]) -> dict[str, Any]:
    calls = obj.get("tool_calls")
    slim_calls: Any = None
    if isinstance(calls, list):
        slim_calls = []
        for c in calls:
            if not isinstance(c, dict):
                slim_calls.append(str(c))
                continue
            slim_calls.append(
                {
                    "id": c.get("id"),
                    "name": c.get("name"),
                    "arguments": c.get("arguments"),
                }
            )
    enc = obj.get("encrypted_content")
    return {
        "type": rec_type(obj),
        "content": content_text(obj.get("content")),
        "synthetic_reason": obj.get("synthetic_reason"),
        "prompt_index": obj.get("prompt_index"),
        "tool_call_id": obj.get("tool_call_id") or obj.get("toolCallId"),
        "summary": obj.get("summary"),
        "enc_len": len(enc) if isinstance(enc, str) else 0,
        "status": obj.get("status"),
        "tool_calls": slim_calls,
    }


def rec_hash(obj: dict[str, Any]) -> str:
    raw = json.dumps(rec_payload(obj), ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def fingerprint(obj: dict[str, Any], index: int) -> dict[str, Any]:
    text = content_text(obj.get("content"))
    enc = obj.get("encrypted_content")
    enc_len = len(enc) if isinstance(enc, str) else 0
    chars = len(text) + enc_len
    if not text and obj.get("summary"):
        text = content_text(obj.get("summary"))
    syn = obj.get("synthetic_reason")
    return {
        "index": index,
        "type": rec_type(obj),
        "key": rec_identity(obj),
        "hash": rec_hash(obj),
        "chars": chars,
        "preview": _preview(text),
        "prompt_index": obj.get("prompt_index"),
        "compact": is_compact_signal(text, syn),
        "synthetic_reason": syn if isinstance(syn, str) else None,
    }


def load_history_records(path: Path) -> Optional[list[dict[str, Any]]]:
    """Parse JSONL. Return None if the last line looks mid-write."""
    try:
        raw = path.read_bytes()
    except OSError:
        return None
    if not raw:
        return []
    text = raw.decode("utf-8", errors="replace")
    lines = text.splitlines()
    out: list[dict[str, Any]] = []
    for i, line in enumerate(lines):
        s = line.strip()
        if not s:
            continue
        try:
            obj = json.loads(s)
        except json.JSONDecodeError:
            if i == len(lines) - 1:
                return None
            continue
        if isinstance(obj, dict):
            out.append(obj)
    return out


def load_fingerprints(path: Path) -> Optional[list[dict[str, Any]]]:
    recs = load_history_records(path)
    if recs is None:
        return None
    return [fingerprint(o, i) for i, o in enumerate(recs)]


def _last_user_index(snaps: list[dict[str, Any]]) -> int:
    last = -1
    for i, s in enumerate(snaps):
        if s.get("type") == "user":
            last = i
    return last


def classify_diff(old: list[dict[str, Any]], new: list[dict[str, Any]]) -> str:
    n, m = len(old), len(new)
    prefix = 0
    while prefix < n and prefix < m and old[prefix]["hash"] == new[prefix]["hash"]:
        prefix += 1
    if prefix == n and m == n:
        return "unchanged"
    if prefix == n and m > n:
        return "append"
    last_user = _last_user_index(old)
    # Dropped only open-turn records (after last user) — not a prefix rewrite
    if m < n and prefix == m and prefix > last_user:
        return "tail"
    if m < n and prefix == m:
        return "truncate"
    # Edits only in the open turn (after last user) = streaming tail, not prefix
    if prefix > last_user:
        return "tail"
    return "mutate"


def _align_changes(
    old: list[dict[str, Any]],
    new: list[dict[str, Any]],
    prefix: int,
) -> list[dict[str, Any]]:
    """Describe non-equal suffix: edit / delete / insert."""
    old_rest = old[prefix:]
    new_rest = new[prefix:]
    changes: list[dict[str, Any]] = []

    def _row(
        op: str,
        index: int,
        typ: str,
        key: Any,
        old_fp: Optional[dict[str, Any]] = None,
        new_fp: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        return {
            "op": op,
            "index": index,
            "type": typ,
            "key": key,
            "old_chars": None if old_fp is None else old_fp.get("chars"),
            "new_chars": None if new_fp is None else new_fp.get("chars"),
            "old_preview": None if old_fp is None else old_fp.get("preview"),
            "new_preview": None if new_fp is None else new_fp.get("preview"),
            "old_hash": None if old_fp is None else old_fp.get("hash"),
            "new_hash": None if new_fp is None else new_fp.get("hash"),
            "compact": bool(
                (new_fp or {}).get("compact") or (old_fp or {}).get("compact")
            ),
        }

    used_old: set[int] = set()
    old_by_key: dict[str, list[int]] = {}
    for i, s in enumerate(old_rest):
        k = s.get("key")
        if isinstance(k, str) and k:
            old_by_key.setdefault(k, []).append(i)

    for j, ns in enumerate(new_rest):
        k = ns.get("key")
        matched = False
        if isinstance(k, str) and k and k in old_by_key:
            for oi in old_by_key[k]:
                if oi in used_old:
                    continue
                used_old.add(oi)
                os_ = old_rest[oi]
                if os_["hash"] != ns["hash"]:
                    changes.append(
                        _row("edit", prefix + oi, ns["type"], k, os_, ns)
                    )
                matched = True
                break
        if matched:
            continue
        # Positional fallback when keys missing and same slot exists
        if j < len(old_rest) and j not in used_old and not old_rest[j].get("key"):
            os_ = old_rest[j]
            used_old.add(j)
            if os_["hash"] != ns["hash"]:
                changes.append(
                    _row("edit", prefix + j, ns["type"], ns.get("key"), os_, ns)
                )
            continue
        changes.append(
            _row("insert", prefix + j, ns["type"], ns.get("key"), None, ns)
        )

    for i, os_ in enumerate(old_rest):
        if i in used_old:
            continue
        changes.append(
            _row("delete", prefix + i, os_["type"], os_.get("key"), os_, None)
        )

    changes.sort(key=lambda c: (c["index"], c["op"]))
    return changes[:MAX_CHANGES_PER_EVENT]


def diff_fingerprints(
    old: list[dict[str, Any]],
    new: list[dict[str, Any]],
) -> dict[str, Any]:
    n, m = len(old), len(new)
    prefix = 0
    while prefix < n and prefix < m and old[prefix]["hash"] == new[prefix]["hash"]:
        prefix += 1
    kind = classify_diff(old, new)
    changes: list[dict[str, Any]] = []
    if kind not in ("unchanged", "append"):
        changes = _align_changes(old, new, prefix)
    elif kind == "append":
        for j in range(n, m):
            ns = new[j]
            changes.append(
                {
                    "op": "insert",
                    "index": j,
                    "type": ns["type"],
                    "key": ns.get("key"),
                    "old_chars": None,
                    "new_chars": ns.get("chars"),
                    "old_preview": None,
                    "new_preview": ns.get("preview"),
                    "old_hash": None,
                    "new_hash": ns.get("hash"),
                    "compact": bool(ns.get("compact")),
                }
            )
            if len(changes) >= MAX_CHANGES_PER_EVENT:
                break

    edited = sum(1 for c in changes if c["op"] == "edit")
    deleted = sum(1 for c in changes if c["op"] == "delete")
    inserted = sum(1 for c in changes if c["op"] == "insert")
    from_compact, compact_why = _compact_meta(old, new, changes, kind)
    return {
        "kind": kind,
        "old_n": n,
        "new_n": m,
        "prefix": prefix,
        "cache_break_index": None if kind in ("unchanged", "append") else prefix,
        "added": max(0, m - n) if kind == "append" else inserted,
        "removed": deleted,
        "changed": edited,
        "changes": changes,
        "from_compact": from_compact,
        "compact_why": compact_why,
    }


def _compact_meta(
    old: list[dict[str, Any]],
    new: list[dict[str, Any]],
    changes: list[dict[str, Any]],
    kind: str,
) -> tuple[bool, Optional[str]]:
    """True when this rewrite looks like harness compaction, not a random edit."""
    if kind in ("unchanged", "append"):
        return False, None
    new_glue = [s for s in new if s.get("compact")]
    old_hashes = {s.get("hash") for s in old}
    appeared = [s for s in new_glue if s.get("hash") not in old_hashes]
    if appeared:
        syn = next((s.get("synthetic_reason") for s in appeared if s.get("synthetic_reason")), None)
        return True, f"compact glue ({syn})" if syn else "compact glue"
    for c in changes:
        blob = " ".join(
            str(x)
            for x in (c.get("new_preview"), c.get("old_preview"), c.get("key"))
            if x
        )
        if is_compact_signal(blob):
            return True, "compact glue in diff"
        if c.get("compact"):
            return True, "compact record"
    return False, None


def iter_chat_history_files(root: Path) -> list[tuple[Path, Path]]:
    """(session_dir, chat_history.jsonl) under sessions root."""
    out: list[tuple[Path, Path]] = []
    if not root.is_dir():
        return out
    dirs = list_session_dirs(root)
    seen: set[Path] = set()
    for d in dirs:
        p = d / "chat_history.jsonl"
        if p.is_file():
            out.append((d, p))
            seen.add(d)
    # Sessions that have history but no updates.jsonl yet
    try:
        children = list(root.iterdir())
    except OSError:
        return out
    for child in children:
        if not child.is_dir():
            continue
        direct = child / "chat_history.jsonl"
        if direct.is_file() and child not in seen:
            out.append((child, direct))
            seen.add(child)
        try:
            subs = list(child.iterdir())
        except OSError:
            continue
        for sub in subs:
            if not sub.is_dir() or sub in seen:
                continue
            p = sub / "chat_history.jsonl"
            if p.is_file():
                out.append((sub, p))
                seen.add(sub)
    return out


class _SessionTrack:
    __slots__ = (
        "session_id",
        "path",
        "session_dir",
        "mtime",
        "size",
        "snaps",
        "first_seen",
        "last_kind",
        "counts",
        "label",
    )

    def __init__(
        self,
        session_id: str,
        path: Path,
        session_dir: Path,
        mtime: float,
        size: int,
        snaps: list[dict[str, Any]],
        now: float,
        label: str,
    ) -> None:
        self.session_id = session_id
        self.path = path
        self.session_dir = session_dir
        self.mtime = mtime
        self.size = size
        self.snaps = snaps
        self.first_seen = now
        self.last_kind = "baseline"
        self.counts = {"append": 0, "tail": 0, "mutate": 0, "truncate": 0}
        self.label = label


def _session_label(session_dir: Path, sid: str) -> str:
    summary = _read_session_summary(session_dir)
    from token_telemetry.session.discover import pick_session_title

    return pick_session_title(summary, session_id=sid, max_len=52)


class HistoryWatcher:
    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = root or SESSIONS_ROOT
        self.lock = threading.Lock()
        self.started_at = time.time()
        self.sessions: dict[str, _SessionTrack] = {}
        self.events: deque[dict[str, Any]] = deque(maxlen=MAX_EVENTS)
        self._event_seq = 0

    def reset(self, root: Optional[Path] = None) -> None:
        with self.lock:
            if root is not None:
                self.root = root
            self.started_at = time.time()
            self.sessions.clear()
            self.events.clear()
            self._event_seq = 0

    def _push_event(self, ev: dict[str, Any]) -> None:
        self._event_seq += 1
        ev["id"] = self._event_seq
        self.events.appendleft(ev)

    def _evict_if_needed(self, incoming_mtime: float) -> bool:
        """Drop a staler session so a newer file can be baselined."""
        if len(self.sessions) < MAX_SESSIONS:
            return True
        victim = min(self.sessions.values(), key=lambda r: (r.counts["mutate"], r.mtime))
        if incoming_mtime <= victim.mtime:
            return False
        del self.sessions[victim.session_id]
        return True

    def tick(self) -> None:
        now = time.time()
        files = iter_chat_history_files(self.root)
        dated: list[tuple[float, Path, Path]] = []
        for session_dir, path in files:
            try:
                dated.append((path.stat().st_mtime, session_dir, path))
            except OSError:
                continue
        dated.sort(key=lambda t: t[0], reverse=True)
        for _mtime, session_dir, path in dated:
            sid = session_dir.name
            try:
                st = path.stat()
            except OSError:
                continue
            with self.lock:
                rec = self.sessions.get(sid)
                if rec is None:
                    if len(self.sessions) >= MAX_SESSIONS and not self._evict_if_needed(
                        st.st_mtime
                    ):
                        continue
                    snaps = load_fingerprints(path)
                    if snaps is None:
                        continue
                    label = _session_label(session_dir, sid)
                    self.sessions[sid] = _SessionTrack(
                        sid, path, session_dir, st.st_mtime, st.st_size, snaps, now, label
                    )
                    self._push_event(
                        {
                            "ts": now,
                            "session_id": sid,
                            "label": label,
                            "kind": "baseline",
                            "old_n": 0,
                            "new_n": len(snaps),
                            "prefix": len(snaps),
                            "cache_break_index": None,
                            "added": 0,
                            "removed": 0,
                            "changed": 0,
                            "changes": [],
                            "note": "first sight — no prior snapshot",
                        }
                    )
                    continue
                if st.st_mtime == rec.mtime and st.st_size == rec.size:
                    continue
                snaps = load_fingerprints(path)
                if snaps is None:
                    continue
                diff = diff_fingerprints(rec.snaps, snaps)
                rec.mtime = st.st_mtime
                rec.size = st.st_size
                rec.snaps = snaps
                rec.label = _session_label(session_dir, sid)
                kind = diff["kind"]
                if kind == "unchanged":
                    rec.last_kind = kind
                    continue
                if kind in rec.counts:
                    rec.counts[kind] += 1
                rec.last_kind = kind
                ev = {
                    "ts": now,
                    "session_id": sid,
                    "label": rec.label,
                    "kind": kind,
                    **{k: diff[k] for k in (
                        "old_n",
                        "new_n",
                        "prefix",
                        "cache_break_index",
                        "added",
                        "removed",
                        "changed",
                        "changes",
                        "from_compact",
                        "compact_why",
                    )},
                }
                self._push_event(ev)

    def snapshot(self) -> dict[str, Any]:
        now = time.time()
        with self.lock:
            sessions = []
            for rec in sorted(
                self.sessions.values(),
                key=lambda r: r.mtime,
                reverse=True,
            ):
                sessions.append(
                    {
                        "session_id": rec.session_id,
                        "label": rec.label,
                        "path": str(rec.path),
                        "records": len(rec.snaps),
                        "bytes": rec.size,
                        "mtime": rec.mtime,
                        "age_label": format_age(max(0.0, now - rec.mtime)),
                        "first_seen": rec.first_seen,
                        "last_kind": rec.last_kind,
                        "mutations": rec.counts["mutate"] + rec.counts["truncate"],
                        "appends": rec.counts["append"],
                        "tails": rec.counts["tail"],
                        "truncates": rec.counts["truncate"],
                    }
                )
            events = list(self.events)
        return {
            "ok": True,
            "started_at": self.started_at,
            "now": now,
            "uptime_s": round(now - self.started_at, 1),
            "note": (
                "Baselines at first sight. Mutations before start, or while "
                "this process was down, cannot be reconstructed."
            ),
            "sessions": sessions,
            "events": events,
        }


WATCHER = HistoryWatcher()
