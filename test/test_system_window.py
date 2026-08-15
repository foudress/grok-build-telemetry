"""R1 System card = history + window remainder (not Σ off_unc, not 8.2k)."""

from __future__ import annotations

from token_telemetry.hierarchy.bootstrap import (
    _classify_bootstrap_message,
    _is_compact_continuation,
)
from token_telemetry.hierarchy.finalize import (
    _inject_system_message_residual,
    _merge_bootstrap_into_breakdown,
)


def _r1(
    *,
    context_end: int = 22516,
    hist=None,
    user_in: int = 3741,
    call_ins=None,
    extra_parts=None,
) -> dict:
    parts = list(
        hist
        or [
            {"kind": "system", "label": "System", "tokens": 1219},
            {"kind": "user_info", "label": "User info", "tokens": 428},
            {"kind": "reminders", "label": "Reminders / skills catalog", "tokens": 1663},
            {"kind": "mcp", "label": "MCP", "tokens": 99},
        ]
    )
    if extra_parts:
        parts.extend(extra_parts)
    hist_sum = sum(int(p["tokens"]) for p in parts if p.get("kind") not in (
        "message", "hooks", "tool_definitions", "tool_defs_message"
    ))
    steps = [{"tokens_in": n} for n in (call_ins or [4522, 1563, 355, 355, 0])]
    return {
        "index": 1,
        "context_end": context_end,
        "context_start": 15350,
        "output_tokens": 1381,
        "system_prompt": {
            "kind": "system_prompt",
            "tokens_in": hist_sum + 8200,
            "logical_tokens": hist_sum + 8200,
            "parts": parts
            + [
                {
                    "kind": "tool_definitions",
                    "label": "Tool definitions",
                    "tokens": 8200,
                }
            ],
        },
        "user_prompt": {"kind": "user_prompt", "tokens_in": user_in, "uncached_est": user_in},
        "model_steps": steps,
        "tool_definitions": {"tokens": 8200, "count": 25, "source": "default"},
        "session_bootstrap": {
            "system_tokens": hist_sum + 8200,
            "parts": list(parts)
            + [{"kind": "tool_definitions", "tokens": 8200}],
        },
        # Poison: old formula used this SUM and produced Message=6399.
        "usage_raw": {
            "inputTokens": 104576,
            "cachedReadTokens": 76032,
            "outputTokens": 1381,
        },
        "breakdown": {},
        "step_usage": {"breakdown": {}, "totals": {}},
    }


class _HB:
    pass


def test_window_identity_not_official_uncached():
    r = _r1()
    _inject_system_message_residual(_HB(), r, {})
    tree = 3741 + 4522 + 1563 + 355 + 355
    hist = 1219 + 428 + 1663 + 99
    bucket = 22516 - tree - hist
    assert bucket == 8571
    sp = r["system_prompt"]
    assert sp["tokens_in"] == hist + bucket
    assert sp["tokens_in"] + tree == 22516
    assert sp["message_residual_tokens"] == bucket
    assert sp["tool_definitions_tokens"] == bucket
    kinds = [p["kind"] for p in sp["parts"]]
    assert "tool_definitions" not in kinds
    assert "message" not in kinds
    assert "tool_defs_message" in kinds
    # Must not park multi-call Σ leftover (old 6399)
    assert sp["message_residual_tokens"] != 6399
    assert sp["tokens_in"] != 18008


def test_peel_start_equals_system_no_out():
    r = _r1()
    _inject_system_message_residual(_HB(), r, {})
    r["breakdown"]["tree_in_tokens"] = 10536
    r["breakdown"]["output_tokens"] = 1381
    r["step_usage"]["breakdown"] = r["breakdown"]
    _merge_bootstrap_into_breakdown(_HB(), r)
    # Round line is ctx 0 → end (nothing cached before Call 1).
    assert r["context_start"] == 0
    assert r["context_delta"] == 22516
    assert r["system_prompt"]["tokens_in"] + 10536 == 22516


def test_overshoot_history_zero_bucket():
    r = _r1(
        context_end=5000,
        hist=[{"kind": "system", "tokens": 4000}],
        user_in=2000,
        call_ins=[500],
    )
    _inject_system_message_residual(_HB(), r, {})
    assert r["system_prompt"]["message_residual_tokens"] == 0
    assert r["system_prompt"]["tokens_in"] == 4000
    kinds = [p["kind"] for p in r["system_prompt"]["parts"]]
    assert "tool_defs_message" not in kinds
    assert "tool_definitions" not in kinds


def test_missing_context_end_no_invent():
    r = _r1()
    r["context_end"] = None
    _inject_system_message_residual(_HB(), r, {})
    assert r["system_prompt"]["message_residual_tokens"] == 0
    assert r["system_prompt"]["tokens_in"] == 1219 + 428 + 1663 + 99
    kinds = [p["kind"] for p in r["system_prompt"]["parts"]]
    assert "tool_definitions" not in kinds


def test_compact_glue_not_user_or_other():
    glue = (
        "This session is being continued from a previous conversation "
        "that ran out of context."
    )
    assert _is_compact_continuation(glue)
    assert _classify_bootstrap_message("user", glue, None) is None
    assert _classify_bootstrap_message("user", "<user_query>hello</user_query>", None) == (
        "user_prompt"
    )
