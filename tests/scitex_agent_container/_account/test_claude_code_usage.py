"""Tests for Claude Code transcript and status-line usage readers."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

from scitex_agent_container._account.claude_code_usage import (
    read_claude_code_usage,
)


def _write_transcript(home: Path, records: list[dict], name: str = "one") -> None:
    path = home / ".claude" / "projects" / "-work" / f"{name}.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(json.dumps(record) for record in records))


def _assistant(
    uuid: str,
    *,
    timestamp: str | None = None,
    model: str = "claude-opus-4-8",
    **usage: int,
) -> dict:
    record = {
        "type": "assistant",
        "uuid": uuid,
        "message": {"type": "message", "model": model, "usage": usage},
    }
    if timestamp is not None:
        record["timestamp"] = timestamp
    return record


def _write_statusline(home: Path, agent: str, cost: float) -> None:
    path = home / ".scitex" / "agent-container" / "statusline" / f"{agent}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "session_id": "session-1",
                "cost": {"total_cost_usd": cost},
            }
        )
    )


def test_reader_sums_all_claude_token_classes(tmp_path: Path) -> None:
    # Arrange
    _write_transcript(
        tmp_path,
        [
            _assistant(
                "a",
                input_tokens=10,
                output_tokens=4,
                cache_creation_input_tokens=3,
                cache_read_input_tokens=2,
            )
        ],
    )
    # Act
    usage = read_claude_code_usage(tmp_path, "sales")
    # Assert
    assert (
        usage["input_tokens"],
        usage["output_tokens"],
        usage["cache_creation_input_tokens"],
        usage["cache_read_input_tokens"],
    ) == (10, 4, 3, 2)


def test_reader_deduplicates_assistant_uuid_across_transcripts(
    tmp_path: Path,
) -> None:
    # Arrange
    record = _assistant("same", input_tokens=10)
    _write_transcript(tmp_path, [record], "one")
    _write_transcript(tmp_path, [record], "fork")
    # Act
    usage = read_claude_code_usage(tmp_path, "sales")
    # Assert
    assert usage["input_tokens"] == 10


def test_reader_counts_unique_assistant_messages(tmp_path: Path) -> None:
    # Arrange
    _write_transcript(
        tmp_path,
        [
            _assistant("a", input_tokens=10),
            _assistant("b", output_tokens=4),
        ],
    )
    # Act
    usage = read_claude_code_usage(tmp_path, "sales")
    # Assert
    assert usage["assistant_messages"] == 2


def test_reader_includes_nested_subagent_transcripts(tmp_path: Path) -> None:
    # Arrange
    path = (
        tmp_path
        / ".claude"
        / "projects"
        / "-work"
        / "session"
        / "subagents"
        / "agent-one.jsonl"
    )
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(_assistant("sub", output_tokens=9)))
    # Act
    usage = read_claude_code_usage(tmp_path, "sales")
    # Assert
    assert usage["output_tokens"] == 9


def test_reader_reports_assistant_usage_timestamp_window(tmp_path: Path) -> None:
    # Arrange
    _write_transcript(
        tmp_path,
        [
            _assistant(
                "later",
                timestamp="2026-07-26T12:48:50.048Z",
                output_tokens=1,
            ),
            _assistant(
                "earlier",
                timestamp="2026-06-27T19:39:10.285Z",
                input_tokens=1,
            ),
        ],
    )
    # Act
    usage = read_claude_code_usage(tmp_path, "sales")
    # Assert
    assert (
        usage["first_observed_at"],
        usage["last_observed_at"],
    ) == ("2026-06-27T19:39:10.285Z", "2026-07-26T12:48:50.048Z")


def test_reader_filters_usage_to_half_open_period(tmp_path: Path) -> None:
    # Arrange
    _write_transcript(
        tmp_path,
        [
            _assistant(
                "before",
                timestamp="2026-07-26T09:59:59Z",
                input_tokens=100,
            ),
            _assistant(
                "inside",
                timestamp="2026-07-26T10:00:00Z",
                input_tokens=10,
            ),
            _assistant(
                "at-end",
                timestamp="2026-07-26T11:00:00Z",
                input_tokens=1_000,
            ),
        ],
    )
    # Act
    usage = read_claude_code_usage(
        tmp_path,
        "sales",
        since=datetime(2026, 7, 26, 10, tzinfo=timezone.utc),
        until=datetime(2026, 7, 26, 11, tzinfo=timezone.utc),
    )
    # Assert
    assert (usage["input_tokens"], usage["assistant_messages"]) == (10, 1)


def test_reader_excludes_untimestamped_messages_from_period(
    tmp_path: Path,
) -> None:
    # Arrange
    _write_transcript(tmp_path, [_assistant("legacy", input_tokens=10)])
    # Act
    usage = read_claude_code_usage(
        tmp_path,
        "sales",
        since=datetime(2026, 7, 26, tzinfo=timezone.utc),
    )
    # Assert
    assert (usage["input_tokens"], usage["untimestamped_messages"]) == (0, 1)


def test_reader_omits_current_session_cost_from_period(tmp_path: Path) -> None:
    # Arrange
    _write_statusline(tmp_path, "sales", 0.012345)
    # Act
    usage = read_claude_code_usage(
        tmp_path,
        "sales",
        since=datetime(2026, 7, 26, tzinfo=timezone.utc),
    )
    # Assert
    assert usage["current_session_cost_usd"] is None


def test_reader_ignores_malformed_and_non_assistant_records(
    tmp_path: Path,
) -> None:
    # Arrange
    path = tmp_path / ".claude" / "projects" / "-work" / "one.jsonl"
    path.parent.mkdir(parents=True)
    path.write_text('not-json\n{"type":"user","usage":{"input_tokens":99}}')
    # Act
    usage = read_claude_code_usage(tmp_path, "sales")
    # Assert
    assert usage["input_tokens"] == 0


def test_reader_reports_current_session_provider_cost(tmp_path: Path) -> None:
    # Arrange
    _write_statusline(tmp_path, "sales", 0.012345)
    # Act
    usage = read_claude_code_usage(tmp_path, "sales")
    # Assert
    assert usage["current_session_cost_usd"] == 0.012345


def test_reader_estimates_api_equivalent_cost(tmp_path: Path) -> None:
    # Arrange
    record = _assistant(
        "priced",
        input_tokens=1_000_000,
        output_tokens=1_000_000,
        cache_creation_input_tokens=2_000_000,
        cache_read_input_tokens=1_000_000,
    )
    record["message"]["usage"]["cache_creation"] = {
        "ephemeral_5m_input_tokens": 1_000_000,
        "ephemeral_1h_input_tokens": 1_000_000,
    }
    _write_transcript(tmp_path, [record])
    # Act
    usage = read_claude_code_usage(tmp_path, "sales")
    # Assert
    assert usage["estimated_api_cost_usd"] == 46.75


def test_reader_marks_unknown_model_estimate_incomplete(tmp_path: Path) -> None:
    # Arrange
    _write_transcript(
        tmp_path,
        [_assistant("unknown", model="claude-future-99", input_tokens=1)],
    )
    # Act
    usage = read_claude_code_usage(tmp_path, "sales")
    # Assert
    assert usage["cost_estimate_complete"] is False


def test_reader_missing_home_is_explicit() -> None:
    # Arrange
    # Act
    usage = read_claude_code_usage(None, "sales")
    # Assert
    assert "could not be resolved" in usage["error"]
