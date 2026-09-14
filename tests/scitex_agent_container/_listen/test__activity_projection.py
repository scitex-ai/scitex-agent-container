"""Tests for the safe runtime activity projection."""

import json

from scitex_agent_container._listen._activity_projection import activity_projection


def test_activity_uses_only_published_runtime_evidence(tmp_path) -> None:
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

    result = activity_projection(
        tmp_path,
        runtime_control={"queue": "draining", "secret": "private"},
        now=100.0,
    )

    assert result["phase"]["value"] == "reviewing"
    assert result["operation"]["value"] == "busy"
    assert result["turn_elapsed"]["value"] == 10.0
    assert result["queue"]["value"] == "draining"
    assert result["last_progress"]["source"] == "session.jsonl.mtime"
    assert result["inference"]["state"] == "unknown"
    assert "private" not in repr(result)


def test_activity_is_explicitly_unknown_without_authoritative_signals(tmp_path) -> None:
    result = activity_projection(tmp_path, now=100.0)

    assert set(result) == {
        "phase",
        "operation",
        "turn_elapsed",
        "last_progress",
        "queue",
        "inference",
        "tool",
        "wait",
    }
    assert all(signal["state"] == "unknown" for signal in result.values())
    assert "turn-start timestamp" in result["turn_elapsed"]["reason"]
