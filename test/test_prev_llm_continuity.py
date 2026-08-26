"""Continuity: prev LLM answer counts in next-round User In."""
from token_telemetry.hierarchy.cache_miss import _attach_prev_llm_answer


class _HB:
    def __init__(self, rounds):
        self.rounds = rounds


def test_continuity_answer_added_to_user_tokens_in():
    prev = {
        "index": 1,
        "completed": True,
        "model_steps": [
            {
                "index": 1,
                "thought_summary_tokens": 40,
                "message_tokens": 60,
                "tokens_out": 200,
                "cost_out_usd": 0.001,
                "composition": {"reasoning_encrypted_out": 100},
                "estimate": {"output_reasoning_tokens": 100, "output_tokens": 200},
                "children": [],
            }
        ],
    }
    # answer_in = 40+100+60 = 200; raw_user prompt-only 33 < 200
    cur = {
        "index": 2,
        "user_preview": "hi",
        "user_text": "hi",
        "context_start": 5000,
        "breakdown": {
            "cache_miss_in_tokens": 500,
            "cache_miss_in_usd": 0.001,
            "uncached_in_tokens": 733,
        },
        "user_prompt": {
            "kind": "user_prompt",
            "tokens_in": 33,
            "uncached_est": 33,
            "cost_in_usd": 0.000066,
            "cost_cached_usd": 0.0,
            "preview": "hi",
        },
        "cache_miss": True,
    }
    hb = _HB([prev, cur])
    _attach_prev_llm_answer(hb, cur)
    up = cur["user_prompt"]
    ans = up["prev_llm_answer"]
    assert ans["tokens_in"] == 200
    assert ans["tokens_in_full"] == 200
    assert up["tokens_in"] == 33 + 200
    assert up["uncached_est"] == 233
    assert not ans.get("from_user_pool")
    # miss peeled by answer
    assert cur["breakdown"]["cache_miss_in_tokens"] == 300
    assert cur["breakdown"]["user_in_tokens"] == 233


def test_from_user_pool_does_not_double_peel_miss():
    prev = {
        "index": 1,
        "model_steps": [
            {
                "index": 1,
                "thought_summary_tokens": 10,
                "message_tokens": 10,
                "tokens_out": 50,
                "composition": {"reasoning_encrypted_out": 5},
                "estimate": {"output_reasoning_tokens": 5},
                "children": [],
            }
        ],
    }
    # answer=25; raw_user=100 already includes answer
    cur = {
        "index": 2,
        "user_text": "x" * 400,
        "context_start": 1000,
        "breakdown": {"cache_miss_in_tokens": 80, "uncached_in_tokens": 180},
        "user_prompt": {
            "kind": "user_prompt",
            "tokens_in": 100,
            "uncached_est": 100,
            "cost_in_usd": 0.0002,
            "cost_cached_usd": 0.0,
        },
        "cache_miss": True,
    }
    hb = _HB([prev, cur])
    _attach_prev_llm_answer(hb, cur)
    up = cur["user_prompt"]
    assert up["tokens_in"] == 100  # unchanged pool
    assert up["prev_llm_answer"]["from_user_pool"] is True
    # miss NOT reduced again
    assert cur["breakdown"]["cache_miss_in_tokens"] == 80


def test_paid_unc_clamps_context_delta_reservation():
    """R3-style: context_delta user reserve > API paid unc → clamp before pool."""
    prev = {
        "index": 2,
        "completed": True,
        "model_steps": [
            {
                "index": 1,
                "thought_summary_tokens": 75,
                "message_tokens": 1400,
                "tokens_out": 2162,
                "composition": {"reasoning_encrypted_out": 687},
                "estimate": {"output_reasoning_tokens": 687, "output_tokens": 2162},
                "children": [],
            }
        ],
    }
    cur = {
        "index": 3,
        "user_text": "hi",
        "context_start": 37394,
        "breakdown": {
            "cache_miss_in_tokens": 0,
            "uncached_in_tokens": 2051,
            "paid_uncached_tokens": 2051,
            "user_uncached_reserved_tokens": 3686,
        },
        "user_prompt": {
            "kind": "user_prompt",
            "tokens_in": 3686,
            "uncached_est": 3686,
            "cost_in_usd": 0.007,
            "cost_cached_usd": 0.0,
            "preview": "hi",
        },
    }
    hb = _HB([prev, cur])
    _attach_prev_llm_answer(hb, cur)
    up = cur["user_prompt"]
    assert up["uncached_est_raw"] == 3686
    assert up["tokens_in"] == 2051
    assert up["prev_llm_answer"]["from_user_pool"] is False
    assert up["prev_llm_answer"]["tokens_in"] == 0


def test_warm_miss_zero_does_not_inflate_user_in():
    """Prior answer is cached — not in uncached bill; do not invent User In."""
    prev = {
        "index": 1,
        "completed": True,
        "model_steps": [
            {
                "index": 1,
                "thought_summary_tokens": 40,
                "message_tokens": 60,
                "tokens_out": 200,
                "composition": {"reasoning_encrypted_out": 100},
                "estimate": {"output_reasoning_tokens": 100, "output_tokens": 200},
                "children": [],
            }
        ],
    }
    cur = {
        "index": 2,
        "user_text": "hi",
        "context_start": 5000,
        "breakdown": {
            "cache_miss_in_tokens": 0,
            "uncached_in_tokens": 33,
        },
        "user_prompt": {
            "kind": "user_prompt",
            "tokens_in": 33,
            "uncached_est": 33,
            "cost_in_usd": 0.000066,
            "cost_cached_usd": 0.0,
            "preview": "hi",
        },
        "cache_miss": False,
    }
    hb = _HB([prev, cur])
    _attach_prev_llm_answer(hb, cur)
    up = cur["user_prompt"]
    ans = up["prev_llm_answer"]
    assert ans["tokens_in_full"] == 200
    assert ans["tokens_in"] == 0  # nothing billed into User In
    assert up["tokens_in"] == 33
    assert up["uncached_est"] == 33
    assert cur["breakdown"]["user_in_tokens"] == 33
    assert cur["breakdown"]["cache_miss_in_tokens"] == 0


def test_partial_miss_peels_only_available_mass():
    prev = {
        "index": 1,
        "completed": True,
        "model_steps": [
            {
                "index": 1,
                "thought_summary_tokens": 40,
                "message_tokens": 60,
                "tokens_out": 200,
                "composition": {"reasoning_encrypted_out": 100},
                "estimate": {"output_reasoning_tokens": 100, "output_tokens": 200},
                "children": [],
            }
        ],
    }
    # answer=200; miss only 50 → User In += 50 only
    cur = {
        "index": 2,
        "user_text": "hi",
        "context_start": 5000,
        "breakdown": {
            "cache_miss_in_tokens": 50,
            "uncached_in_tokens": 83,
        },
        "user_prompt": {
            "kind": "user_prompt",
            "tokens_in": 33,
            "uncached_est": 33,
            "cost_in_usd": 0.000066,
            "cost_cached_usd": 0.0,
            "preview": "hi",
        },
        "cache_miss": True,
    }
    hb = _HB([prev, cur])
    _attach_prev_llm_answer(hb, cur)
    up = cur["user_prompt"]
    ans = up["prev_llm_answer"]
    assert ans["tokens_in_full"] == 200
    assert ans["tokens_in"] == 50
    assert up["tokens_in"] == 83
    assert cur["breakdown"]["cache_miss_in_tokens"] == 0
    assert cur["breakdown"]["user_in_tokens"] == 83
    assert cur.get("cache_miss") is False
