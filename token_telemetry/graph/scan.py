"""Walk a repo root and emit a Python ast call-graph with folder clusters."""

from __future__ import annotations

import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

from token_telemetry.graph.resolve import (
    IGNORE_DIRS,
    MAX_FILE_BYTES,
    MAX_FILE_NODES,
    MAX_SITES,
    Binding,
    ancestor_dirs,
    extract_module,
    module_name,
    parent_posix,
    parse_python,
    posix_rel,
    resolve_call,
    resolve_import,
)


def _keep_dir(name: str) -> bool:
    if name in IGNORE_DIRS or name.lower() in IGNORE_DIRS:
        return False
    # Home-folder scans otherwise walk AppData / .cache forever.
    if name.startswith(".") and name not in (".", ".."):
        return False
    return True


def _iter_py_files(root: Path) -> list[Path]:
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = sorted(d for d in dirnames if _keep_dir(d))
        for name in filenames:
            if not name.endswith(".py"):
                continue
            path = Path(dirpath) / name
            try:
                if path.stat().st_size > MAX_FILE_BYTES:
                    continue
            except OSError:
                continue
            found.append(path)
            if len(found) >= MAX_FILE_NODES:
                dirnames.clear()
                return found
    found.sort(key=lambda p: posix_rel(root, p))
    return found


def _read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def _file_node(rel: str, loc: int) -> dict[str, Any]:
    return {
        "id": rel,
        "path": rel,
        "dir": parent_posix(rel),
        "loc": loc,
        "deg": 0,
        "kind": "file",
    }


def _cluster_node(rel_dir: str) -> dict[str, Any]:
    return {
        "id": f"dir:{rel_dir}",
        "path": rel_dir,
        "dir": parent_posix(rel_dir),
        "loc": 0,
        "deg": 0,
        "kind": "cluster",
    }


def scan_repo(root: str | Path) -> dict[str, Any]:
    """Return `{root, nodes, edges, scanned_at}` for Python files under *root*."""
    scanned_at = time.time()
    root_path = Path(root).expanduser().resolve()
    root_str = os.path.normpath(str(root_path))
    empty: dict[str, Any] = {
        "root": root_str,
        "nodes": [],
        "edges": [],
        "scanned_at": scanned_at,
    }
    if not root_path.is_dir():
        return empty

    discovered = _iter_py_files(root_path)
    selected = discovered[:MAX_FILE_NODES]

    by_name: dict[str, str] = {}
    packages: set[str] = set()
    locs: dict[str, int] = {}
    extracted: dict[str, Any] = {}

    for path in selected:
        rel = posix_rel(root_path, path)
        text = _read_text(path)
        if text is None:
            continue
        locs[rel] = len(text.splitlines())
        mod = module_name(rel)
        by_name[mod] = rel
        if Path(rel).name == "__init__.py":
            packages.add(mod)
        tree = parse_python(text, filename=str(path))
        extracted[rel] = extract_module(tree) if tree is not None else None

    defs_by_mod: dict[str, set[str]] = {}
    for mod, rel in by_name.items():
        ext = extracted.get(rel)
        defs_by_mod[mod] = set(ext.defs) if ext is not None else set()

    rel_of: dict[str, str] = {rel: mod for mod, rel in by_name.items()}

    # (src, dst, kind) -> sites
    buckets: dict[tuple[str, str, str], list[dict[str, str]]] = defaultdict(list)
    for rel, ext in extracted.items():
        if ext is None:
            continue
        current_mod = rel_of[rel]
        bindings: dict[str, Binding] = {}
        for raw in ext.imports:
            bound = resolve_import(raw, current_mod, by_name, packages)
            if bound is not None:
                bindings[raw.local] = bound
        for call in ext.calls:
            resolved = resolve_call(
                call, bindings, rel, ext.defs, by_name, defs_by_mod
            )
            if resolved is None:
                continue
            buckets[(rel, resolved.dst, resolved.kind)].append(
                {"caller": resolved.caller, "callee": resolved.callee}
            )

    # Prefer a resolved call over the import-fallback on the same pair.
    pairs_with_call = {(s, d) for (s, d, k) in buckets if k == "call"}
    edges: list[dict[str, Any]] = []
    for (src, dst, kind), sites in buckets.items():
        if kind == "import" and (src, dst) in pairs_with_call:
            continue
        edge: dict[str, Any] = {
            "src": src,
            "dst": dst,
            "kind": kind,
            "w": 1 if kind == "import" else len(sites),
        }
        shown = sites[:1] if kind == "import" else sites
        if len(shown) <= MAX_SITES:
            edge["sites"] = shown
        edges.append(edge)

    file_ids = set(locs)
    nodes = [_file_node(rel, locs[rel]) for rel in file_ids]
    cluster_dirs: set[str] = set()
    for rel in file_ids:
        cluster_dirs.update(ancestor_dirs(rel))
    nodes.extend(_cluster_node(d) for d in cluster_dirs)

    deg: dict[str, int] = defaultdict(int)
    for edge in edges:
        deg[edge["src"]] += 1
        deg[edge["dst"]] += 1
    for node in nodes:
        node["deg"] = deg.get(node["id"], 0)

    nodes.sort(key=lambda n: n["id"])
    edges.sort(key=lambda e: (e["src"], e["dst"], e["kind"]))
    return {
        "root": root_str,
        "nodes": nodes,
        "edges": edges,
        "scanned_at": scanned_at,
    }
