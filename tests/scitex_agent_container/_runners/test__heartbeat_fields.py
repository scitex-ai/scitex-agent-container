"""Tests for ``_runners/_heartbeat_fields.heartbeat_jsonl_fields``.

Operator-requested per-beat productivity signal
(``feedback_sac_heartbeat_observability``). Real ``tmp_path`` state
dir with real ``session.jsonl`` + ``heartbeat.json`` files — no
mocks. STX-TQ002 AAA + STX-TQ007 one-assert.
"""

from __future__ import annotations

import json
from pathlib import Path

from scitex_agent_container._runners._heartbeat_fields import (
    heartbeat_jsonl_fields,
)


def _write_prior_beat(state_dir: Path, *, ts: float, jsonl_bytes: int) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "heartbeat.json").write_text(
        json.dumps({"ts": ts, "session_jsonl_bytes": jsonl_bytes}),
        encoding="utf-8",
    )


def _write_jsonl(state_dir: Path, *, size: int) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    (state_dir / "session.jsonl").write_bytes(b"x" * size)


# ---------------------------------------------------------------------------
# session_jsonl_bytes
# ---------------------------------------------------------------------------


def test_session_jsonl_bytes_reflects_current_file_size(tmp_path: Path) -> None:
    # Arrange
    _write_jsonl(tmp_path, size=2048)
    # Act
    fields = heartbeat_jsonl_fields(tmp_path, now=100.0)
    # Assert
    assert fields["session_jsonl_bytes"] == 2048


def test_session_jsonl_bytes_is_zero_when_file_absent(tmp_path: Path) -> None:
    # Arrange — empty state dir, no session.jsonl, no prior beat.
    # Act
    fields = heartbeat_jsonl_fields(tmp_path, now=100.0)
    # Assert
    assert fields["session_jsonl_bytes"] == 0


# ---------------------------------------------------------------------------
# session_jsonl_delta_bytes
# ---------------------------------------------------------------------------


def test_delta_bytes_positive_when_jsonl_grew(tmp_path: Path) -> None:
    # Arrange — prior beat saw 1000 bytes; current size is 1500.
    _write_jsonl(tmp_path, size=1500)
    _write_prior_beat(tmp_path, ts=90.0, jsonl_bytes=1000)
    # Act
    fields = heartbeat_jsonl_fields(tmp_path, now=100.0)
    # Assert
    assert fields["session_jsonl_delta_bytes"] == 500


def test_delta_bytes_zero_when_jsonl_unchanged(tmp_path: Path) -> None:
    # Arrange — idle agent: prior == current.
    _write_jsonl(tmp_path, size=1000)
    _write_prior_beat(tmp_path, ts=90.0, jsonl_bytes=1000)
    # Act
    fields = heartbeat_jsonl_fields(tmp_path, now=100.0)
    # Assert
    assert fields["session_jsonl_delta_bytes"] == 0


def test_delta_bytes_clamped_to_zero_on_rotate(tmp_path: Path) -> None:
    # Arrange — rotate / truncate: prior saw 5000, now 100; delta must
    # NOT be negative (operator would misread it as destroyed work).
    _write_jsonl(tmp_path, size=100)
    _write_prior_beat(tmp_path, ts=90.0, jsonl_bytes=5000)
    # Act
    fields = heartbeat_jsonl_fields(tmp_path, now=100.0)
    # Assert
    assert fields["session_jsonl_delta_bytes"] == 0


def test_delta_bytes_absent_when_no_prior_beat(tmp_path: Path) -> None:
    # Arrange — first beat ever; no prior heartbeat.json on disk.
    _write_jsonl(tmp_path, size=1500)
    # Act
    fields = heartbeat_jsonl_fields(tmp_path, now=100.0)
    # Assert
    assert "session_jsonl_delta_bytes" not in fields


# ---------------------------------------------------------------------------
# seconds_since_last_beat
# ---------------------------------------------------------------------------


def test_seconds_since_last_beat_reflects_gap(tmp_path: Path) -> None:
    # Arrange — prior beat at ts=90, now=100 → 10s gap.
    _write_jsonl(tmp_path, size=1000)
    _write_prior_beat(tmp_path, ts=90.0, jsonl_bytes=1000)
    # Act
    fields = heartbeat_jsonl_fields(tmp_path, now=100.0)
    # Assert
    assert fields["seconds_since_last_beat"] == 10.0


def test_seconds_since_last_beat_absent_when_no_prior_beat(tmp_path: Path) -> None:
    # Arrange — fresh state, only session.jsonl is present.
    _write_jsonl(tmp_path, size=500)
    # Act
    fields = heartbeat_jsonl_fields(tmp_path, now=100.0)
    # Assert
    assert "seconds_since_last_beat" not in fields


def test_seconds_since_last_beat_absent_when_prior_ts_malformed(tmp_path: Path) -> None:
    # Arrange — prior heartbeat carries a non-numeric ts (corrupted file).
    _write_jsonl(tmp_path, size=500)
    (tmp_path / "heartbeat.json").write_text(
        json.dumps({"ts": "not-a-number"}), encoding="utf-8"
    )
    # Act
    fields = heartbeat_jsonl_fields(tmp_path, now=100.0)
    # Assert
    assert "seconds_since_last_beat" not in fields


# ---------------------------------------------------------------------------
# Robustness — malformed prior heartbeat must NEVER crash the runner
# ---------------------------------------------------------------------------


def test_malformed_prior_heartbeat_json_does_not_raise(tmp_path: Path) -> None:
    # Arrange — corrupted prior heartbeat.json (not valid JSON).
    _write_jsonl(tmp_path, size=500)
    (tmp_path / "heartbeat.json").write_text("not-json", encoding="utf-8")
    raised: BaseException | None = None
    # Act
    try:
        fields = heartbeat_jsonl_fields(tmp_path, now=100.0)
    except Exception as exc:  # stx-allow: test-capture (reason: STX-TQ002 splits Act from Assert; the helper is contracted to NEVER raise.)
        raised = exc
        fields = {}
    # Assert — never raises; current jsonl bytes still observable.
    assert raised is None and fields.get("session_jsonl_bytes") == 500


def test_missing_session_jsonl_returns_zero_not_error(tmp_path: Path) -> None:
    # Arrange — only a prior heartbeat exists; session.jsonl never created.
    _write_prior_beat(tmp_path, ts=90.0, jsonl_bytes=0)
    # Act
    fields = heartbeat_jsonl_fields(tmp_path, now=100.0)
    # Assert
    assert fields["session_jsonl_bytes"] == 0
