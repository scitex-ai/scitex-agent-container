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


def _write_subagent_session(state_dir: Path, *, name: str, size: int) -> None:
    sub = state_dir / "subagents" / name
    sub.mkdir(parents=True, exist_ok=True)
    (sub / "session.jsonl").write_bytes(b"y" * size)


def _write_task_output(
    state_dir: Path, *, layout: str, task_id: str, size: int
) -> None:
    # layout is ".tasks" or "tasks"
    task = state_dir / layout / task_id
    task.mkdir(parents=True, exist_ok=True)
    (task / "output").write_bytes(b"z" * size)


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


# ---------------------------------------------------------------------------
# subagent_jsonl_bytes / subagent_jsonl_delta_bytes
#
# Closes the PR #370 false-idle CAVEAT: an active subagent + idle main
# beat used to show ``delta=0``; now the operator sees the subagent
# production summed across every candidate layout.
# ---------------------------------------------------------------------------


def test_subagent_keys_absent_when_no_subagent_dirs_exist(tmp_path: Path) -> None:
    # Arrange — only the main session.jsonl; no subagents/, .tasks/, tasks/.
    _write_jsonl(tmp_path, size=1000)
    # Act
    fields = heartbeat_jsonl_fields(tmp_path, now=100.0)
    # Assert — ABSENT (not zero) so operator distinguishes "no
    # subagent infra" from "subagents present but idle".
    assert (
        "subagent_jsonl_bytes" not in fields
        and "subagent_jsonl_delta_bytes" not in fields
    )


def test_subagent_jsonl_bytes_sums_subagents_layout(tmp_path: Path) -> None:
    # Arrange — two subagent dirs under subagents/<name>/session.jsonl.
    _write_jsonl(tmp_path, size=0)
    _write_subagent_session(tmp_path, name="sub-a", size=300)
    _write_subagent_session(tmp_path, name="sub-b", size=700)
    # Act
    fields = heartbeat_jsonl_fields(tmp_path, now=100.0)
    # Assert
    assert fields["subagent_jsonl_bytes"] == 1000


def test_subagent_jsonl_bytes_sums_all_three_layouts(tmp_path: Path) -> None:
    # Arrange — one file per supported layout, simultaneously present.
    _write_jsonl(tmp_path, size=0)
    _write_subagent_session(tmp_path, name="sub-1", size=100)
    _write_task_output(tmp_path, layout=".tasks", task_id="t-dot", size=200)
    _write_task_output(tmp_path, layout="tasks", task_id="t-plain", size=400)
    # Act
    fields = heartbeat_jsonl_fields(tmp_path, now=100.0)
    # Assert — 100 + 200 + 400 across the three candidate globs.
    assert fields["subagent_jsonl_bytes"] == 700


def test_subagent_delta_bytes_matches_sum_growth(tmp_path: Path) -> None:
    # Arrange — prior beat saw subagent total of 500; now files sum to 1200.
    _write_jsonl(tmp_path, size=0)
    _write_subagent_session(tmp_path, name="sub-a", size=800)
    _write_task_output(tmp_path, layout="tasks", task_id="t1", size=400)
    (tmp_path / "heartbeat.json").write_text(
        json.dumps({"ts": 90.0, "session_jsonl_bytes": 0, "subagent_jsonl_bytes": 500}),
        encoding="utf-8",
    )
    # Act
    fields = heartbeat_jsonl_fields(tmp_path, now=100.0)
    # Assert — 800 + 400 - 500 == 700.
    assert fields["subagent_jsonl_delta_bytes"] == 700


def test_subagent_delta_clamped_to_zero_on_rotate(tmp_path: Path) -> None:
    # Arrange — prior saw 5000 subagent bytes; a subagent dir got cleaned
    # up and current sums to 100. Negative delta would mislead operator.
    _write_jsonl(tmp_path, size=0)
    _write_subagent_session(tmp_path, name="sub-survivor", size=100)
    (tmp_path / "heartbeat.json").write_text(
        json.dumps(
            {"ts": 90.0, "session_jsonl_bytes": 0, "subagent_jsonl_bytes": 5000}
        ),
        encoding="utf-8",
    )
    # Act
    fields = heartbeat_jsonl_fields(tmp_path, now=100.0)
    # Assert
    assert fields["subagent_jsonl_delta_bytes"] == 0


def test_subagent_delta_absent_when_no_prior_subagent_bytes(tmp_path: Path) -> None:
    # Arrange — prior heartbeat exists but pre-dates the new field; the
    # delta cannot be computed and must be ABSENT (not a misleading 0).
    _write_jsonl(tmp_path, size=0)
    _write_subagent_session(tmp_path, name="sub-a", size=300)
    _write_prior_beat(tmp_path, ts=90.0, jsonl_bytes=0)
    # Act
    fields = heartbeat_jsonl_fields(tmp_path, now=100.0)
    # Assert — current size IS reported, delta is not.
    assert (
        fields["subagent_jsonl_bytes"] == 300
        and "subagent_jsonl_delta_bytes" not in fields
    )
