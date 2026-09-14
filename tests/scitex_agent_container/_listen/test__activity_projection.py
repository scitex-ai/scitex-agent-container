"""Tests for the safe runtime activity projection."""

import json

from scitex_agent_container._listen._activity_projection import activity_projection


def test_activity_uses_only_published_runtime_evidence(tmp_path) -> None:
    # Arrange
    (tmp_path / "heartbeat.json").write_text(
        json.dumps(
            {
                "state": "busy",
                "current_phase": "reviewing",
                "turn_started_at": 90.0,
                "prompt": "private",
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "session.jsonl").write_text("progress\n", encoding="utf-8")

    # Act
    result = activity_projection(
        tmp_path,
        runtime_control={"queue": "draining", "secret": "private"},
        now=100.0,
    )

    # Assert
    assert (
        result["phase"]["value"],
        result["operation"]["value"],
        result["turn_elapsed"]["value"],
        result["queue"]["value"],
        result["last_progress"]["source"],
        result["inference"]["state"],
        "private" in repr(result),
    ) == (
        "reviewing",
        "busy",
        10.0,
        "draining",
        "session.jsonl.mtime",
        "unknown",
        False,
    )


def test_activity_is_explicitly_unknown_without_authoritative_signals(tmp_path) -> None:
    # Arrange
    expected = {
        "phase",
        "operation",
        "turn_elapsed",
        "last_progress",
        "queue",
        "inference",
        "tool",
        "wait",
    }

    # Act
    result = activity_projection(tmp_path, now=100.0)

    # Assert
    assert (
        set(result),
        {signal["state"] for signal in result.values()},
        "turn-start timestamp" in result["turn_elapsed"]["reason"],
    ) == (expected, {"unknown"}, True)
