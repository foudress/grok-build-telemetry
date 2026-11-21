"""External connector discovery and /api/sources payload."""

from __future__ import annotations

import json
from pathlib import Path

import token_telemetry.plugins as plugins


class _Feed:
    def select(self, session_id):
        return {"ok": True, "session_id": session_id}

    def reset(self):
        return {"ok": True, "source": "demo"}

    def health(self):
        return {"ok": True, "source": "demo"}

    def sessions(self):
        return {"sessions": [], "source": "demo"}

    def state_bytes(self):
        return b"{}", '"1"'

    def aggregate(self, period, offset, grain, on_progress=None, billing=False):
        return {"ok": True, "period": period, "billing": bool(billing)}


def test_dropin_connector(tmp_path: Path, monkeypatch):
    root = tmp_path / "connectors" / "demo"
    root.mkdir(parents=True)
    (root / "connector.py").write_text(
        "CONNECTOR = {"
        " 'id': 'demo',"
        " 'label': 'Demo',"
        " 'feed': type('F', (), {"
        "   'select': staticmethod(lambda sid: {'ok': True}),"
        "   'reset': staticmethod(lambda: {'ok': True}),"
        "   'health': staticmethod(lambda: {'ok': True}),"
        "   'sessions': staticmethod(lambda: {'sessions': []}),"
        "   'state_bytes': staticmethod(lambda: (b'{}', '\"1\"')),"
        "   'aggregate': staticmethod(lambda *a, **k: {'ok': True}),"
        " })()"
        "}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(plugins, "connectors_root", lambda: tmp_path / "connectors")
    monkeypatch.delenv("TOKEN_TELEMETRY_PLUGINS", raising=False)
    rows = plugins.reload_connectors()
    assert any(c.id == "demo" and c.label == "Demo" for c in rows)
    payload = plugins.sources_payload()
    ids = [s["id"] for s in payload["sources"]]
    assert ids[:3] == ["grok", "codex", "antigravity"]
    assert "antigravity" in plugins.RESERVED_IDS
    assert "demo" in ids


def test_reserved_id_rejected(tmp_path: Path, monkeypatch):
    root = tmp_path / "connectors" / "codex"
    root.mkdir(parents=True)
    (root / "connector.py").write_text(
        "class F:\n"
        "  def select(self, session_id): return {'ok': True}\n"
        "  def reset(self): return {'ok': True}\n"
        "  def health(self): return {'ok': True}\n"
        "  def sessions(self): return {'sessions': []}\n"
        "  def state_bytes(self): return b'{}', '\"1\"'\n"
        "  def aggregate(self, period, offset, grain, on_progress=None): return {'ok': True}\n"
        "CONNECTOR = {'id': 'codex', 'label': 'Nope', 'feed': F()}\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(plugins, "connectors_root", lambda: tmp_path / "connectors")
    monkeypatch.delenv("TOKEN_TELEMETRY_PLUGINS", raising=False)
    plugins.reload_connectors()
    assert plugins.get_connector("codex") is None
    assert plugins.load_errors()


def test_inject_sources_snippet():
    from token_telemetry.server.http import _inject_sources

    html = b"<html><head><title>x</title></head></html>"
    out = _inject_sources(html)
    assert b"window.__TT_SOURCES__=" in out
    start = out.index(b"window.__TT_SOURCES__=") + len(b"window.__TT_SOURCES__=")
    end = out.index(b";</script>", start)
    payload = json.loads(out[start:end].decode("utf-8"))
    assert payload["sources"][0]["id"] == "grok"
