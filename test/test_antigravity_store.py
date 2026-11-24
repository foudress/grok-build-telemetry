"""Antigravity reads gen_metadata counts. Cache read is not inside input."""

import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from token_telemetry.antigravity import store
from token_telemetry.antigravity.adapt import build_period, state_for
from token_telemetry.antigravity.prices import price_usage, priced_model
from token_telemetry.antigravity.proto import parse_usage
from token_telemetry.antigravity.store import clear_cache, focused_snapshot, load_snapshot


def _list(uncached: int, cached: int, output: int) -> float:
    """Gemini 4 Hydrogen standard: $1.50 / $0.075 / $7.50 per 1M."""
    return (uncached * 1.50 + cached * 0.075 + output * 7.50) / 1_000_000


def _token_values(node: object) -> set[int]:
    found: set[int] = set()
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(key, str) and "token" in key and isinstance(value, int):
                found.add(value)
            found.update(_token_values(value))
    elif isinstance(node, list):
        for item in node:
            found.update(_token_values(item))
    return found


def _v(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def _msg(field: int, body: bytes) -> bytes:
    return _v((field << 3) | 2) + _v(len(body)) + body


def _vi(field: int, n: int) -> bytes:
    return _v((field << 3) | 0) + _v(n)


def _st(field: int, text: str) -> bytes:
    raw = text.encode()
    return _v((field << 3) | 2) + _v(len(raw)) + raw


def _usage(*, uncached: int, cached: int, output: int, thinking: int, visible: int, response: str) -> bytes:
    return b"".join([
        _vi(1, 1537),
        _vi(2, uncached),
        _vi(3, output),
        _vi(5, cached),
        _vi(6, 24),
        _vi(9, thinking),
        _vi(10, visible),
        _st(11, response),
    ])


def _gen(usage: bytes, model: str = "gemini-4-hydrogen", label: str = "Gemini 4 Hydrogen") -> bytes:
    chat = b"".join([_msg(4, usage), _st(19, model), _st(21, label)])
    return _msg(1, chat)


def _indices(steps: list[int]) -> bytes:
    return _msg(2, b"".join(_v(n) for n in steps))


def _user(text: str) -> tuple[int, bytes, bytes]:
    body = _st(2, text)
    payload = b"".join([_vi(1, 14), _vi(4, 3), _msg(19, body)])
    return 14, b"", payload


def _planner(epoch: int, *, thinking: str = "", response: str = "", tool: tuple[str, str] | None = None) -> tuple[int, bytes, bytes]:
    parts = []
    if response:
        parts.append(_st(1, response))
    if thinking:
        parts.append(_st(3, thinking))
    if tool:
        name, args = tool
        call = b"".join([_st(1, "call-1"), _st(2, name), _st(3, args), _st(7, "RESULT!!")])
        parts.append(_msg(7, call))
    meta = _msg(1, _vi(1, epoch))
    payload = b"".join([_vi(1, 15), _vi(4, 3), _msg(20, b"".join(parts))])
    return 15, meta, payload


def _tool(name: str, args: str) -> tuple[int, bytes, bytes]:
    call = b"".join([_st(1, "call-1"), _st(2, name), _st(3, args), _st(7, "RESULT!!")])
    return 132, _msg(4, call), b"".join([_vi(1, 132), _vi(4, 3)])


def _aux(kind: str, text: str) -> tuple[int, bytes, bytes]:
    body = b"".join([_st(1, text), _st(3, kind)])
    return 101, b"", b"".join([_vi(1, 101), _vi(4, 3), _msg(114, body)])


def _db(path: Path, gens: list[bytes], steps: list[tuple[int, bytes, bytes]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE gen_metadata (idx INTEGER PRIMARY KEY, data BLOB)")
    con.execute(
        "CREATE TABLE steps ("
        "idx INTEGER PRIMARY KEY, step_type INTEGER, metadata BLOB, step_payload BLOB)"
    )
    for i, blob in enumerate(gens):
        con.execute("INSERT INTO gen_metadata (idx, data) VALUES (?, ?)", (i, blob))
    for i, (kind, meta, payload) in enumerate(steps):
        con.execute(
            "INSERT INTO steps (idx, step_type, metadata, step_payload) VALUES (?, ?, ?, ?)",
            (i, kind, meta, payload),
        )
    con.commit()
    con.close()


def _home(tmp_path: Path) -> Path:
    root = tmp_path / "antigravity"
    conv = root / "conversations"
    epoch = int(datetime(2026, 11, 21, 12, 0, tzinfo=timezone.utc).timestamp())
    first = _usage(uncached=100, cached=50, output=30, thinking=10, visible=20, response="resp-1")
    smaller = _usage(uncached=1, cached=0, output=1, thinking=0, visible=1, response="resp-1")
    second = _usage(uncached=40, cached=150, output=10, thinking=0, visible=10, response="resp-2")
    third = _usage(uncached=5, cached=0, output=2, thinking=0, visible=2, response="resp-3")
    decoy = _msg(9, _usage(uncached=99999, cached=0, output=1, thinking=0, visible=1, response="resp-1"))
    _db(
        conv / "root-cascade.db",
        [
            _gen(first) + _indices([1, 2]),
            _gen(smaller),
            _gen(second) + _indices([4]),
            _gen(third) + _indices([6]),
        ],
        [
            _user("déjà look"),
            _planner(epoch, thinking="hmm", tool=("read_file", '{"path":"src/a.py"}')),
            _tool("read_file", '{"path":"src/a.py"}'),
            _aux("agent_message", "working"),
            _planner(epoch + 20, response="done"),
            _user("again"),
            _planner(epoch + 40, response="next"),
            (132, decoy, b""),
        ],
    )
    child = _usage(uncached=7, cached=0, output=1, thinking=0, visible=1, response="resp-child")
    _db(
        conv / "child-cascade.db",
        [_gen(child, model="gemini-flash-worker-agy", label="") + _indices([0])],
        [_planner(epoch + 30, tool=("edit_file", '{"path":"src/b.py"}'))],
    )
    con = sqlite3.connect(root / "conversation_summaries.db")
    con.execute(
        "CREATE TABLE conversation_summaries ("
        "conversation_id TEXT PRIMARY KEY, title TEXT, last_modified_time TEXT, "
        "workspace_uris TEXT, parent_conversation_id TEXT, agent_name TEXT)"
    )
    con.execute(
        "INSERT INTO conversation_summaries VALUES (?, ?, '', ?, '', '')",
        (
            "root-cascade",
            "UI work",
            '["file:///f%3A/Dev/app"]',
        ),
    )
    con.execute(
        "INSERT INTO conversation_summaries VALUES (?, '', '', '', ?, '')",
        ("child-cascade", "root-cascade"),
    )
    con.commit()
    con.close()
    return root


def test_public_model_rates() -> None:
    assert priced_model("Gemini 4 Hydrogen (High)") == "gemini-4-hydrogen"
    assert priced_model("gemini-flash-worker-agy") == "gemini-4-hydrogen"
    assert priced_model("Orchestrator (Neon)") == "gemini-4-neon"
    assert priced_model("gemini-4-argon-trial") == "gemini-4-argon"
    hydrogen = price_usage(
        "gemini-4-hydrogen", uncached=1_000_000, cached=1_000_000, output=1_000_000,
    )
    assert hydrogen["total"] == 1.50 + 0.075 + 7.50
    early = price_usage(
        "gemini-4-argon", uncached=1_000_000, cached=0, output=1_000_000,
        when="2026-11-21T00:00:00+00:00",
    )
    late = price_usage(
        "gemini-4-argon", uncached=1_000_000, cached=0, output=1_000_000,
        when="2026-12-01T00:00:00+00:00",
    )
    assert early["total"] == 12.0
    assert late["total"] == 24.0
    assert price_usage("not-a-model", uncached=10, cached=0, output=1)["priced"] is False


def test_orchestrator_prices_the_observed_model_and_wal_updates_the_snapshot(tmp_path: Path) -> None:
    root = _home(tmp_path)
    db = root / "conversations/root-cascade.db"
    with closing(sqlite3.connect(db)) as writer:
        writer.execute("PRAGMA journal_mode=WAL")
        first = focused_snapshot(root, "root-cascade")
        before = db.stat().st_mtime_ns
        generation = _gen(
            _usage(uncached=100, cached=50, output=20, thinking=0, visible=20, response="opus"),
            model="claude-opus-5.5-fast", label="Orchestrator (Neon)",
        )
        writer.execute("INSERT INTO gen_metadata (idx, data) VALUES (?, ?)", (999, generation))
        writer.commit()
        assert db.stat().st_mtime_ns == before
        refreshed = focused_snapshot(root, "root-cascade")
        assert refreshed["n_calls"] == first["n_calls"] + 1
        same_revision = focused_snapshot(root, "root-cascade")
        assert refreshed is same_revision
        state = state_for(refreshed, "root-cascade", pinned="root-cascade")
        previous = state_for(first, "root-cascade", pinned="root-cascade")
        delta = state["totals"]["estimate_usd"] - previous["totals"]["estimate_usd"]
        assert abs(delta - (100 * 8 + 50 * 0.40 + 20 * 40) / 1_000_000) < 1e-12


def test_antigravity_premium_and_unpriced_models() -> None:
    usage = {"uncached": 100, "cached": 50, "output": 20}
    assert price_usage("GPT-OSS 120B (Medium)", **usage)["priced"] is False
    standard = price_usage("Gemini 4 Neon", **usage)
    priority = price_usage("Gemini 4 Neon (Priority)", **usage)
    assert abs(priority["total"] - standard["total"] * 1.8) < 1e-12
    sonnet = price_usage("Claude Sonnet 5.5 Medium", cache_write=50, **usage)
    assert sonnet["model"] == "claude-sonnet-5-5"
    assert sonnet["write"] == 50 * 2.5 / 1_000_000


def test_usage_keeps_cache_read_out_of_uncached() -> None:
    usage = parse_usage(_usage(
        uncached=100, cached=50, output=30, thinking=10, visible=20, response="resp-1"
    ))
    assert usage is not None
    assert usage["uncached"] == 100
    assert usage["cached"] == 50
    assert usage["output"] == 30
    assert usage["reasoning"] == 10


def test_system_estimate_subtracts_full_first_query_from_total_input(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(store, "count_tokens", lambda text: len(text.split()))
    epoch = int(datetime(2026, 11, 21, 12, tzinfo=timezone.utc).timestamp())
    first = _usage(uncached=100, cached=50, output=20, thinking=0, visible=20, response="first")
    later = _usage(uncached=400, cached=40, output=10, thinking=0, visible=10, response="later")
    _db(
        tmp_path / "conversations/cascade.db",
        [_gen(first) + _indices([1]), _gen(later) + _indices([3])],
        [_user("instruction " * 70), _planner(epoch), _user("next"), _planner(epoch + 20)],
    )
    state = state_for(load_snapshot(tmp_path), "cascade", pinned="cascade")
    first_round, second_round = state["rounds"]
    estimate = first_round["system_estimate"]
    assert estimate["input_tokens"] == 150
    assert estimate["user_tokens"] == 70
    assert estimate["tokens"] == 80
    assert first_round["breakdown"]["tree_in_tokens"] == 100
    assert first_round["breakdown"]["cached_tokens"] == 50
    assert abs(first_round["estimate_usd"] - _list(100, 50, 20)) < 1e-12
    assert "system_estimate" not in second_round


def test_output_follows_thinking_plus_visible() -> None:
    raw = b"".join([
        _vi(2, 1),
        _vi(3, 5),
        _vi(9, 3),
        _vi(10, 3),
    ])
    usage = parse_usage(raw)
    assert usage is not None
    assert usage["output"] == 6
    assert usage["reasoning"] == 3


def test_snapshot_sums_generations_once(tmp_path: Path) -> None:
    clear_cache()
    home = _home(tmp_path)
    snap = load_snapshot(home)
    state = state_for(snap, "root-cascade", pinned="root-cascade")
    assert state["ledger_source"] == "antigravity"
    assert state["session_id"] == "root-cascade"
    assert state["source"].endswith("Dev/app")
    assert len(state["sessions"]) == 1
    assert len(state["rounds"]) == 2
    rnd = state["rounds"][0]
    bd = rnd["breakdown"]
    assert bd["tree_in_tokens"] == 140
    assert bd["cached_tokens"] == 200
    assert bd["output_tokens"] == 40
    assert bd["llm_reasoning_tokens"] == 10
    assert bd["user_in_tokens"] == 100
    assert bd["harness_in_tokens"] == 40
    assert rnd["context_end"] == 190
    assert rnd["user_prompt"]["preview"] == "déjà look"
    assert len(rnd["model_steps"]) == 2
    first = rnd["model_steps"][0]
    kinds = [ch["kind"] for ch in first["children"]]
    assert kinds == ["phase_llm", "phase_harness"]
    harness = first["children"][1]
    tools = [ch for ch in harness["children"] if ch.get("kind") == "tool"]
    assert tools[0]["name"] == "read_file"
    assert tools[0]["path"] == "src/a.py"
    assert tools[0]["tokens_in"] == 10
    assert "working" in str(first)
    assert "hmm" in str(first)
    assert "done" in str(rnd["model_steps"][1])
    nxt = state["rounds"][1]
    assert nxt["breakdown"]["tree_in_tokens"] == 5
    assert nxt["user_prompt"]["preview"] == "again"
    assert "next" in str(nxt)
    parent = _list(100, 50, 30) + _list(40, 150, 10) + _list(5, 0, 2)
    child = _list(7, 0, 1)
    assert state["totals"]["official_usd"] == 0
    assert abs(state["totals"]["estimate_usd"] - (parent + child)) < 1e-9
    assert abs(state["rounds"][0]["estimate_usd"] - (_list(100, 50, 30) + _list(40, 150, 10))) < 1e-9
    assert abs(state["rounds"][0]["breakdown"]["tree_in_usd"] - (_list(100, 0, 0) + _list(40, 0, 0))) < 1e-9
    assert state["pricing"]["model"] == "gemini-4-hydrogen"
    assert state["sessions"][0]["model"] == "Gemini 4 Hydrogen"
    assert state["sub_sessions"][0]["model"] == "gemini-flash-worker-agy"
    assert abs(state["sub_sessions"][0]["estimate_usd"] - child) < 1e-9
    assert 99999 not in _token_values(state["rounds"])
    assert len(state["sub_sessions"]) == 1
    child_round = state["sub_sessions"][0]["rounds"][0]
    assert child_round["breakdown"]["tree_in_tokens"] == 7
    assert "edit_file" in str(child_round)
    when = datetime(2026, 11, 21, 18, 0, tzinfo=timezone.utc)
    period = build_period(snap, now=when)
    assert period["ledger_source"] == "antigravity"
    assert period["totals"]["tokens_in"] == 152
    assert period["totals"]["tokens_cached"] == 200
    assert period["totals"]["tokens_out"] == 43
    assert period["totals"]["tokens_reason"] == 10
    assert abs(period["totals"]["estimate_usd"] - (parent + child)) < 1e-9
    again = load_snapshot(home)
    assert again["n_calls"] == snap["n_calls"]
