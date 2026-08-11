"""Hierarchical session reconstruction (package).

Public entry: ``from token_telemetry.hierarchy import HierarchyBuilder``.
Private helpers used by tests are re-exported for shim compatibility.
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
    _classify_bootstrap_message,
)
from token_telemetry.hierarchy.builder import HierarchyBuilder
from token_telemetry.hierarchy.compact_out import (
    _compact_child,
    _shallow_round_copy,
    compact_round_inplace,
)
from token_telemetry.hierarchy.cache_miss import (
    _apply_session_restart_cache_miss,
    _attach_prev_llm_answer,
    _compute_idle_gap_ms,
    _detect_context_reread,
)
from token_telemetry.hierarchy.finalize import (
    _attach_step_estimates,
    _finalize_round,
    _finalize_step,
    _inject_system_message_residual,
    _merge_bootstrap_into_breakdown,
    _price_bootstrap_prompts,
    _reprice_completed_rounds,
)
from token_telemetry.hierarchy.recap_compact import (
    _attach_pending_recap_compact,
    _fill_compact_cost,
    _on_compact,
    _on_recap,
    _recap_prompt_info,
)
from token_telemetry.hierarchy.hooks import (
    _TO_USER_HOOK_EVENTS,
    _USER_SECTION_HOOK_EVENTS,
    _hook_slot,
)
from token_telemetry.hierarchy.text_metrics import (
    MAX_ROUNDS_RETAINED,
    _PREVIEW_MAX,
    _clip_str,
    _extract_recap_prompt_text,
    _json_len,
    _msg_text,
    _preview,
    _scale_chars_to_tokens,
    _text_of,
    json_dumps_len,
)
from token_telemetry.hierarchy.tools_meta import (
    _KNOWN_TOOLS,
    _arg_metrics,
    _content_metrics,
    _extract_plan_meta,
    _is_plan_tool,
    _plan_steps_from_todos,
    _primary_raw_payload,
    _raw_meta_path_fields,
    _search_replace_prompt_output,
    _short_title,
    _tool_name,
    _tool_seq_from_id,
    _wire_content_text_parts,
)

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
    # private helpers re-exported for tests / scripts/hierarchy.py shim
    "_content_metrics",
    "_primary_raw_payload",
    "_arg_metrics",
    "_preview",
    "_text_of",
    "_msg_text",
    "_tool_name",
    "_hook_slot",
    "_shallow_round_copy",
    "_scale_chars_to_tokens",
    "_extract_recap_prompt_text",
    "_json_len",
    "_clip_str",
    "_is_plan_tool",
    "_extract_plan_meta",
    "_plan_steps_from_todos",
    "_short_title",
    "_tool_seq_from_id",
    "_wire_content_text_parts",
    "_search_replace_prompt_output",
    "_raw_meta_path_fields",
    "_compact_child",
    "_classify_bootstrap_message",
    "_KNOWN_TOOLS",
    "_PREVIEW_MAX",
    "_USER_SECTION_HOOK_EVENTS",
    "_TO_USER_HOOK_EVENTS",
    # recap / compact free functions (S7b)
    "_on_recap",
    "_on_compact",
    "_recap_prompt_info",
    "_fill_compact_cost",
    "_attach_pending_recap_compact",
    # cache-miss / context-reread free functions (S7c)
    "_detect_context_reread",
    "_compute_idle_gap_ms",
    "_apply_session_restart_cache_miss",
    "_attach_prev_llm_answer",
    # finalize / pricing-attach free functions (S7d)
    "_finalize_round",
    "_finalize_step",
    "_attach_step_estimates",
    "_price_bootstrap_prompts",
    "_merge_bootstrap_into_breakdown",
    "_inject_system_message_residual",
    "_reprice_completed_rounds",
]
