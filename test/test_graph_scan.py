"""Call-graph scan: resolve, same-file skip, import fallback, ignore dirs."""

from __future__ import annotations

from pathlib import Path

from token_telemetry.graph.scan import scan_repo

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "graph_pkg"
NODE_KEYS = {"id", "path", "dir", "loc", "deg", "kind"}
EDGE_KEYS = {"src", "dst", "kind", "w"}


def _scan():
    return scan_repo(FIXTURE)


def test_resolve_a_to_b_call():
    g = _scan()
    assert any(
        e["src"] == "a.py" and e["dst"] == "b.py" and e["kind"] == "call"
        for e in g["edges"]
    )


def test_skip_same_file_calls():
    g = _scan()
    assert not any(e["src"] == e["dst"] for e in g["edges"])
    assert not any(e["src"] == "a.py" and e["dst"] == "a.py" for e in g["edges"])


def test_import_fallback_present():
    g = _scan()
    assert any(
        e["src"] == "c.py" and e["dst"] == "d.py" and e["kind"] == "import"
        for e in g["edges"]
    )


def test_ignore_venv():
    g = _scan()
    ids = [n["id"] for n in g["nodes"]]
    paths = [n["path"] for n in g["nodes"]]
    assert not any("venv" in i or "ignored" in i for i in ids)
    assert not any("venv" in p or "ignored" in p for p in paths)


def test_ignore_appdata_and_dotdirs(tmp_path):
    (tmp_path / "keep.py").write_text("x = 1\n", encoding="utf-8")
    junk = tmp_path / "AppData" / "Roaming"
    junk.mkdir(parents=True)
    (junk / "secret.py").write_text("y = 2\n", encoding="utf-8")
    hidden = tmp_path / ".cache"
    hidden.mkdir()
    (hidden / "c.py").write_text("z = 3\n", encoding="utf-8")
    g = scan_repo(tmp_path)
    ids = [n["id"] for n in g["nodes"]]
    assert "keep.py" in ids
    assert not any("secret" in i or "AppData" in i or ".cache" in i for i in ids)


def test_scan_invalid_escape_does_not_warn(tmp_path):
    import warnings

    # File contents: x = "\d"  (invalid Python escape; graph still parses it)
    (tmp_path / "bad.py").write_bytes(b'x = "\\d"\n')
    with warnings.catch_warnings(record=True) as rec:
        warnings.simplefilter("always")
        g = scan_repo(tmp_path)
    assert not any(issubclass(w.category, SyntaxWarning) for w in rec)
    assert any(n["id"] == "bad.py" for n in g["nodes"])


def test_scan_fixture_shape():
    g = _scan()
    assert Path(g["root"]).is_absolute()
    assert isinstance(g["scanned_at"], float)
    assert {"root", "nodes", "edges", "scanned_at"} <= set(g)
    assert g["nodes"]
    assert g["edges"]
    for node in g["nodes"]:
        assert NODE_KEYS <= set(node)
        assert node["kind"] in {"file", "cluster"}
        assert isinstance(node["loc"], int)
        assert isinstance(node["deg"], int)
        assert "/" not in node["path"] or "\\" not in node["path"]
        assert "\\" not in node["id"]
        assert "\\" not in node["path"]
        assert "\\" not in node["dir"]
    for edge in g["edges"]:
        assert EDGE_KEYS <= set(edge)
        assert edge["kind"] in {"call", "import"}
        assert isinstance(edge["w"], int) and edge["w"] >= 1
    ids = {n["id"] for n in g["nodes"]}
    assert "sub/e.py" in ids
    assert "dir:sub" in ids
    cluster = next(n for n in g["nodes"] if n["id"] == "dir:sub")
    assert cluster["kind"] == "cluster"
    assert cluster["loc"] == 0
    assert cluster["path"] == "sub"
    file_a = next(n for n in g["nodes"] if n["id"] == "a.py")
    assert file_a["dir"] == ""
    assert file_a["kind"] == "file"
    assert file_a["loc"] > 0
