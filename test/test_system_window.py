"""R1 System card = history + window remainder (not Σ off_unc, not 8.2k)."""

from __future__ import annotations

import json

from token_telemetry.hierarchy.bootstrap import (
    _classify_bootstrap_message,
    _is_compact_continuation,
    _session_is_subagent,
    parse_session_bootstrap,
)
from token_telemetry.hierarchy.breakdown import _merge_bootstrap_into_breakdown
from token_telemetry.hierarchy.tooldef_r1 import (
    _estimate_tooldef_message_bucket,
    _inject_system_message_residual,
    _is_cold_session_first,
    _is_first_round,
    stamp_r1_tooldef_jump,
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
    def __init__(self, rounds=None):
        self.rounds = list(rounds or [])


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
    assert "tool_definitions" in kinds
    assert "message" not in kinds
    assert "tool_defs_message" not in kinds
    assert "unknown" not in kinds
    labels = [p.get("label") for p in sp["parts"] if p.get("kind") == "tool_definitions"]
    assert labels == ["Tool Definitions"]
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
    assert "unknown" not in kinds


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


def test_packed_card_quoting_compact_is_still_user_prompt():
    """Workflow Super Agent prompts embed a prior-session summary — not glue."""
    text = (
        "Implement ONLY the packed card at .grok/state/cards_foudres/fix-x.\n"
        + ("body " * 200)
        + "\nThis session is being continued from a previous conversation "
        "that ran out of context.\nThe summary is below."
    )
    assert not _is_compact_continuation(text)
    assert _classify_bootstrap_message("user", text, None, subagent=True) == (
        "user_prompt"
    )


def test_classify_no_start_user_query_stays_user_prompt():
    assert _classify_bootstrap_message(
        "user", "<user_query>no slash start</user_query>", None
    ) == "user_prompt"
    skills = (
        "<system-reminder>Skills are available. "
        "Use the skill tool when needed.</system-reminder>"
    )
    assert _classify_bootstrap_message("user", skills, "system_reminder") == "reminders"
    mcp = "<system-reminder>MCP servers are configured.</system-reminder>"
    assert _classify_bootstrap_message("user", mcp, None) == "mcp"


def test_untagged_parent_task_is_other_on_main_super_agent_on_sub():
    task = "You are a pragmatic implementer. Implement code changes."
    assert _classify_bootstrap_message("user", task, None) == "other"
    assert _classify_bootstrap_message("user", task, None, subagent=True) == (
        "user_prompt"
    )


def test_session_is_subagent_includes_resume(tmp_path):
    d = tmp_path / "kid"
    d.mkdir()
    (d / "summary.json").write_text(
        json.dumps({"session_kind": "subagent_resume"}), encoding="utf-8"
    )
    assert _session_is_subagent(d) is True
    (d / "summary.json").write_text(
        json.dumps({"session_kind": "subagent"}), encoding="utf-8"
    )
    assert _session_is_subagent(d) is True
    (d / "summary.json").write_text(
        json.dumps({"session_kind": "session"}), encoding="utf-8"
    )
    assert _session_is_subagent(d) is False


def test_resume_untagged_task_lands_on_user_not_system_other(tmp_path):
    """Tabs show subagent_resume. Untagged parent task must not stay System Other."""
    d = tmp_path / "resume"
    d.mkdir()
    (d / "summary.json").write_text(
        json.dumps({"session_kind": "subagent_resume"}), encoding="utf-8"
    )
    hist = [
        {
            "type": "system",
            "content": "You are a Grok Build subagent — a focused worker.",
        },
        {
            "type": "user",
            "content": "<system-reminder>MCP servers are configured.</system-reminder>",
        },
        {
            "type": "user",
            "content": "You are a pragmatic implementer. Implement code changes.",
        },
    ]
    (d / "chat_history.jsonl").write_text(
        "\n".join(json.dumps(o) for o in hist) + "\n", encoding="utf-8"
    )
    boot = parse_session_bootstrap(d)
    kinds = [p.get("kind") for p in (boot.get("parts") or [])]
    assert "other" not in kinds
    assert int(boot.get("user_tokens") or 0) > 0
    assert "pragmatic implementer" in (boot.get("user_preview") or "")


def test_tagged_user_query_still_user_prompt_on_resume(tmp_path):
    d = tmp_path / "resume"
    d.mkdir()
    (d / "summary.json").write_text(
        json.dumps({"session_kind": "subagent_resume"}), encoding="utf-8"
    )
    hist = [
        {
            "type": "system",
            "content": "You are a Grok Build subagent — a focused worker.",
        },
        {
            "type": "user",
            "content": "<user_query>You are a pragmatic implementer.</user_query>",
        },
    ]
    (d / "chat_history.jsonl").write_text(
        "\n".join(json.dumps(o) for o in hist) + "\n", encoding="utf-8"
    )
    boot = parse_session_bootstrap(d)
    kinds = [p.get("kind") for p in (boot.get("parts") or [])]
    assert "other" not in kinds
    assert int(boot.get("user_tokens") or 0) > 0


def test_main_session_untagged_stays_system_other(tmp_path):
    d = tmp_path / "main"
    d.mkdir()
    (d / "summary.json").write_text(
        json.dumps({"session_kind": "session"}), encoding="utf-8"
    )
    hist = [
        {"type": "system", "content": "You are Grok."},
        {"type": "user", "content": "loose note without tags"},
    ]
    (d / "chat_history.jsonl").write_text(
        "\n".join(json.dumps(o) for o in hist) + "\n", encoding="utf-8"
    )
    boot = parse_session_bootstrap(d)
    kinds = [p.get("kind") for p in (boot.get("parts") or [])]
    assert "other" in kinds
    assert int(boot.get("user_tokens") or 0) == 0


def test_no_start_floors_remainder_with_bootstrap_residual():
    """01a01ebe-class: stream 3808, official input 13916 → remainder ~10108."""
    hist = [
        {"kind": "system", "label": "System", "tokens": 1219},
        {"kind": "user_info", "label": "User info", "tokens": 428},
        {"kind": "reminders", "label": "Reminders / skills catalog", "tokens": 1663},
        {"kind": "mcp", "label": "MCP", "tokens": 99},
    ]
    user_in = 399
    r = _r1(context_end=3808, hist=hist, user_in=user_in, call_ins=[0])
    r["usage_raw"] = {
        "inputTokens": 13916,
        "cachedReadTokens": 128,
        "outputTokens": 30,
    }
    recon = {"bootstrap_residual_tokens": 10108}
    _inject_system_message_residual(_HB(), r, recon)
    hist_sum = 1219 + 428 + 1663 + 99
    tree = user_in + 0
    bucket = 10108
    assert r["system_prompt"]["message_residual_tokens"] == bucket
    assert r["system_prompt"]["message_residual_tokens"] > 0
    assert r["system_prompt"]["tokens_in"] == hist_sum + bucket
    assert r["context_end"] == hist_sum + bucket + tree
    assert r["system_prompt"]["tokens_in"] + tree == r["context_end"]
    assert r["context_end"] == 13916
    kinds = [p["kind"] for p in r["system_prompt"]["parts"]]
    assert "tool_definitions" in kinds
    assert "message" not in kinds
    assert "unknown" not in kinds
    assert "tool_defs_message" not in kinds
    assert 8200 not in (
        r["system_prompt"]["message_residual_tokens"],
        r["system_prompt"]["tokens_in"],
    )
    # Never Σ off_unc (13916 − 128) onto Message
    assert r["system_prompt"]["message_residual_tokens"] != 13788


def test_no_start_empty_recon_uses_stream_formula_only():
    hist = [
        {"kind": "system", "tokens": 1219},
        {"kind": "user_info", "tokens": 428},
        {"kind": "reminders", "tokens": 1663},
        {"kind": "mcp", "tokens": 99},
    ]
    r = _r1(context_end=3808, hist=hist, user_in=399, call_ins=[0])
    _inject_system_message_residual(_HB(), r, {})
    # Lagging stream end → stream formula ~0; recon floor happens on 2nd call
    assert r["system_prompt"]["message_residual_tokens"] == 0
    _inject_system_message_residual(
        _HB(), r, {"bootstrap_residual_tokens": 10108}
    )
    assert r["system_prompt"]["message_residual_tokens"] == 10108
    assert r["system_prompt"]["tokens_in"] + 399 == r["context_end"]


def test_cold_one_call_floors_bucket_with_off_in():
    boot = {
        "user_tokens": 399,
        "parts": [
            {"kind": "system", "tokens": 1219},
            {"kind": "user_info", "tokens": 428},
            {"kind": "reminders", "tokens": 1663},
            {"kind": "mcp", "tokens": 99},
        ],
    }
    steps = [
        {
            "harness_pool_tokens": 0,
            "stream_context_raw": 3808,
            "context_start": 3808,
        }
    ]
    r = {
        "context_end": 3808,
        "usage_raw": {
            "inputTokens": 13916,
            "cachedReadTokens": 128,
        },
    }
    got = _estimate_tooldef_message_bucket(r, boot, steps)
    # 1-call jump==stream end: remainder is System residual, not Call-1 ToolDef bump.
    hist = 1219 + 428 + 1663 + 99
    assert got == max(0, 3808 - 399 - 0 - hist)
    assert got == 0
    assert got != 13916 - 128  # never Σ off_unc
    assert got != 10108


def test_split_tooldef_and_message_when_independent():
    r = _r1()
    r["tool_definitions"] = {
        "tokens": 8200,
        "independent_tokens": 3000,
        "count": 25,
        "source": "env",
    }
    _inject_system_message_residual(_HB(), r, {})
    tree = 3741 + 4522 + 1563 + 355 + 355
    hist = 1219 + 428 + 1663 + 99
    bucket = 22516 - tree - hist
    sp = r["system_prompt"]
    assert sp["message_residual_tokens"] == bucket
    assert sp["tool_definitions_tokens"] == bucket
    kinds = [p["kind"] for p in sp["parts"]]
    assert "tool_definitions" in kinds
    assert "message" not in kinds
    assert "unknown" not in kinds
    assert "tool_defs_message" not in kinds
    by_kind = {p["kind"]: p["tokens"] for p in sp["parts"]}
    assert by_kind["tool_definitions"] == bucket
    assert by_kind["system"] == 1219
    assert by_kind["user_info"] == 428
    assert sp["tokens_in"] + tree == 22516


def test_inject_uses_first_tool_call_jump_not_round_peak():
    """01a068 call-6808da03… totalTokens 4523 → ToolDef ~2533 not ~26.6k."""
    hist_tok = 1990
    r = {
        "index": 1,
        "context_end": 26600,
        "system_prompt": {
            "kind": "system_prompt",
            "tokens_in": hist_tok,
            "logical_tokens": hist_tok,
            "parts": [{"kind": "system", "label": "System", "tokens": hist_tok}],
        },
        "user_prompt": {"kind": "user_prompt", "tokens_in": 0, "uncached_est": 0},
        "model_steps": [
            {
                "totalTokens": 4523,
                "stream_context_raw": 4523,
                "context_start": 4523,
                "tokens_in": 0,
            }
        ],
        "session_bootstrap": {
            "system_tokens": hist_tok,
            "parts": [{"kind": "system", "tokens": hist_tok}],
        },
        "breakdown": {},
        "step_usage": {"breakdown": {}, "totals": {}},
    }
    _inject_system_message_residual(_HB(), r, {})
    sp = r["system_prompt"]
    # Jump high-side is 4523, not round peak 26600. ToolDef sits on System.
    assert sp["message_residual_tokens"] == 2533
    assert sp["tool_definitions_tokens"] == 2533
    kinds = [p["kind"] for p in sp["parts"]]
    assert "tool_definitions" in kinds
    assert "message" not in kinds
    assert "unknown" not in kinds
    assert sp["tokens_in"] == hist_tok + 2533
    assert sp["tokens_in"] != 26600
    by_kind = {p["kind"]: p["tokens"] for p in sp["parts"]}
    assert by_kind["tool_definitions"] == 2533


def test_first_round_detection_survives_re_finalize():
    r1 = {"index": 1, "system_prompt": {"kind": "system_prompt"}}
    r2 = {"index": 2}
    hb = _HB([r1])
    assert _is_first_round(hb, r1)
    hb.rounds = [r1, r2]
    assert _is_first_round(hb, r1)
    assert not _is_first_round(hb, r2)
    wiped = {"index": 1}
    hb.rounds = [wiped, r2]
    assert _is_first_round(hb, wiped)
    assert _is_first_round(
        _HB([r2]), {"index": 2, "system_prompt": {"kind": "system_prompt"}}
    )
    assert not _is_first_round(_HB([r1, r2]), {"index": 2})
    assert not _is_first_round(_HB(), {"index": 2})


def test_pruned_list_head_is_not_session_first():
    r = {"index": 13, "cache_baseline_at_start": 8000}
    hb = _HB([r, {"index": 14, "cache_baseline_at_start": 9000}])
    assert not _is_first_round(hb, r)
    assert not _is_cold_session_first(r)
    r1 = {"index": 1, "cache_baseline_at_start": None}
    assert _is_first_round(hb, r1)
    assert _is_cold_session_first(r1)
    warm_r1 = {"index": 1, "cache_baseline_at_start": 8000}
    assert _is_first_round(hb, warm_r1)
    assert not _is_cold_session_first(warm_r1)


def test_stamp_jump_ignores_flat_completed_then_catches_later_declare():
    """01a044ac-class: first tools complete at LLM tt, jump on a later tool_call."""
    r = {"index": 1, "cache_baseline_at_start": None}
    stamp_r1_tooldef_jump(r, "agent_thought_chunk", 1653)
    stamp_r1_tooldef_jump(r, "agent_message_chunk", 1653)
    stamp_r1_tooldef_jump(r, "tool_call", 1653)
    stamp_r1_tooldef_jump(r, "tool_call_update", 1653)
    stamp_r1_tooldef_jump(r, "tool_call", 1653)
    stamp_r1_tooldef_jump(r, "tool_call_update", 1653)
    stamp_r1_tooldef_jump(r, "tool_call", 13762)
    assert r["first_llm_total_tokens"] == 1653
    assert r["tooldef_high_total_tokens"] == 13762


def test_stamp_jump_on_first_pending_tool_call():
    """Some sessions jump on the first tool_call (no completed-at-same-tt lag)."""
    r = {"index": 1, "cache_baseline_at_start": None}
    stamp_r1_tooldef_jump(r, "agent_thought_chunk", 4226)
    stamp_r1_tooldef_jump(r, "tool_call", 14983)
    assert r["first_llm_total_tokens"] == 4226
    assert r["tooldef_high_total_tokens"] == 14983


def test_stamp_jump_skips_warm_rounds():
    r = {"index": 2, "cache_baseline_at_start": 8000}
    stamp_r1_tooldef_jump(r, "agent_thought_chunk", 8000)
    stamp_r1_tooldef_jump(r, "tool_call", 20000)
    assert "first_llm_total_tokens" not in r
    assert "tooldef_high_total_tokens" not in r


def test_lag_jump_tooldef_on_system_not_round_peak():
    hist = 3100
    user = 353
    r = {
        "index": 1,
        "context_end": 40000,
        "first_llm_total_tokens": 1653,
        "tooldef_high_total_tokens": 13762,
        "system_prompt": {
            "kind": "system_prompt",
            "tokens_in": hist,
            "logical_tokens": hist,
            "parts": [{"kind": "system", "label": "System", "tokens": hist}],
        },
        "user_prompt": {"kind": "user_prompt", "tokens_in": user, "uncached_est": user},
        "model_steps": [
            {
                "stream_context_raw": 1653,
                "context_start": 1653,
                "tokens_in": 12109,
                "thought_chars": 0,
                "message_chars": 0,
                "tools": [
                    {"declare_ctx": 1653, "arg_chars": 0, "result_chars": 0},
                    {"declare_ctx": 13762, "arg_chars": 0, "result_chars": 0},
                ],
            }
        ],
        "session_bootstrap": {
            "user_tokens": user,
            "system_tokens": hist,
            "parts": [{"kind": "system", "tokens": hist}],
        },
        "breakdown": {},
        "step_usage": {"breakdown": {}, "totals": {}},
    }
    boot = r["session_bootstrap"]
    got = _estimate_tooldef_message_bucket(r, boot, r["model_steps"])
    assert got == 13762 - hist - user
    assert got == 10309
    assert got != 40000 - hist - user
    _inject_system_message_residual(_HB(), r, {})
    sp = r["system_prompt"]
    assert sp["tool_definitions_tokens"] == got
    assert "message" not in [p["kind"] for p in sp["parts"]]
    assert r["model_steps"][0]["tokens_in"] == 12109 - got
    assert r["model_steps"][0]["tooldef_peeled_from_call"] == got


def test_same_tools_same_high_minus_hist_user():
    """Two cold sessions with the same jump high-side and hist converge."""
    def one(high, hist, user, llm):
        boot = {
            "user_tokens": user,
            "parts": [{"kind": "system", "tokens": hist}],
        }
        steps = [
            {
                "stream_context_raw": llm,
                "context_start": llm,
                "tools": [{"declare_ctx": high, "arg_chars": 0}],
            }
        ]
        r = {
            "first_llm_total_tokens": llm,
            "tooldef_high_total_tokens": high,
            "context_end": high + 8000,
        }
        return _estimate_tooldef_message_bucket(r, boot, steps)

    a = one(13762, 3100, 353, 1653)
    b = one(13762, 3100, 353, 1653)
    assert a == b
    assert a == 13762 - 3100 - 353
