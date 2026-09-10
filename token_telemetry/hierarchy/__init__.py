"""Hierarchical session reconstruction (package).

Public entry: ``from token_telemetry.hierarchy import HierarchyBuilder``.
"""

from __future__ import annotations

from token_telemetry.hierarchy.bootstrap import (
    DEFAULT_TOOL_DEFINITION_COUNT,
    DEFAULT_TOOL_DEFINITION_TOKENS,
    inject_tool_definitions_into_bootstrap,
    load_chat_history_reasonings,
    load_chat_history_tool_results,
    parse_session_bootstrap,
    resolve_tool_definitions,
)
from token_telemetry.hierarchy.builder import HierarchyBuilder
from token_telemetry.hierarchy.compact_out import compact_round_inplace
from token_telemetry.hierarchy.gen_rate import (
    attach_gen_rates,
    call_out_tokf,
    mean_gen_rate,
    stream_out_tokens,
)
from token_telemetry.hierarchy.text_metrics import MAX_ROUNDS_RETAINED, json_dumps_len

__all__ = [
    "HierarchyBuilder",
    "MAX_ROUNDS_RETAINED",
    "DEFAULT_TOOL_DEFINITION_TOKENS",
    "DEFAULT_TOOL_DEFINITION_COUNT",
    "compact_round_inplace",
    "json_dumps_len",
    "parse_session_bootstrap",
    "resolve_tool_definitions",
    "inject_tool_definitions_into_bootstrap",
    "load_chat_history_tool_results",
    "load_chat_history_reasonings",
    "attach_gen_rates",
    "call_out_tokf",
    "mean_gen_rate",
    "stream_out_tokens",
]
