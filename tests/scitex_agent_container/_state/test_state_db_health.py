"""An empty read must say whether it is a fact about the fleet or the reader.

WHY THIS EXISTS. ``open_db`` calls ``init_schema`` unconditionally, so opening a
wrong path — or a zero-byte file — CREATES the schema there and every query then
returns zero rows. Measured 2026-08-09: a 0-byte ``state.db`` at the pre-cutover
path was accepted by ``sqlite3.connect``, the tables were built on it, and the
fleet read as "no agents registered" while twelve agents were running and
answering on the a2a rail.

Four states, four different responses. Only ``populated`` licenses a factual
claim about fleet contents.
"""

from __future__ import annotations

import sqlite3

import pytest

from scitex_agent_container._state import state_db
from scitex_agent_container._state.state_db_health import (
    CORE_TABLES,
    STORE_STATES,
    StoreState,
    inspect_store,
)


@pytest.fixture
def missing(tmp_path):
    return tmp_path / "state.db"


@pytest.fixture
def zero_byte(tmp_path):
    path = tmp_path / "state.db"
    path.touch()
    return path


@pytest.fixture
def other_database(tmp_path):
    """A real SQLite file that is NOT our store."""
    path = tmp_path / "someone-elses.db"
    conn = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE unrelated (id INTEGER)")
        conn.commit()
    finally:
        conn.close()
    yield path


@pytest.fixture
def real_store(tmp_path):
    """A store built the way production builds one — ``init_schema``.

    IT USED TO HAND-WRITE ``CREATE TABLE`` FOR EACH NAME IN ``CORE_TABLES``,
    and that is precisely why this suite could not see the bug it exists to
    catch. Restating the constant in the fixture makes the test a tautology:
    whatever ``CORE_TABLES`` lists, the fixture creates, so the pair always
    agrees — even when the real ``init_schema`` stopped creating one of those
    tables entirely. Measured on 2026-08-28: ``channel_events`` left SQLite
    for the shared PostgreSQL (ADR-0023) and ``CORE_TABLES`` still named it,
    so every store this package initialises was permanently missing half its
    core, and these tests stayed green because the fixture kept building the
    table by hand.

    Calling the production initialiser makes the two independent: the
    constant is the claim, ``init_schema`` is the world, and a name in the
    former with no table in the latter now fails
    ``test_the_core_tables_are_all_created_by_init_schema`` below.
    """
    path = tmp_path / "state.db"
    state_db.init_schema(path)
    yield path


def test_a_missing_file_is_absent_not_empty(missing):
    # Arrange
    path = missing
    # Act
    result = inspect_store(path)
    # Assert
    assert result.state == "absent"


def test_a_zero_byte_file_is_empty_not_populated(zero_byte):
    # Arrange
    path = zero_byte
    # Act
    result = inspect_store(path)
    # Assert — the exact file that produced the "no agents" reading.
    assert result.state == "empty"


def test_a_zero_byte_file_is_distinguishable_from_a_missing_one(
    zero_byte, tmp_path
):
    # Arrange
    absent = tmp_path / "nope" / "state.db"
    # Act
    present = inspect_store(zero_byte)
    gone = inspect_store(absent)
    # Assert
    assert present.state != gone.state


def test_a_foreign_sqlite_file_is_schemaless(other_database):
    # Arrange
    path = other_database
    # Act
    result = inspect_store(path)
    # Assert — a real database, just not ours.
    assert result.state == "schemaless"


def test_a_store_with_core_tables_is_populated(real_store):
    # Arrange
    path = real_store
    # Act
    result = inspect_store(path)
    # Assert
    assert result.state == "populated"


def test_only_populated_licenses_a_row_count_claim(zero_byte):
    # Arrange
    path = zero_byte
    # Act
    result = inspect_store(path)
    # Assert
    assert result.is_populated is False


def test_a_real_store_licenses_a_row_count_claim(real_store):
    # Arrange
    path = real_store
    # Act
    result = inspect_store(path)
    # Assert
    assert result.is_populated is True


def test_inspecting_an_absent_path_does_not_create_it(missing):
    # Arrange
    path = missing
    # Act
    inspect_store(path)
    # Assert — a diagnostic that causes the bug it diagnoses is useless.
    assert not path.exists()


def test_inspecting_a_zero_byte_file_does_not_build_a_schema(zero_byte):
    # Arrange
    path = zero_byte
    # Act
    inspect_store(path)
    # Assert — this is the side effect that made the defect invisible.
    assert path.stat().st_size == 0


def test_absent_advice_says_the_reading_describes_the_path(missing):
    # Arrange
    result = inspect_store(missing)
    # Act
    described = result.describe()
    # Assert
    assert "path" in described.lower()


def test_empty_advice_names_the_silent_schema_build(zero_byte):
    # Arrange
    result = inspect_store(zero_byte)
    # Act
    described = result.describe()
    # Assert — name the mechanism, not just the symptom.
    assert "empty tables" in described or "schema-init" in described


def test_schemaless_advice_says_verify_the_path(other_database):
    # Arrange
    result = inspect_store(other_database)
    # Act
    described = result.describe()
    # Assert
    assert "verify the path" in described.lower()


def test_a_nonsense_state_is_rejected_where_it_is_built(tmp_path):
    # Arrange
    fields = {"path": tmp_path / "x.db", "state": "probably-fine"}

    # Act
    def build():
        return StoreState(**fields)

    # Assert
    with pytest.raises(ValueError):
        build()


def test_every_declared_state_is_constructible(tmp_path):
    # Arrange
    built = []
    # Act
    for state in STORE_STATES:
        built.append(StoreState(path=tmp_path / "x.db", state=state).state)
    # Assert
    assert built == list(STORE_STATES)


# ---------------------------------------------------------------------------
# The constant vs the world.
#
# ``CORE_TABLES`` is a CLAIM about what an initialised store contains, and
# until 2026-08-28 nothing checked it against ``init_schema``. It named
# ``channel_events`` for the rest of the day after that table left SQLite for
# the shared PostgreSQL (ADR-0023), so every store this package created was
# missing half its declared core and the suite could not tell, because the
# ``real_store`` fixture hand-wrote both tables from the same constant.
# ---------------------------------------------------------------------------


def test_the_core_tables_are_all_created_by_init_schema(tmp_path):
    """Every name in the core must be a table ``init_schema`` actually makes.

    A name here that ``init_schema`` does not create cannot make the store
    classify wrongly — the predicate is ANY — but it silently shrinks the
    core, and :meth:`StoreState.describe` prints the whole list to an
    operator as "this file carries none of ...", sending them to look for a
    table that exists nowhere.
    """
    # Arrange
    path = tmp_path / "state.db"
    state_db.init_schema(path)
    # Act
    missing_from_schema = sorted(
        name for name in CORE_TABLES if name not in _table_names(path)
    )
    # Assert
    assert missing_from_schema == []


def test_the_core_is_not_empty(tmp_path):
    """POSITIVE CONTROL — an empty core would make the test above vacuous.

    ``all(... in tables)`` over an empty tuple is True, so a core that lost
    its last name would pass the check above while
    :func:`inspect_store` classified every SQLite file on the machine as
    ours.
    """
    # Arrange
    declared = CORE_TABLES
    # Act
    count = len(declared)
    # Assert
    assert count >= 1


def _table_names(path):
    """The tables in ``path``, read through a real connection."""
    conn = sqlite3.connect(path)
    try:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    finally:
        conn.close()
    return {r[0] for r in rows}
