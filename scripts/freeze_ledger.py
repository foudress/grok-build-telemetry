#!/usr/bin/env python3
"""Dump / check slim per-round ledgers. Local goldens, not CI session dumps.

Usage:
  python scripts/freeze_ledger.py --dump [--sid SID ...]
  python scripts/freeze_ledger.py --check

Goldens live in ``~/.grok/token-telemetry/goldens/<sid>.ledger.json`` by default
(personal jsonl never goes in git). Set ``TT_GOLDENS`` to override.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from token_telemetry.hierarchy import HierarchyBuilder  # noqa: E402
from token_telemetry.session.discover import (  # noqa: E402
    SESSIONS_ROOT,
    list_session_dirs,
)
from token_telemetry.util import as_float, as_int  # noqa: E402

DEFAULT_GOLDENS = Path.home() / ".grok" / "token-telemetry" / "goldens"
USD_EPS = 1e-9


def _goldens_dir() -> Path:
    raw = __import__("os").environ.get("TT_GOLDENS")
    return Path(raw) if raw else DEFAULT_GOLDENS


def _find_session_dir(sid: str) -> Optional[Path]:
    sid = str(sid or "").strip()
    if not sid:
        return None
    for d in list_session_dirs():
        if d.name == sid and (d / "updates.jsonl").is_file():
            return d
    # Direct under any cwd encoding.
    if SESSIONS_ROOT.is_dir():
        for p in SESSIONS_ROOT.rglob(sid):
            if p.is_dir() and (p / "updates.jsonl").is_file():
                return p
    return None


def replay(session_dir: Path) -> HierarchyBuilder:
    hb = HierarchyBuilder(max_rounds=4000)
    hb.set_session_dir(session_dir)
    path = session_dir / "updates.jsonl"
    with path.open(encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            hb.feed_raw(rec)
    return hb


def _part_slim(p: Any) -> Optional[dict[str, Any]]:
    if not isinstance(p, dict):
        return None
    return {"kind": p.get("kind"), "tokens": as_int(p.get("tokens") or p.get("tokens_in"))}


def _step_slim(s: Any) -> Optional[dict[str, Any]]:
    if not isinstance(s, dict):
        return None
    return {
        "tokens_in": as_int(s.get("tokens_in")),
        "tokens_cached": as_int(s.get("tokens_cached") or s.get("cached_read_tokens")),
        "tokens_out": as_int(s.get("tokens_out")),
        "composition": s.get("composition"),
    }


def _sa_slim(sa: Any) -> Optional[dict[str, Any]]:
    if not isinstance(sa, dict):
        return None
    return {
        "label": sa.get("label"),
        "is_sys": bool(sa.get("is_sys")),
        "tokens_in": as_int(sa.get("tokens_in")),
        "tokens_cached": as_int(sa.get("tokens_cached")),
        "tokens_out": as_int(sa.get("tokens_out")),
    }


def _bd_slim(bd: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "tree_in_tokens",
        "tree_in_usd",
        "user_in_tokens",
        "user_in_usd",
        "harness_in_tokens",
        "harness_in_usd",
        "cache_miss_in_tokens",
        "cache_miss_in_usd",
        "cached_tokens",
        "cached_usd",
        "output_tokens",
        "output_usd",
        "llm_thought_summary_tokens",
        "llm_reasoning_encrypted_tokens",
        "llm_out_to_harness_tokens",
        "llm_out_to_user_tokens",
        "round_total_peeled_system",
        "api_total_usd",
    )
    out: dict[str, Any] = {}
    for k in keys:
        if k not in bd:
            continue
        v = bd.get(k)
        if k.endswith("_usd") or k == "round_total_peeled_system" and isinstance(v, float):
            out[k] = as_float(v)
        else:
            out[k] = v
    # Aliases used in some rounds.
    if "tree_in_tokens" not in out and "tree_in" in bd:
        out["tree_in_tokens"] = as_int(bd.get("tree_in"))
    return out


def slim_round(r: dict[str, Any]) -> dict[str, Any]:
    sp = r.get("system_prompt") if isinstance(r.get("system_prompt"), dict) else {}
    up = r.get("user_prompt") if isinstance(r.get("user_prompt"), dict) else {}
    bd = r.get("breakdown") if isinstance(r.get("breakdown"), dict) else {}
    co = up.get("compact_out") if isinstance(up.get("compact_out"), dict) else {}
    parts = []
    for p in sp.get("parts") or []:
        slim = _part_slim(p)
        if slim:
            parts.append(slim)
    steps = []
    for s in r.get("model_steps") or []:
        slim = _step_slim(s)
        if slim:
            steps.append(slim)
    sas = []
    for step in r.get("model_steps") or []:
        if not isinstance(step, dict):
            continue
        for sa in step.get("subagents_after") or []:
            slim = _sa_slim(sa)
            if slim:
                sas.append(slim)
    return {
        "index": r.get("index"),
        "context_start": r.get("context_start"),
        "context_end": r.get("context_end"),
        "system": {
            "tokens_in": as_int(sp.get("tokens_in") or sp.get("logical_tokens")),
            "parts": parts,
        },
        "user": {
            "tokens_in": as_int(up.get("tokens_in") or up.get("uncached_est")),
            "prompt_tokens_in": up.get("prompt_tokens_in"),
            "compact_out": {"tokens_in": as_int(co.get("tokens_in"))} if co else None,
        },
        "breakdown": _bd_slim(bd),
        "steps": steps,
        "subagents_after": sas,
    }


def kpi_from_rounds(rounds: list[dict[str, Any]]) -> dict[str, Any]:
    unc = cache = out = 0
    official = 0.0
    estimate = 0.0
    parent_est = 0.0
    for r in rounds:
        if not isinstance(r, dict):
            continue
        usage = r.get("usage_raw") if isinstance(r.get("usage_raw"), dict) else {}
        inn = as_int(usage.get("inputTokens"))
        c = as_int(usage.get("cachedReadTokens"))
        o = as_int(usage.get("outputTokens"))
        unc += max(0, inn - min(c, inn))
        cache += c
        out += o
        bd = r.get("breakdown") if isinstance(r.get("breakdown"), dict) else {}
        api = bd.get("api_total_usd")
        if api is not None:
            parent_est += as_float(api)
        else:
            parent_est += as_float(r.get("estimate_usd"))
        estimate += as_float(r.get("estimate_usd"))
        ticks = usage.get("costUsdTicks")
        if ticks is not None:
            official += as_float(ticks) / 1e10
    return {
        "uncached": int(unc),
        "cached": int(cache),
        "output": int(out),
        "official_usd": round(official, 6),
        "estimate_usd": round(estimate, 6),
        "parent_est": round(parent_est, 6),
        "children_est": 0.0,
    }


def dump_session(session_dir: Path) -> dict[str, Any]:
    hb = replay(session_dir)
    rounds = hb.snapshot_rounds(include_open=False)
    return {
        "session_id": session_dir.name,
        "rounds": [slim_round(r) for r in rounds if isinstance(r, dict)],
        "kpi": kpi_from_rounds(rounds),
    }


def _num_eq(a: Any, b: Any) -> bool:
    if isinstance(a, float) or isinstance(b, float):
        try:
            return abs(float(a or 0) - float(b or 0)) < USD_EPS
        except (TypeError, ValueError):
            return a == b
    return a == b


def _diff(path: str, a: Any, b: Any, out: list[str]) -> None:
    if type(a) is not type(b) and not (
        isinstance(a, (int, float)) and isinstance(b, (int, float))
    ):
        if a is None or b is None:
            if a != b:
                out.append(f"{path}: {a!r} != {b!r}")
            return
    if isinstance(a, dict) and isinstance(b, dict):
        keys = sorted(set(a) | set(b))
        for k in keys:
            _diff(f"{path}.{k}", a.get(k), b.get(k), out)
        return
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            out.append(f"{path}: len {len(a)} != {len(b)}")
            n = min(len(a), len(b))
        else:
            n = len(a)
        for i in range(n):
            _diff(f"{path}[{i}]", a[i], b[i], out)
        return
    if not _num_eq(a, b):
        out.append(f"{path}: {a!r} != {b!r}")


def check_blob(got: dict[str, Any], exp: dict[str, Any]) -> list[str]:
    diffs: list[str] = []
    _diff("", got, exp, diffs)
    return diffs


def _manifest_sids(gdir: Path) -> list[str]:
    man = gdir / "manifest.json"
    if man.is_file():
        try:
            data = json.loads(man.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            data = None
        if isinstance(data, dict) and isinstance(data.get("sids"), list):
            return [str(s) for s in data["sids"] if s]
        if isinstance(data, list):
            return [str(s) for s in data if s]
    return [p.stem.replace(".ledger", "") for p in sorted(gdir.glob("*.ledger.json"))]


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dump", action="store_true")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--sid", action="append", default=[])
    ap.add_argument("--goldens", type=Path, default=None)
    args = ap.parse_args(argv)
    gdir = args.goldens or _goldens_dir()
    if not args.dump and not args.check:
        ap.print_help()
        return 2
    sids = list(args.sid) or _manifest_sids(gdir)
    if args.dump:
        if not sids:
            print("no --sid and no goldens/manifest.json — nothing to dump", file=sys.stderr)
            return 1
        gdir.mkdir(parents=True, exist_ok=True)
        wrote = 0
        for sid in sids:
            d = _find_session_dir(sid)
            if d is None:
                print(f"skip missing session {sid}", file=sys.stderr)
                continue
            blob = dump_session(d)
            dest = gdir / f"{sid}.ledger.json"
            dest.write_text(json.dumps(blob, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
            print(f"wrote {dest} ({len(blob.get('rounds') or [])} rounds)")
            wrote += 1
        man = {"sids": sids}
        (gdir / "manifest.json").write_text(
            json.dumps(man, indent=2) + "\n", encoding="utf-8"
        )
        return 0 if wrote else 1
    # --check
    files = sorted(gdir.glob("*.ledger.json"))
    if not files:
        print("no local goldens; skip freeze check (ok)")
        return 0
    failed = 0
    for path in files:
        try:
            exp = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as e:
            print(f"{path.name}: cannot read ({e})")
            failed += 1
            continue
        sid = str(exp.get("session_id") or path.name.replace(".ledger.json", ""))
        d = _find_session_dir(sid)
        if d is None:
            print(f"{sid}: session dir missing — skip")
            continue
        got = dump_session(d)
        diffs = check_blob(got, exp)
        if diffs:
            failed += 1
            print(f"{sid}: {len(diffs)} diffs")
            for line in diffs[:40]:
                print(f"  {line}")
            if len(diffs) > 40:
                print(f"  … {len(diffs) - 40} more")
        else:
            print(f"{sid}: ok")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
