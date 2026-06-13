"""Tests for ``_runners/_heartbeat_fields.heartbeat_jsonl_fields``.

Operator-requested per-beat productivity signal
(``feedback_sac_heartbeat_observability``). Real ``tmp_path`` state
dir with real ``session.jsonl`` + ``heartbeat.json`` files — no
mocks. STX-TQ002 AAA + STX-TQ007 one-assert.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Iterator

import pytest

from scitex_agent_container._runners._heartbeat_fields import (
    heartbeat_jsonl_fields,
    heartbeat_progress_fields,
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


# ---------------------------------------------------------------------------
# heartbeat_progress_fields — capped + current_phase (card
# sac-heartbeat-progress-signal PARTIAL fix).
#
# Both fields are ALWAYS present (False / "" default) so downstream
# consumers (`sac agents list` CAPPED color, board v3 dot strip) can
# rely on a stable schema without absence checks.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _clear_phase_env() -> Iterator[None]:
    """Drop SAC_AGENT_PHASE before each test, restore after.

    Tests assert on default behaviour; an env leak from a parent shell
    that exported SAC_AGENT_PHASE would corrupt every test. Plain
    os.environ pop + try/finally — no monkeypatch fixture (banned by
    PA-306 §3 no-mocks).
    """
    saved = os.environ.pop("SAC_AGENT_PHASE", None)
    try:
        yield
    finally:
        if saved is not None:
            os.environ["SAC_AGENT_PHASE"] = saved
        else:
            os.environ.pop("SAC_AGENT_PHASE", None)


@pytest.fixture
def set_phase_env() -> Iterator[callable]:
    """Manual SAC_AGENT_PHASE setter; restores prior value on teardown.

    Returns a setter function each test calls with the desired value;
    keeps the env mutation reversible without monkeypatch.
    """
    saved = os.environ.get("SAC_AGENT_PHASE")
    try:
        yield lambda value: os.environ.__setitem__("SAC_AGENT_PHASE", value)
    finally:
        if saved is not None:
            os.environ["SAC_AGENT_PHASE"] = saved
        else:
            os.environ.pop("SAC_AGENT_PHASE", None)


def test_progress_defaults_capped_false_phase_empty(tmp_path: Path) -> None:
    # Arrange — empty state dir; no env, no sidecar, no session.jsonl.
    # Act
    fields = heartbeat_progress_fields(tmp_path)
    # Assert — both keys present with safe defaults.
    assert fields == {"capped": False, "current_phase": ""}


def test_progress_current_phase_from_env_var(tmp_path: Path, set_phase_env) -> None:
    # Arrange
    set_phase_env("ingesting-records")
    # Act
    fields = heartbeat_progress_fields(tmp_path)
    # Assert
    assert fields["current_phase"] == "ingesting-records"


def test_progress_current_phase_from_sidecar_file(tmp_path: Path) -> None:
    # Arrange — agent wrote its phase to <state_dir>/phase.txt.
    (tmp_path / "phase.txt").write_text("compiling-report\n", encoding="utf-8")
    # Act
    fields = heartbeat_progress_fields(tmp_path)
    # Assert
    assert fields["current_phase"] == "compiling-report"


def test_progress_env_var_beats_sidecar(tmp_path: Path, set_phase_env) -> None:
    # Arrange — both sources present; env wins (resolution order).
    set_phase_env("env-wins")
    (tmp_path / "phase.txt").write_text("sidecar-loses\n", encoding="utf-8")
    # Act
    fields = heartbeat_progress_fields(tmp_path)
    # Assert
    assert fields["current_phase"] == "env-wins"


def test_progress_phase_truncated_to_max_chars(tmp_path: Path, set_phase_env) -> None:
    # Arrange — agent published an over-long phrase; payload must not
    # break, but the prefix is preserved so the operator still sees it.
    set_phase_env("x" * 500)
    # Act
    fields = heartbeat_progress_fields(tmp_path)
    # Assert — exact cap value lives in _PHASE_MAX_CHARS (120); we
    # assert the public contract: truncated below the original length.
    assert len(fields["current_phase"]) == 120


def test_progress_capped_true_when_sidecar_file_present(tmp_path: Path) -> None:
    # Arrange — out-of-band watcher dropped the sentinel file.
    (tmp_path / "capped").write_text("", encoding="utf-8")
    # Act
    fields = heartbeat_progress_fields(tmp_path)
    # Assert
    assert fields["capped"] is True


def test_progress_capped_true_on_weekly_limit_marker_in_session_jsonl(
    tmp_path: Path,
) -> None:
    # Arrange — most-recent assistant turn carries the operator's
    # observed cap phrasing ("hit your weekly limit · resets ...").
    record = (
        '{"type":"assistant","text":'
        '"You\'ve hit your weekly limit \\u00b7 resets 2026-06-18T05:00Z"}'
    )
    (tmp_path / "session.jsonl").write_text(record + "\n", encoding="utf-8")
    # Act
    fields = heartbeat_progress_fields(tmp_path)
    # Assert
    assert fields["capped"] is True


def test_progress_capped_false_when_session_jsonl_has_no_cap_marker(
    tmp_path: Path,
) -> None:
    # Arrange — normal assistant turn, no cap phrasing anywhere.
    (tmp_path / "session.jsonl").write_text(
        '{"type":"assistant","text":"hello, the build is green"}\n',
        encoding="utf-8",
    )
    # Act
    fields = heartbeat_progress_fields(tmp_path)
    # Assert
    assert fields["capped"] is False


def test_progress_capped_scans_only_tail_of_large_jsonl(tmp_path: Path) -> None:
    # Arrange — old assistant turn long ago hit the cap, then 64KB of
    # subsequent traffic recovered (e.g. account rotated). The CURRENT
    # state is healthy; the tail (the only thing we scan) carries no
    # cap marker, so we must NOT report capped=True.
    cap_line = '{"type":"assistant","text":"hit your weekly limit (old turn)"}\n'
    healthy_filler = (
        '{"type":"assistant","text":"ok working on the next task"}\n' * 2000
    )
    (tmp_path / "session.jsonl").write_text(cap_line + healthy_filler, encoding="utf-8")
    # Act
    fields = heartbeat_progress_fields(tmp_path)
    # Assert — tail-only scan keeps the heartbeat cheap on long sessions
    # AND avoids permanently flagging an agent that has since recovered.
    assert fields["capped"] is False


def test_progress_never_raises_on_corrupted_state_dir(tmp_path: Path) -> None:
    # Arrange — session.jsonl is a DIR (not a file); reads would EISDIR.
    (tmp_path / "session.jsonl").mkdir()
    raised: BaseException | None = None
    # Act
    try:
        fields = heartbeat_progress_fields(tmp_path)
    except Exception as exc:  # stx-allow: test-capture (reason: STX-TQ002 splits Act from Assert; heartbeat_progress_fields is contracted to NEVER raise)
        raised = exc
        fields = {}
    # Assert — degrades cleanly to safe defaults; never crashes the loop.
    assert raised is None and fields == {"capped": False, "current_phase": ""}
