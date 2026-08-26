"""Tool-result weights count content body, not ACP JSON envelope.

Tool-request weights use chat_history tool_calls[].arguments (not update rawInput).
"""

import json

from token_telemetry.hierarchy.bootstrap import (
    load_chat_history_tool_requests,
    load_chat_history_tool_results,
)
from token_telemetry.hierarchy.finalize import (
    _stamp_tool_chat_args,
    _stamp_tool_chat_results,
)
from token_telemetry.hierarchy.tools_meta import _content_metrics
from token_telemetry.tokenizer import count_tokens


def test_content_metrics_excludes_json_envelope():
    body = "hello from grep"
    m = _content_metrics(
        {
            "toolCallId": "call-1",
            "content": [{"type": "content", "text": body}],
        }
    )
    assert m["result_chars"] == len(body)
    assert m["result_tokens_est"] == count_tokens(body)
    wrapped = count_tokens(
        '{"type": "tool_result", "tool_call_id": "call-1", "content": "hello from grep"}'
    )
    assert m["result_tokens_est"] < wrapped


def test_get_command_task_output_uses_parent_facing_output():
    """Stream wait results live in rawOutput TaskOutput, not chat_history."""
    body = "Wave A landed on execute-plan/pr-1 (local only)."
    m = _content_metrics(
        {
            "title": "multi-wait (wait_all)",
            "toolCallId": "call-8eda273c-0608-49fd-afe2-49e162d0cd97-69",
            "status": "completed",
            "rawOutput": {
                "type": "TaskOutput",
                "MultiResult": {
                    "mode": "wait_all",
                    "results": [
                        {
                            "task_id": "01a01f99-b56e-7363-80e2-766701f6c487",
                            "command": "[subagent:general-purpose] Wave A",
                            "status": "completed",
                            "output": body,
                            "raw_output_bytes": 999999,
                        }
                    ],
                },
            },
        }
    )
    assert m["result_tokens_est"] == count_tokens(body)
    assert m["result_chars"] == len(body)
    # Must not count the child-session dump size (raw_output_bytes) or envelope.
    assert m["result_tokens_est"] < 200


def test_get_command_single_result_output():
    body = "Review notes are in the temp file."
    m = _content_metrics(
        {
            "title": "[subagent:general-purpose] [reviewer] pr-2",
            "status": "completed",
            "rawOutput": {
                "type": "TaskOutput",
                "Result": {
                    "task_id": "01a01fa3-7f05-70b1-9f67-047a435d3cf3",
                    "command": "[subagent:general-purpose] [reviewer]",
                    "status": "completed",
                    "output": body,
                },
            },
        }
    )
    assert m["result_tokens_est"] == count_tokens(body)


def test_wait_tool_history_uses_inner_content(tmp_path):
    tid = "call-8eda273c-0608-49fd-afe2-49e162d0cd97-69"
    body = (
        "=== Multi-wait (wait_all) ===\n"
        "--- Task 01a01f99-b56e-7363-80e2-766701f6c487 [completed] ---\n"
        "Wave A Out peel is on execute-plan/pr-1\n"
        "<subagent_result>\nsubagent_id: 01a01f99-b56e-7363-80e2-766701f6c487\n"
        "</subagent_result>\n"
        "2/2 tasks completed (wait_all)"
    )
    compact = tmp_path / "compaction_requests"
    compact.mkdir()
    (compact / "req.json").write_text(
        json.dumps(
            {
                "chat_history": [
                    {
                        "type": "assistant",
                        "content": "",
                        "tool_calls": [{"id": tid, "name": "get_command_or_subagent_output"}],
                    },
                    {"type": "tool_result", "tool_call_id": tid, "content": body},
                ]
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "chat_history.jsonl").write_text("", encoding="utf-8")
    by_id = load_chat_history_tool_results(tmp_path)
    hit = by_id[tid]
    assert hit["body_tokens"] == count_tokens(body)
    assert hit["body_chars"] == len(body)
    wrapped = count_tokens(
        json.dumps(
            {"type": "tool_result", "tool_call_id": tid, "content": body},
            ensure_ascii=False,
        )
    )
    assert hit["body_tokens"] < wrapped
    assert hit["subagent_id"] == "01a01f99-b56e-7363-80e2-766701f6c487"


def test_spawn_history_keeps_full_session_id(tmp_path):
    tid = "call-7965a813-4ada-427c-9156-488ec6fc7f8d-53"
    uid = "01a01f99-b56e-7363-80e2-766701f6c487"
    body = (
        "Subagent started in background.\n"
        f"subagent_id: {uid}\n"
        "type: general-purpose\n"
        "description: [implementer] pr-1: Wave A Out peel\n"
    )
    (tmp_path / "chat_history.jsonl").write_text(
        json.dumps({"type": "tool_result", "tool_call_id": tid, "content": body})
        + "\n",
        encoding="utf-8",
    )
    hit = load_chat_history_tool_results(tmp_path)[tid]
    assert hit["subagent_id"] == uid
    assert uid not in (hit.get("preview") or "") or hit["preview"].endswith("…")
    from token_telemetry.session.subagents import apply_history_subagent_ids

    tool = {
        "name": "spawn_subagent",
        "tool_call_id": tid,
        "result_preview": hit["preview"],
    }
    apply_history_subagent_ids(tool, hit)
    assert tool["subagent_id"] == uid


def test_get_command_counts_prompt_json_envelope():
    m = _content_metrics(
        {
            "title": "get_command_or_subagent_output",
            "toolCallId": "call-8eda273c-0608-49fd-afe2-49e162d0cd97-69",
            "content": {
                "type": "tool_result",
                "tool_call_id": "call-8eda273c-0608-49fd-afe2-49e162d0cd97-69",
                "content": "=== Task abc [completed] ===\nhello",
            },
        }
    )
    assert m["result_tokens_est"] > 0
    assert m["result_chars"] > 20


def test_load_tool_requests_uses_arguments_string(tmp_path):
    tid = "call-e664ff8f-9c3a-4d15-92ca-18c44321c81a-0"
    args = '{"target_file":"C:\\\\Users\\\\Alexy\\\\.grok\\\\projects.md"}'
    (tmp_path / "chat_history.jsonl").write_text(
        json.dumps(
            {
                "type": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": tid, "name": "read_file", "arguments": args},
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )
    hit = load_chat_history_tool_requests(tmp_path)[tid]
    assert hit["arguments"] == args
    assert hit["arg_chars"] == len(args)
    assert hit["arg_tokens"] == count_tokens(args)
    # Must not tokenize a re-dumped object with spaces / different key order cost
    inflated = json.dumps(
        {"variant": "ReadFile", "target_file": "C:\\Users\\Alexy\\.grok\\projects.md"},
        ensure_ascii=False,
    )
    assert hit["arg_tokens"] < count_tokens(inflated)


def test_stamp_results_always_body_not_envelope(tmp_path):
    tid = "call-read-1"
    body = "line1\nline2\nhello from read_file"
    (tmp_path / "chat_history.jsonl").write_text(
        json.dumps({"type": "tool_result", "tool_call_id": tid, "content": body})
        + "\n",
        encoding="utf-8",
    )

    class _HB:
        _session_dir = tmp_path
        _tool_results_cache = None
        _tool_results_mtime = None

        def _load_tool_results_fresh(self):
            from token_telemetry.hierarchy.finalize import _load_tool_results_fresh

            return _load_tool_results_fresh(self)

    tools = [
        {
            "name": "read_file",
            "tool_call_id": tid,
            "result_chars": 1,
            "result_tokens_est": 1,
        }
    ]
    _stamp_tool_chat_results(_HB(), tools)
    assert tools[0]["ch_result_tokens"] == count_tokens(body)
    assert tools[0]["result_tokens_est"] == count_tokens(body)
    env = count_tokens(
        json.dumps(
            {"type": "tool_result", "tool_call_id": tid, "content": body},
            ensure_ascii=False,
        )
    )
    assert tools[0]["result_tokens_est"] < env
    assert tools[0]["weight_source"] == "chat_history_tokenizer"


def test_stamp_args_overwrites_inflated_update_raw_input(tmp_path):
    tid = "call-bash-1"
    args = '{"command":"echo hi","description":"say hi"}'
    (tmp_path / "chat_history.jsonl").write_text(
        json.dumps(
            {
                "type": "assistant",
                "content": "",
                "tool_calls": [
                    {"id": tid, "name": "run_terminal_command", "arguments": args},
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )

    class _HB:
        _session_dir = tmp_path
        _tool_requests_cache = None
        _tool_requests_mtime = None

        def _load_tool_requests_fresh(self):
            from token_telemetry.hierarchy.finalize import _load_tool_requests_fresh

            return _load_tool_requests_fresh(self)

    tools = [
        {
            "name": "run_terminal_command",
            "tool_call_id": tid,
            # Pretend tool_call_update added variant + is_background
            "arg_chars": 200,
            "arg_tokens_est": 80,
        }
    ]
    _stamp_tool_chat_args(_HB(), tools)
    assert tools[0]["arg_chars"] == len(args)
    assert tools[0]["arg_tokens_est"] == count_tokens(args)
    assert tools[0]["arg_weight_source"] == "chat_history_arguments"
    assert tools[0]["arg_tokens_est"] < 80
