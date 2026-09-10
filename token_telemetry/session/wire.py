"""Moved from session/monitor.py — same bodies."""
from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional

from token_telemetry.pricing import (
    estimate_cost_usd,
    estimate_from_usage,
    pick_tier,
    pricing_model_scope,
    pricing_payload,
    ticks_to_usd,
)
from token_telemetry.hierarchy import HierarchyBuilder, MAX_ROUNDS_RETAINED
from token_telemetry.session.discover import (
    SESSIONS_ROOT,
    list_sessions_for_ui,
    read_active_session_ids,
    resolve_session_dir,
)
from token_telemetry.session.subagents import (
    attach_subagents_after_steps,
    capture_child_sys,
    child_ids_from_session_update,
    collect_child_ids_from_round,
    collect_resume_alias_from_round,
    ensure_lineage_subagent_cards,
    is_subagent_session,
    latest_in_resume_chain,
    read_session_summary,
    root_subagent_id,
    sibling_session_dir,
)
from token_telemetry.hierarchy.gen_rate import mean_gen_rate

def _slim_wire(obj: Any) -> Any:
    """Drop nulls / full user_text / plan step bodies before /api/state JSON.

    Keeps the tree/chart fields the dashboard reads while cutting idle poll
    allocation (browser re-parses this blob every second when unchanged).
    """
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            if v is None:
                continue
            if k == "user_text":
                continue
            if k == "plan" and isinstance(v, dict):
                steps_in = v.get("steps")
                steps_out: list[dict[str, Any]] = []
                if isinstance(steps_in, list):
                    for s in steps_in:
                        if not isinstance(s, dict):
                            continue
                        steps_out.append(
                            {
                                "n": s.get("n", s.get("id")),
                                "status": s.get("status"),
                            }
                        )
                plan: dict[str, Any] = {}
                if v.get("is_plan") is not None:
                    plan["is_plan"] = v.get("is_plan")
                if v.get("mode") is not None:
                    plan["mode"] = v.get("mode")
                sc = v.get("step_count")
                if sc is None and steps_out:
                    sc = len(steps_out)
                if sc is not None:
                    plan["step_count"] = sc
                if steps_out:
                    plan["steps"] = steps_out
                out[k] = plan
                continue
            out[k] = _slim_wire(v)
        return out
    if isinstance(obj, list):
        return [_slim_wire(x) for x in obj]
    return obj

def _slim_signals(sig: dict[str, Any]) -> dict[str, Any]:
    """Keep only fields the dashboard reads — signals.json can be large."""
    if not sig:
        return {}
    keys = (
        "contextTokensUsed",
        "contextWindowTokens",
        "avgTimeToFirstTokenMs",
        "avgResponseTimeMs",
        "itlP50Ms",
        "itlP99Ms",
        "itlMeanMs",
        "totalChunkCount",
        "toolCallCount",
        "turnCount",
        "sessionDurationSeconds",
        "modelsUsed",
        "primaryModelId",
        "toolsUsed",
    )
    out = {k: sig[k] for k in keys if k in sig}
    # Cap long lists
    for lk in ("modelsUsed", "toolsUsed"):
        v = out.get(lk)
        if isinstance(v, list) and len(v) > 40:
            out[lk] = v[:40]
    return out
