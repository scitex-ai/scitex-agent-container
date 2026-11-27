"""The START path mirrors an authored ``spec.uuid`` into runtime state.

Without the mirror, a SAC < 0.40 host-only ``uuid:`` line is the ONLY copy
of the identity: deleting it makes the next start mint a new one. Real
specs loaded from a tmp git repo; the state dir is the project-scope one the
start resolves. AAA markers, one assertion per test.
"""

from __future__ import annotations

from pathlib import Path

from scitex_agent_container._lifecycle._start_spec_uuid import start_spec_uuid
from scitex_agent_container.config import load_config
from scitex_agent_container.config._spec_uuid import (
    record_runtime_spec_uuid,
    runtime_spec_uuid,
)
from tests.scitex_agent_container.config.test__spec_uuid_migrate import make_fleet

_AUTHORED = "12345678-1234-5678-1234-567812345678"
_OTHER = "0f0e0d0c-0b0a-4908-8706-050403020100"


def _state_file(spec: Path) -> Path:
    return spec.parents[2] / "runtime" / spec.parent.name / "spec_uuid"


def test_authored_uuid_is_recorded_in_state(tmp_path: Path) -> None:
    # Arrange
    spec = make_fleet(tmp_path, uuid=_AUTHORED)
    # Act
    start_spec_uuid(load_config(spec))
    # Assert
    assert _state_file(spec).read_text().strip() == _AUTHORED


def test_authored_uuid_is_what_the_start_records(tmp_path: Path) -> None:
    # Arrange
    spec = make_fleet(tmp_path, uuid=_AUTHORED)
    # Act
    got = start_spec_uuid(load_config(spec))
    # Assert
    assert got == _AUTHORED


def test_spec_wins_over_a_different_state_value(tmp_path: Path) -> None:
    # Arrange
    spec = make_fleet(tmp_path, uuid=_AUTHORED)
    _state_file(spec).parent.mkdir(parents=True)
    _state_file(spec).write_text(_OTHER + "\n")
    # Act
    start_spec_uuid(load_config(spec))
    # Assert
    assert _state_file(spec).read_text().strip() == _AUTHORED


def test_identity_survives_removing_the_line(tmp_path: Path) -> None:
    # Arrange — start once with the line, then the line goes away.
    spec = make_fleet(tmp_path, uuid=_AUTHORED)
    start_spec_uuid(load_config(spec))
    # Act — the uuid a later uuid-less start reads.
    got = runtime_spec_uuid(_state_file(spec).parent)
    # Assert
    assert got == _AUTHORED


def test_uuidless_spec_still_mints_into_state(tmp_path: Path) -> None:
    # Arrange
    spec = make_fleet(tmp_path)
    # Act
    got = start_spec_uuid(load_config(spec))
    # Assert
    assert _state_file(spec).read_text().strip() == got


def test_unwritable_state_keeps_the_authored_uuid(tmp_path: Path) -> None:
    # Arrange — the state DIR path is occupied by a file: the mirror fails.
    spec = make_fleet(tmp_path, uuid=_AUTHORED)
    _state_file(spec).parent.parent.mkdir(parents=True)
    _state_file(spec).parent.write_text("not a directory")
    # Act
    got = start_spec_uuid(load_config(spec))
    # Assert
    assert got == _AUTHORED


def test_record_reports_previous_value(tmp_path: Path) -> None:
    # Arrange
    state = tmp_path / "state"
    state.mkdir()
    (state / "spec_uuid").write_text(_OTHER + "\n")
    # Act
    previous = record_runtime_spec_uuid(state, _AUTHORED)
    # Assert
    assert previous == _OTHER


def test_record_is_a_no_op_when_equal(tmp_path: Path) -> None:
    # Arrange
    state = tmp_path / "state"
    record_runtime_spec_uuid(state, _AUTHORED)
    mtime = (state / "spec_uuid").stat().st_mtime_ns
    # Act
    record_runtime_spec_uuid(state, _AUTHORED)
    # Assert
    assert (state / "spec_uuid").stat().st_mtime_ns == mtime
