"""``incarnations`` — the birth-certificate table (v4 step 5).

One row joins the three settled identities (spec / agent / incarnation)
plus the compiled-spec snapshot at launch; the exit mirror completes the
life-and-death record. Real on-disk SQLite via explicit ``db_path`` —
no env juggling, no mocks.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container._state.state_db_incarnations import (
    get_incarnation,
    record_incarnation_birth,
    record_incarnation_exit,
)


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "state.db"


def _birth(db: Path, incarnation: str = "inc-1") -> str:
    return record_incarnation_birth(
        incarnation,
        agent_id="alpha",
        spec_id="/specs/alpha/spec.yaml",
        spec_git_sha="abc123",
        host="h1",
        compiled_spec_json='{"name": "alpha"}',
        db_path=db,
    )


def test_birth_row_is_readable_by_incarnation_id(db: Path) -> None:
    # Arrange
    _birth(db)
    # Act
    row = get_incarnation("inc-1", db_path=db)
    # Assert
    assert row is not None and row["agent_id"] == "alpha"


def test_birth_row_carries_the_spec_git_sha(db: Path) -> None:
    # Arrange
    _birth(db)
    # Act
    row = get_incarnation("inc-1", db_path=db)
    # Assert
    assert row["spec_git_sha"] == "abc123"


def test_birth_row_carries_the_compiled_spec_json(db: Path) -> None:
    # Arrange
    _birth(db)
    # Act
    row = get_incarnation("inc-1", db_path=db)
    # Assert
    assert row["compiled_spec_json"] == '{"name": "alpha"}'


def test_birth_is_upsert_on_the_incarnation_key(db: Path) -> None:
    # Arrange: a retried launch re-records the same incarnation.
    _birth(db)
    record_incarnation_birth(
        "inc-1",
        agent_id="alpha",
        spec_id="/specs/alpha/spec.yaml",
        spec_git_sha="def456",
        host="h1",
        compiled_spec_json='{"name": "alpha"}',
        db_path=db,
    )
    # Act
    row = get_incarnation("inc-1", db_path=db)
    # Assert: refreshed, not duplicated / not crashed.
    assert row["spec_git_sha"] == "def456"


def test_exit_mirror_updates_the_birth_row(db: Path) -> None:
    # Arrange
    _birth(db)
    # Act
    record_incarnation_exit("inc-1", reason="harness-returned", code=1, db_path=db)
    row = get_incarnation("inc-1", db_path=db)
    # Assert
    assert (row["exit_reason"], row["exit_code"]) == ("harness-returned", 1)


def test_exit_mirror_stamps_exited_at(db: Path) -> None:
    # Arrange
    _birth(db)
    # Act
    record_incarnation_exit("inc-1", reason="stopped-by-signal", code=0, db_path=db)
    row = get_incarnation("inc-1", db_path=db)
    # Assert
    assert row["exited_at"] is not None


def test_exit_without_birth_reports_false_not_insert(db: Path) -> None:
    # Arrange: no birth row for this id.
    _birth(db, "inc-other")
    # Act
    updated = record_incarnation_exit("inc-ghost", reason="crashed", code=1, db_path=db)
    # Assert: a death with no recorded birth is a real signal, not an
    # excuse to fabricate one.
    assert updated is False


def test_unknown_incarnation_reads_none(db: Path) -> None:
    # Arrange
    _birth(db)
    # Act
    row = get_incarnation("inc-nope", db_path=db)
    # Assert
    assert row is None


def test_incarnations_is_a_known_table(db: Path) -> None:
    # Arrange
    from scitex_agent_container._state.state_db import KNOWN_TABLES

    # Act
    known = set(KNOWN_TABLES)
    # Assert: `sac db query --table=incarnations` can reach the record.
    assert "incarnations" in known
