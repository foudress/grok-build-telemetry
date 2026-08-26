"""streamStart → last tool-request tok/s; TokF only, never enc blob / sub-agent."""

from token_telemetry.hierarchy.builder import HierarchyBuilder
from token_telemetry.hierarchy.gen_rate import (
    attach_gen_rates,
    call_out_tokf,
    mean_gen_rate,
    stream_out_tokens,
)


def test_call_window_is_stream_start_to_last_receive():
    r = {
        "last_user_ms": 1000,
        "started_ms": 1000,
        "completed_ms": 5400,
        "model_steps": [
            {
                "index": 1,
                "stream_start_ms": 1100,
                "started_ms": 1100,
                "first_llm_ms": 1100,
                "last_llm_ms": 1300,
                "first_tool_ms": 1400,
                "last_tool_request_ms": 1400,
                "last_tool_ms": 5000,
                "thought_summary_tokens": 30,
                "tokens_out": 30,
            },
            {
                "index": 2,
                "stream_start_ms": 5100,
                "started_ms": 5100,
                "first_llm_ms": 5100,
                "last_llm_ms": 5300,
                "thought_summary_tokens": 70,
                "tokens_out": 70,
            },
        ],
    }
    attach_gen_rates(r)
    c1, c2 = r["model_steps"]
    # LLM process = this call streamStart → last tool request, not user_submit
    # and not previous tool *execution* end (5000).
    assert c1["prompt_start_ms"] == 1100
    assert c1["gen_ms"] == 300
    assert c1["gen_tokens_per_sec"] == 100.0
    assert c1["response_end_ms"] == 1400
    assert c1["response_end_ms"] != 5000
    assert c2["prompt_start_ms"] == 5100
    assert c2["gen_ms"] == 200
    assert c2["gen_tokens_per_sec"] == 350.0
    assert r["gen_tokens_per_sec"] == 225.0


def test_does_not_use_round_completed_when_tools_ran():
    r = {
        "user_submit_ms": 1000,
        "started_ms": 1000,
        "completed_ms": 20_000,
        "model_steps": [
            {
                "index": 1,
                "stream_start_ms": 1000,
                "started_ms": 1000,
                "last_llm_ms": 2000,
                "first_tool_ms": 2100,
                "last_tool_request_ms": 2100,
                "last_tool_ms": 19_000,
                "thought_summary_tokens": 100,
            }
        ],
    }
    attach_gen_rates(r)
    s = r["model_steps"][0]
    assert s["gen_ms"] == 1100
    assert s["response_end_ms"] == 2100
    assert s["gen_tokens_per_sec"] == round(100 / 1.1, 3)


def test_tokf_not_encrypted_blob():
    step = {
        "thought_summary_tokens": 8,
        "thought_encrypted_tokens": 6045,
        "message_tokens": 0,
        "estimate": {
            "output_tokens": 89,
            "output_thought_tokens": 8,
            "output_reasoning_tokens": 81,  # leftover Enc after peel
            "output_message_tokens": 0,
            "output_emit_tokens": 0,
        },
    }
    assert call_out_tokf(step) == 89  # 8+81, never enc blob 6045
    assert stream_out_tokens(step) == 89


def test_peel_tokf_is_thought_plus_leftover_enc():
    """thought_F + reasoning_F + message_F = billed Out; never enc TokZ."""
    step = {
        "thought_summary_tokens": 22,
        "thought_encrypted_tokens": 130,
        "message_tokens": 1,
        "estimate": {
            "output_tokens": 30,
            "output_thought_tokens": 22,
            "output_reasoning_tokens": 7,
            "output_message_tokens": 1,
            "output_emit_tokens": 0,
        },
    }
    assert call_out_tokf(step) == 30
    r = {
        "model_steps": [
            {
                **step,
                "stream_start_ms": 1000,
                "first_llm_ms": 1000,
                "last_llm_ms": 2000,
            }
        ],
    }
    attach_gen_rates(r)
    s = r["model_steps"][0]
    assert s["gen_out_tokens"] == 30
    assert s["gen_ms"] == 1000
    assert s["gen_tokens_per_sec"] == 30.0


def test_ignores_official_out_subagent_dump():
    step = {
        "tokens_out": 48_000,
        "estimate": {"output_tokens": 48_000, "output_reasoning_tokens": 47_000},
        "thought_summary_tokens": 40,
        "thought_encrypted_tokens": 200,
        "message_tokens": 12,
        "tools": [{"name": "grep", "arg_tokens_est": 8}],
    }
    # 47k residual > enc stamp → drop reasoning (never use enc TokZ)
    assert call_out_tokf(step) == 60
    r = {
        "last_user_ms": 0,
        "started_ms": 1000,
        "model_steps": [
            {
                **step,
                "stream_start_ms": 1800,
                "first_llm_ms": 1800,
                "last_llm_ms": 2000,
            }
        ],
    }
    attach_gen_rates(r)
    s = r["model_steps"][0]
    assert s["gen_out_tokens"] == 60
    assert s["gen_ms"] == 200
    assert s["gen_tokens_per_sec"] == 300.0
    assert s["prompt_start_ms"] == 1800


def test_own_enc_stamp_caps_merged_extra_reasonings():
    """Last-call extra history rows must not unlock official leftover Out."""
    step = {
        "thought_summary_tokens": 7,
        "thought_encrypted_tokens": 5000,
        "thought_encrypted_tokens_own": 58,
        "message_tokens": 244,
        "estimate": {
            "output_thought_tokens": 7,
            "output_reasoning_tokens": 5968,
            "output_message_tokens": 244,
            "output_emit_tokens": 0,
            "output_tokens": 6212,
        },
        "tokens_out": 6212,
        "stream_start_ms": 1000,
        "last_llm_ms": 6511,
    }
    assert call_out_tokf(step) == 7 + 244
    r = {"model_steps": [step]}
    attach_gen_rates(r)
    s = r["model_steps"][0]
    assert s["gen_out_tokens"] == 251
    assert s["gen_ms"] == 5511
    assert s["gen_tokens_per_sec"] == round(251 / 5.511, 3)
    assert s["gen_tokens_per_sec"] < 100


def test_caps_parts_at_billed_out_tokf():
    """Thought TokZ + Enc residual must not exceed this call's billed Out."""
    step = {
        "estimate": {
            "output_thought_tokens": 236,
            "output_reasoning_tokens": 314,
            "output_message_tokens": 0,
            "output_emit_tokens": 0,
            "output_tokens": 314,
        },
        "thought_encrypted_tokens_own": 5003,
    }
    assert call_out_tokf(step) == 314


def test_no_1ms_spike_when_timestamps_collapse():
    r = {
        "last_user_ms": 5000,
        "model_steps": [
            {
                "stream_start_ms": 5000,
                "first_llm_ms": 5000,
                "last_llm_ms": 5000,
                "thought_summary_tokens": 80,
                "tokens_out": 80,
            }
        ],
    }
    attach_gen_rates(r)
    s = r["model_steps"][0]
    assert s["gen_ms"] is None
    assert s["gen_tokens_per_sec"] is None


def test_does_not_use_first_visible_chunk_as_start():
    """Hidden reasoning lives between streamStart and first thought chunk."""
    r = {
        "user_submit_ms": 1000,
        "model_steps": [
            {
                "stream_start_ms": 1100,
                "first_llm_ms": 80_000,
                "last_llm_ms": 80_800,
                "estimate": {
                    "output_thought_tokens": 10,
                    "output_reasoning_tokens": 80,
                    "output_message_tokens": 0,
                    "output_emit_tokens": 0,
                },
                "thought_encrypted_tokens": 4000,
            }
        ],
    }
    attach_gen_rates(r)
    s = r["model_steps"][0]
    assert s["prompt_start_ms"] == 1100
    assert s["gen_ms"] == 79_700
    assert s["gen_out_tokens"] == 90
    assert s["gen_tokens_per_sec"] == round(90 / 79.7, 3)
    assert s["gen_tokens_per_sec"] < 200


def test_session_mean_is_mean_of_rounds():
    rounds = [
        {"gen_tokens_per_sec": 10.0},
        {"gen_tokens_per_sec": 30.0},
        {"index": 3},
    ]
    assert mean_gen_rate(rounds) == 20.0


def _raw(kind, *, tt=100, stream=1000, agent=1000, pid="p1", **update):
    u = {"sessionUpdate": kind, **update}
    meta = {
        "promptId": pid,
        "streamStartMs": stream,
        "agentTimestampMs": agent,
        "totalTokens": tt,
    }
    return {"params": {"update": u, "_meta": meta}}


def test_builder_stamps_prompt_and_tool_clocks():
    hb = HierarchyBuilder(max_rounds=8)
    hb.feed_raw(_raw("user_message_chunk", agent=1000, stream=1, content={"text": "hi"}))
    hb.feed_raw(_raw("agent_thought_chunk", agent=1200, stream=1100, content={"text": "t"}))
    hb.feed_raw(_raw("agent_thought_chunk", agent=1500, stream=1100, content={"text": "t2"}))
    hb.feed_raw(
        _raw(
            "tool_call",
            agent=1600,
            stream=1100,
            toolCallId="c1",
            title="grep",
        )
    )
    hb.feed_raw(
        _raw(
            "tool_call_update",
            agent=8000,
            stream=1100,
            toolCallId="c1",
            status="completed",
            title="grep",
        )
    )
    hb.feed_raw(_raw("agent_thought_chunk", agent=8200, stream=8100, content={"text": "ok"}))
    hb.feed_raw(
        _raw(
            "turn_completed",
            agent=8300,
            stream=8100,
            usage={
                "inputTokens": 10,
                "outputTokens": 8,
                "cachedReadTokens": 0,
                "modelCalls": 2,
                "apiDurationMs": 50_000,
            },
        )
    )
    assert hb.rounds
    r = hb.rounds[-1]
    assert r.get("last_user_ms") == 1000
    steps = r.get("model_steps") or []
    assert len(steps) >= 2
    s0, s1 = steps[0], steps[1]
    assert s0.get("last_llm_ms") == 1500
    assert s0.get("first_tool_ms") == 1600
    assert s0.get("last_tool_request_ms") == 1600
    assert s0.get("last_tool_ms") == 8000
    assert s1.get("last_llm_ms") == 8200
    attach_gen_rates(r)
    assert s0.get("prompt_start_ms") == 1100
    assert s0.get("response_end_ms") == 1600
    assert s0.get("gen_ms") == 500
    # next call starts at *its* streamStart, not previous tool completion
    assert s1.get("prompt_start_ms") == 8100
    assert s1.get("gen_ms") == 100
