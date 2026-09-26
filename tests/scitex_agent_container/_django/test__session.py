"""Tests for ``_django/_session`` (session-log tail: parse, summarize, redact).

Pure-function coverage — no HTTP, no mocks, no monkeypatch. Each test has AAA
markers and a single assertion (STX-TQ002/TQ007).
"""

from __future__ import annotations

from scitex_agent_container._django._session import (
    parse_tail_frames,
    redact,
    summarize_record,
)


def test_redact_skr_secret_present():
    # Arrange
    text = "token sk-abc123DEF456GHI789jkl012 set"
    # Act
    out = redact(text)
    # Assert
    assert "sk-" not in out and "[REDACTED]" in out


def test_redact_bearer_token():
    # Arrange
    out = redact("Authorization: Bearer abcdef1234567890XYZ")
    # Act
    present = "[REDACTED]" in out
    # Assert
    assert present


def test_redact_keyvalue_secret():
    # Arrange
    out = redact("api_key: mySuperSecretKey123")
    # Act
    present = "[REDACTED]" in out
    # Assert
    assert present


def test_redact_is_idempotent():
    # Arrange
    once = redact("sk-abc123DEF456GHI789jkl012")
    # Act
    stable = redact(once) == once
    # Assert
    assert stable


def test_redact_none_is_empty():
    # Arrange
    out = redact(None)
    # Act
    empty = out == ""
    # Assert
    assert empty


def test_summarize_uses_type_and_text():
    # Arrange
    record = {"type": "assistant", "text": "hello world"}
    # Act
    line = summarize_record(record)
    # Assert
    assert line.startswith("assistant:") and "hello world" in line


def test_summarize_redacts_secrets_in_text():
    # Arrange
    record = {"type": "user", "text": "use key sk-abc123DEF456GHI789jkl012 now"}
    # Act
    line = summarize_record(record)
    # Assert
    assert "sk-" not in line and "[REDACTED]" in line


def test_parse_frames_returns_summarized_lines():
    # Arrange
    body = ('data: {"line_no": 1, "record": {"type": "user", "text": "go"}}\n\n'
            'data: {"line_no": 2, "record": {"type": "result", "text": "done"}}\n')
    # Act
    lines = parse_tail_frames(body)
    # Assert
    assert lines == ["user: go", "result: done"]


def test_parse_frames_bounded_to_last_n():
    # Arrange
    body = "".join(f'data: {{"line_no": {i}, "record": {{"type": "user", "text": "m{i}"}}}}\n\n' for i in range(40))
    # Act
    lines = parse_tail_frames(body, limit=25)
    # Assert
    assert len(lines) == 25 and lines[-1] == "user: m39"


def test_parse_frames_ignores_keepalive_and_bad_frames():
    # Arrange
    body = ': keep-alive\n\ndata: not-json\n\ndata: {"line_no": 1, "record": {"type": "user", "text": "x"}}\n'
    # Act
    lines = parse_tail_frames(body)
    # Assert
    assert lines == ["user: x"]


def test_parse_frames_empty():
    # Arrange
    body = ""
    # Act
    lines = parse_tail_frames(body)
    # Assert
    assert lines == []
