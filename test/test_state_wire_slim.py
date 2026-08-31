"""Wire slim + ETag helpers for /api/state RAM control."""

from token_telemetry.session.monitor import _slim_wire


def test_slim_wire_drops_nulls_and_user_text():
    raw = {
        "user_prompt": {
            "kind": "user_prompt",
            "preview": "hi",
            "user_text": "full prompt body " * 40,
            "tokens_in": 12,
            "extra": None,
        },
        "keep_zero": 0,
        "keep_false": False,
        "drop_me": None,
    }
    out = _slim_wire(raw)
    assert "drop_me" not in out
    assert out["keep_zero"] == 0
    assert out["keep_false"] is False
    up = out["user_prompt"]
    assert "user_text" not in up
    assert up["preview"] == "hi"
    assert up["tokens_in"] == 12
    assert "extra" not in up


def test_slim_wire_plan_keeps_step_status_only():
    raw = {
        "kind": "tool",
        "plan": {
            "is_plan": True,
            "mode": "modify",
            "steps": [
                {"n": 1, "status": "completed", "content": "huge " * 200},
                {"id": 2, "status": "pending", "content": "more"},
            ],
        },
    }
    out = _slim_wire(raw)
    plan = out["plan"]
    assert plan["is_plan"] is True
    assert plan["mode"] == "modify"
    assert plan["step_count"] == 2
    assert plan["steps"] == [
        {"n": 1, "status": "completed"},
        {"n": 2, "status": "pending"},
    ]
    assert "content" not in plan["steps"][0]
