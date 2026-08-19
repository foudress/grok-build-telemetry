"""Tool-result weights count content body, not ACP JSON envelope."""

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
