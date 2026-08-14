"""Contract tests for :mod:`scitex_agent_container._runners._incarnation`.

The v4 step-5 liveness artifact's building blocks: the bind-once
incarnation adoption (what makes the beat a WITNESS instead of an echo
of the ledger), the beat enrichment fields, the first-cause-wins exit
holder, and the terminal ExitRecord file. No mocks — real files, real
clocks with explicit ``boot_ts`` injection.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import pytest

from scitex_agent_container._runners import _incarnation as inc


@pytest.fixture(autouse=True)
def _unbound(tmp_path: Path):
    """Each test starts and ends with no bind for its tmp state dir."""
    inc.clear_incarnation_binding(tmp_path)
    yield
    inc.clear_incarnation_binding(tmp_path)


def _write_marker(state_dir: Path, incarnation: str) -> Path:
    state_dir.mkdir(parents=True, exist_ok=True)
    p = state_dir / "instance_id"
    p.write_text(incarnation, encoding="utf-8")
    return p


# ---------------------------------------------------------------------------
# bind-once adoption
# ---------------------------------------------------------------------------


def test_bind_adopts_fresh_marker(tmp_path: Path) -> None:
    # Arrange: a marker younger than the process.
    _write_marker(tmp_path, "inc-aaa")
    # Act
    got = inc.try_bind_incarnation(tmp_path, boot_ts=time.time() - 5)
    # Assert
    assert got == "inc-aaa"


def test_bind_refuses_stale_marker_from_previous_incarnation(tmp_path: Path) -> None:
    # Arrange: the marker predates this "process" by more than the grace
    # — a crashed previous incarnation's leftover.
    marker = _write_marker(tmp_path, "inc-old")
    old = time.time() - 3600
    os.utime(marker, (old, old))
    # Act
    got = inc.try_bind_incarnation(tmp_path, boot_ts=time.time())
    # Assert
    assert got is None


def test_bind_is_once_a_rewritten_marker_never_rebinds(tmp_path: Path) -> None:
    # Arrange: bound to the first marker; then a start path mints a NEW
    # id over this (still bound) process — the P0 shape where the ledger
    # writes while the process never cycles.
    _write_marker(tmp_path, "inc-first")
    inc.try_bind_incarnation(tmp_path, boot_ts=time.time() - 5)
    _write_marker(tmp_path, "inc-second")
    # Act
    rebound = inc.try_bind_incarnation(tmp_path, boot_ts=time.time() - 5)
    # Assert: the process keeps testifying to ITS OWN incarnation.
    assert rebound == "inc-first"


def test_bind_without_marker_is_none_not_error(tmp_path: Path) -> None:
    # Arrange: no instance_id marker exists.
    boot = time.time()
    # Act
    got = inc.try_bind_incarnation(tmp_path, boot_ts=boot)
    # Assert
    assert got is None


def test_clear_binding_allows_a_new_process_generation(tmp_path: Path) -> None:
    # Arrange: a previous generation bound "inc-one"; a NEW daemon
    # generation clears at boot and a fresh marker lands.
    _write_marker(tmp_path, "inc-one")
    inc.try_bind_incarnation(tmp_path, boot_ts=time.time() - 5)
    inc.clear_incarnation_binding(tmp_path)
    _write_marker(tmp_path, "inc-two")
    # Act
    got = inc.try_bind_incarnation(tmp_path, boot_ts=time.time() - 5)
    # Assert
    assert got == "inc-two"


def test_bound_incarnation_is_passive_and_none_when_unbound(tmp_path: Path) -> None:
    # Arrange: a marker exists but nothing bound it.
    _write_marker(tmp_path, "inc-passive")
    # Act
    got = inc.bound_incarnation(tmp_path)
    # Assert: passive read never adopts — observers must not guess.
    assert got is None


# ---------------------------------------------------------------------------
# beat enrichment fields
# ---------------------------------------------------------------------------


def test_beat_fields_seq_is_monotonic_over_prev_beat(tmp_path: Path) -> None:
    # Arrange
    prev = {"seq": 41}
    # Act
    out = inc.incarnation_beat_fields(
        tmp_path, prev_beat=prev, writer=inc.WRITER_SESSION_DAEMON
    )
    # Assert
    assert out["seq"] == 42


def test_beat_fields_carry_the_writer_name(tmp_path: Path) -> None:
    # Arrange
    prev = {"seq": 1}
    # Act
    out = inc.incarnation_beat_fields(
        tmp_path, prev_beat=prev, writer=inc.WRITER_SESSION_DAEMON
    )
    # Assert
    assert out["writer"] == inc.WRITER_SESSION_DAEMON


def test_beat_fields_seq_starts_at_one_without_prev(tmp_path: Path) -> None:
    # Arrange
    prev = None
    # Act
    out = inc.incarnation_beat_fields(tmp_path, prev_beat=prev, writer=None)
    # Assert
    assert out["seq"] == 1


def test_beat_fields_seq_survives_malformed_prev(tmp_path: Path) -> None:
    # Arrange
    prev = {"seq": "garbage"}
    # Act
    out = inc.incarnation_beat_fields(tmp_path, prev_beat=prev, writer=None)
    # Assert
    assert out["seq"] == 1


def test_beat_fields_omit_incarnation_when_unbound(tmp_path: Path) -> None:
    # Arrange: nothing bound — an observer must not guess.
    prev = None
    # Act
    out = inc.incarnation_beat_fields(tmp_path, prev_beat=prev, writer=None)
    # Assert
    assert "incarnation_id" not in out


def test_beat_fields_stamp_incarnation_once_bound(tmp_path: Path) -> None:
    # Arrange
    _write_marker(tmp_path, "inc-bound")
    inc.try_bind_incarnation(tmp_path, boot_ts=time.time() - 5)
    # Act
    out = inc.incarnation_beat_fields(tmp_path, prev_beat=None, writer=None)
    # Assert
    assert out["incarnation_id"] == "inc-bound"


def test_beat_fields_omit_writer_when_unidentified(tmp_path: Path) -> None:
    # Arrange
    prev = None
    # Act
    out = inc.incarnation_beat_fields(tmp_path, prev_beat=prev, writer=None)
    # Assert
    assert "writer" not in out


# ---------------------------------------------------------------------------
# ExitReasonHolder — first cause wins
# ---------------------------------------------------------------------------


def test_holder_records_the_first_cause(tmp_path: Path) -> None:
    # Arrange
    h = inc.ExitReasonHolder()
    # Act
    recorded = h.set_once(inc.EXIT_STOPPED_BY_SIGNAL, 0)
    # Assert
    assert recorded is True


def test_holder_second_cause_does_not_overwrite(tmp_path: Path) -> None:
    # Arrange
    h = inc.ExitReasonHolder()
    h.set_once(inc.EXIT_STOPPED_BY_SIGNAL, 0)
    # Act
    h.set_once(inc.EXIT_HARNESS_RETURNED, 1)
    # Assert
    assert (h.reason, h.code) == (inc.EXIT_STOPPED_BY_SIGNAL, 0)


def test_holder_rejects_unknown_reason(tmp_path: Path) -> None:
    # Arrange
    h = inc.ExitReasonHolder()
    # Act
    # Assert
    with pytest.raises(ValueError):
        h.set_once("wandered-off", 3)


# ---------------------------------------------------------------------------
# ExitRecord file
# ---------------------------------------------------------------------------


def test_exit_record_schema_as_landed(tmp_path: Path) -> None:
    # Arrange
    now = 1700000000.0
    # Act
    inc.write_exit_record(
        tmp_path,
        reason=inc.EXIT_HARNESS_RETURNED,
        code=1,
        incarnation_id=None,
        pid=4321,
        now_fn=lambda: now,
    )
    # Assert: the on-disk record is exactly the artifact schema.
    assert json.loads((tmp_path / inc.EXIT_RECORD_FILENAME).read_text()) == {
        "incarnation_id": None,
        "reason": "harness-returned",
        "code": 1,
        "ts": 1700000000.0,
        "pid": 4321,
    }


def test_exit_record_rejects_unknown_reason(tmp_path: Path) -> None:
    # Arrange
    state_dir = tmp_path
    # Act
    # Assert: the vocabulary is closed — a typo must fail loud.
    with pytest.raises(ValueError):
        inc.write_exit_record(state_dir, reason="gave-up", code=1)


def test_clear_exit_record_removes_previous_farewell(tmp_path: Path) -> None:
    # Arrange
    inc.write_exit_record(tmp_path, reason=inc.EXIT_STOPPED_BY_SIGNAL, code=0)
    # Act
    inc.clear_exit_record(tmp_path)
    # Assert
    assert inc.read_exit_record(tmp_path) is None


def test_clear_exit_record_is_idempotent_on_clean_dir(tmp_path: Path) -> None:
    # Arrange
    state_dir = tmp_path
    # Act
    inc.clear_exit_record(state_dir)
    # Assert
    assert inc.read_exit_record(state_dir) is None


def test_read_exit_record_corrupt_is_none(tmp_path: Path) -> None:
    # Arrange
    (tmp_path / inc.EXIT_RECORD_FILENAME).write_text("{not json", encoding="utf-8")
    # Act
    got = inc.read_exit_record(tmp_path)
    # Assert
    assert got is None
