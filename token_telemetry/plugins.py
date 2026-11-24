"""External connector plugins for the dashboard.

Built-in sources stay ``grok`` (default ``/api/*``), ``codex``
(``/api/codex/*``), and ``antigravity`` (``/api/antigravity/*``). Extra
sources load from outside this package and mount at
``/api/<id>/*`` with the same feed shape Codex already serves.

Discovery order (later overrides same id):

1. ``~/.grok-telemetry/connectors/<id>/connector.py``
2. ``TOKEN_TELEMETRY_PLUGINS`` — ``os.pathsep`` list of dirs, ``connector.py``
   files, or importable module paths
3. setuptools entry points group ``token_telemetry.connectors``

A connector module must expose either:

- ``CONNECTOR`` — a mapping / object with ``id``, ``label``, ``feed``
- ``register()`` — returns that mapping / object

``feed`` must implement the Codex feed surface used by the HTTP server:
``select``, ``reset``, ``health``, ``sessions``, ``state_bytes``, ``aggregate``.
Optional: ``last_active_mtime() -> float`` for launch preference.
"""

from __future__ import annotations

import importlib
import importlib.util
import os
import sys
import threading
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

RESERVED_IDS = frozenset(
    {
        "grok",
        "codex",
        "antigravity",
        "api",
        "css",
        "js",
        "history",
        "graph",
        "sources",
        "static",
        "favicon.ico",
    }
)

_ENTRY_GROUP = "token_telemetry.connectors"
_ENV = "TOKEN_TELEMETRY_PLUGINS"

_lock = threading.RLock()
_loaded = False
_connectors: dict[str, "Connector"] = {}
_errors: list[dict[str, str]] = []


@dataclass(frozen=True)
class Connector:
    """One extra telemetry source shown in the header switch."""

    id: str
    label: str
    feed: Any
    title: Optional[str] = None
    context_cliff: Optional[int] = None
    last_active_mtime: Optional[Callable[[], float]] = None


def connectors_root() -> Path:
    return Path.home() / ".grok-telemetry" / "connectors"


def _slug(raw: str) -> str:
    text = "".join(ch if ch.isalnum() or ch in "-_" else "-" for ch in (raw or "").strip().lower())
    while "--" in text:
        text = text.replace("--", "-")
    return text.strip("-_")


def _call_maybe(obj: Any, name: str) -> Any:
    attr = getattr(obj, name, None)
    if callable(attr):
        return attr()
    return attr


def _as_connector(raw: Any, *, fallback_id: str = "") -> Connector:
    if isinstance(raw, Connector):
        conn = raw
    elif isinstance(raw, dict):
        conn = Connector(
            id=str(raw.get("id") or fallback_id or "").strip(),
            label=str(raw.get("label") or raw.get("id") or fallback_id or "").strip(),
            feed=raw.get("feed"),
            title=(str(raw["title"]).strip() if raw.get("title") else None),
            context_cliff=(
                int(raw["context_cliff"])
                if raw.get("context_cliff") is not None
                else None
            ),
            last_active_mtime=raw.get("last_active_mtime"),
        )
    else:
        conn = Connector(
            id=str(_call_maybe(raw, "id") or fallback_id or "").strip(),
            label=str(_call_maybe(raw, "label") or _call_maybe(raw, "id") or fallback_id or "").strip(),
            feed=getattr(raw, "feed", raw),
            title=(str(t).strip() if (t := _call_maybe(raw, "title")) else None),
            context_cliff=(
                int(c) if (c := _call_maybe(raw, "context_cliff")) is not None else None
            ),
            last_active_mtime=getattr(raw, "last_active_mtime", None),
        )
    cid = _slug(conn.id)
    if not cid or cid in RESERVED_IDS:
        raise ValueError(f"invalid connector id {conn.id!r}")
    if not conn.label:
        raise ValueError(f"connector {cid!r} missing label")
    feed = conn.feed
    if feed is None:
        raise ValueError(f"connector {cid!r} missing feed")
    for method in ("select", "reset", "health", "sessions", "state_bytes", "aggregate"):
        if not callable(getattr(feed, method, None)):
            raise ValueError(f"connector {cid!r} feed missing {method}()")
    last = conn.last_active_mtime
    if last is not None and not callable(last):
        raise ValueError(f"connector {cid!r} last_active_mtime must be callable")
    return Connector(
        id=cid,
        label=conn.label,
        feed=feed,
        title=conn.title or conn.label,
        context_cliff=conn.context_cliff,
        last_active_mtime=last,
    )


def _load_module(path: Path, *, module_name: str) -> Any:
    path = path.resolve()
    if not path.is_file():
        raise FileNotFoundError(str(path))
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _from_module(mod: Any, *, fallback_id: str) -> Connector:
    if hasattr(mod, "register") and callable(mod.register):
        return _as_connector(mod.register(), fallback_id=fallback_id)
    if hasattr(mod, "CONNECTOR"):
        return _as_connector(mod.CONNECTOR, fallback_id=fallback_id)
    raise ValueError("module must define CONNECTOR or register()")


def _remember(conn: Connector) -> None:
    _connectors[conn.id] = conn


def _note_error(source: str, exc: BaseException) -> None:
    _errors.append(
        {
            "source": source,
            "error": f"{type(exc).__name__}: {exc}",
            "detail": traceback.format_exc(limit=6),
        }
    )


def _load_dropins() -> None:
    root = connectors_root()
    if not root.is_dir():
        return
    try:
        children = sorted(root.iterdir(), key=lambda p: p.name.lower())
    except OSError as exc:
        _note_error(str(root), exc)
        return
    for child in children:
        if not child.is_dir():
            continue
        path = child / "connector.py"
        if not path.is_file():
            continue
        name = f"token_telemetry_plugin_{_slug(child.name) or 'x'}"
        try:
            mod = _load_module(path, module_name=name)
            _remember(_from_module(mod, fallback_id=child.name))
        except Exception as exc:  # noqa: BLE001 — isolate bad plugins
            _note_error(str(path), exc)


def _load_env_paths() -> None:
    raw = (os.environ.get(_ENV) or "").strip()
    if not raw:
        return
    for part in raw.split(os.pathsep):
        item = part.strip().strip('"').strip("'")
        if not item:
            continue
        path = Path(item).expanduser()
        try:
            if path.is_dir():
                candidate = path / "connector.py"
                if not candidate.is_file():
                    # allow package dir with __init__.py that re-exports
                    init = path / "__init__.py"
                    if init.is_file():
                        candidate = init
                    else:
                        raise FileNotFoundError(f"no connector.py under {path}")
                mod_name = f"token_telemetry_plugin_env_{_slug(path.name) or 'x'}"
                mod = _load_module(candidate, module_name=mod_name)
                _remember(_from_module(mod, fallback_id=path.name))
            elif path.is_file():
                mod_name = f"token_telemetry_plugin_file_{_slug(path.stem) or 'x'}"
                mod = _load_module(path, module_name=mod_name)
                _remember(_from_module(mod, fallback_id=path.stem))
            else:
                mod = importlib.import_module(item)
                _remember(_from_module(mod, fallback_id=item.rsplit(".", 1)[-1]))
        except Exception as exc:  # noqa: BLE001
            _note_error(item, exc)


def _load_entry_points() -> None:
    try:
        from importlib.metadata import entry_points
    except ImportError:
        return
    try:
        eps = entry_points()
    except Exception as exc:  # noqa: BLE001
        _note_error("entry_points", exc)
        return
    selected: Iterable[Any]
    if hasattr(eps, "select"):
        selected = eps.select(group=_ENTRY_GROUP)
    else:  # pragma: no cover — py3.10 dict shape
        selected = eps.get(_ENTRY_GROUP, [])  # type: ignore[call-arg]
    for ep in selected:
        try:
            loaded = ep.load()
            if callable(loaded) and not hasattr(loaded, "feed"):
                loaded = loaded()
            _remember(_as_connector(loaded, fallback_id=str(getattr(ep, "name", "") or "")))
        except Exception as exc:  # noqa: BLE001
            _note_error(f"entry:{getattr(ep, 'name', '?')}", exc)


def reload_connectors() -> list[Connector]:
    """Clear and reload every discovery source. Useful for tests."""
    global _loaded
    with _lock:
        _connectors.clear()
        _errors.clear()
        _load_dropins()
        _load_env_paths()
        _load_entry_points()
        _loaded = True
        return list(_connectors.values())


def ensure_loaded() -> None:
    with _lock:
        if not _loaded:
            reload_connectors()


def list_connectors() -> list[Connector]:
    ensure_loaded()
    with _lock:
        return sorted(_connectors.values(), key=lambda c: c.label.lower())


def get_connector(connector_id: str) -> Optional[Connector]:
    ensure_loaded()
    cid = _slug(connector_id)
    with _lock:
        return _connectors.get(cid)


def load_errors() -> list[dict[str, str]]:
    ensure_loaded()
    with _lock:
        return list(_errors)


def builtin_sources() -> list[dict[str, Any]]:
    return [
        {
            "id": "grok",
            "label": "Grok Build",
            "title": "Metryx — Grok Build",
            "api_prefix": "/api",
            "builtin": True,
            "context_cliff": 200_000,
        },
        {
            "id": "codex",
            "label": "Codex",
            "title": "Metryx — Codex",
            "api_prefix": "/api/codex",
            "builtin": True,
            "context_cliff": 272_000,
        },
        {
            "id": "antigravity",
            "label": "Antigravity",
            "title": "Metryx — Antigravity",
            "api_prefix": "/api/antigravity",
            "builtin": True,
            "context_cliff": 0,
        },
    ]


def sources_payload() -> dict[str, Any]:
    rows = list(builtin_sources())
    for conn in list_connectors():
        rows.append(
            {
                "id": conn.id,
                "label": conn.label,
                "title": conn.title or conn.label,
                "api_prefix": f"/api/{conn.id}",
                "builtin": False,
                "context_cliff": conn.context_cliff,
            }
        )
    return {"sources": rows, "errors": load_errors()}
