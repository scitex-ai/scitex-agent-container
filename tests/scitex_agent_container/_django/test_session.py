"""Session-log tail: SSE parsing, summarization, and secret redaction."""

from __future__ import annotations

from scitex_agent_container._django._session import (
    parse_tail_frames,
    redact,
    summarize_record,
)


def test_redact_skr_secret():
    assert "sk-" not in redact("token sk-abc123DEF456GHI789jkl012 set")
    assert "[REDACTED]" in redact("token sk-abc123DEF456GHI789jkl012 set")


def test_redact_bearer_and_keyvalue():
    assert "xyz" not in redact("Authorization: Bearer abcdef1234567890XYZ").replace("[REDACTED]", "") or "[REDACTED]" in redact(
        "Authorization: Bearer abcdef1234567890XYZ"
    )
    assert "[REDACTED]" in redact("api_key: mySuperSecretKey123")


def test_redact_is_idempotent_and_safe_on_none():
    once = redact("sk-abc123DEF456GHI789jkl012")
    assert redact(once) == once
    assert redact(None) == ""


def test_summarize_record_uses_type_and_text():
    line = summarize_record({"type": "assistant", "text": "hello world"})
    assert line.startswith("assistant:")
    assert "hello world" in line


def test_summarize_record_redacts_secrets_in_text():
    line = summarize_record({"type": "user", "text": "use key sk-abc123DEF456GHI789jkl012 now"})
    assert "sk-" not in line
    assert "[REDACTED]" in line


def test_parse_tail_frames_returns_summarized_lines():
    body = (
        'data: {"line_no": 1, "record": {"type": "user", "text": "go"}}\n'
        "\n"
        'data: {"line_no": 2, "record": {"type": "result", "text": "done"}}\n'
    )
    lines = parse_tail_frames(body)
    assert lines == ["user: go", "result: done"]


def test_parse_tail_frames_bounded_to_last_n():
    body = "".join(
        f'data: {{"line_no": {i}, "record": {{"type": "user", "text": "m{i}"}}}}\n\n' for i in range(40)
    )
    lines = parse_tail_frames(body, limit=25)
    assert len(lines) == 25
    assert lines[-1] == "user: m39"


def test_parse_tail_frames_ignores_keepalive_and_bad_frames():
    body = ": keep-alive\n\ndata: not-json\n\ndata: {\"line_no\": 1, \"record\": {\"type\": \"user\", \"text\": \"x\"}}\n"
    lines = parse_tail_frames(body)
    assert lines == ["user: x"]


def test_parse_tail_frames_empty():
    assert parse_tail_frames("") == []
