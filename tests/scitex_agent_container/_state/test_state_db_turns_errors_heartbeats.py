"""Tests for the diary stores: turns / errors / heartbeats (2026-05-17).

Foundation for fleet-wide visibility before fanout: every agent
writes timestamped state-transition rows like a journal; the lead
reads + filters them. Three logical stores — ``turns``, ``errors``,
``heartbeats`` — plus runner write-paths.

They lived in ``state.db`` until 2026-08-28 and are now per-host
PostgreSQL (:mod:`scitex_agent_container._state.state_db_diary`). The
SQLite half of this file was INVERTED rather than deleted: the first
group below now asserts the tables are absent, unqueryable, and refused
by ``sac db query``, because a table that still exists and is never
written answers every reader with a confident zero.

Conventions:

  * One assertion per test (STX-TQ007); related invariants collapse
    into ``pytest.parametrize``.
  * AAA markers (Arrange / Act / Assert).
  * No ``monkeypatch`` / ``mocker`` (STX-NM002); env save/restore is
    done explicitly via the ``db_path`` fixture, and runner
    integration uses an injectable ``db_writer`` so we never patch.
"""

from __future__ import annotations

import importlib
import os
import sqlite3
from pathlib import Path

import pytest

from scitex_agent_container._state.state_db_diary import (
    ERRORS_STORE,
    HEARTBEATS_STORE,
    TURNS_STORE,
)


@pytest.fixture
def db_path(tmp_path: Path):
    """Isolated state.db location, exported via env so callers pick it up.

    PA-306: explicit env save/restore (no monkeypatch fixture).
    """
    p = tmp_path / "state.db"
    key = "SCITEX_AGENT_CONTAINER_STATE_DB"
    saved = os.environ.get(key)
    os.environ[key] = str(p)
    import scitex_agent_container._state.state_db as mod

    importlib.reload(mod)
    try:
        yield p
    finally:
        if saved is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = saved
        importlib.reload(mod)


# ---------------------------------------------------------------------------
# The SQLite tables are GONE, and asking for one must SAY so.
#
# These three tests were ONE test asserting the exact opposite — that
# ``init_schema`` created ``turns`` / ``errors`` / ``heartbeats`` in state.db.
# The writers moved to PostgreSQL first, which left DDL creating three tables
# nothing would ever write again. An always-empty table is the worst shape
# available: every reader still gets an answer, the answer is zero rows, and
# zero rows reads as "this agent recorded no turns" when the truth is "you are
# asking the wrong database". That is the ruling ``incarnations`` established
# on 2026-08-19, applied to the trio on 2026-08-28.
# ---------------------------------------------------------------------------


def _table_names(db_path: Path) -> set[str]:
    """Read ``sqlite_master`` and hand back the names, closing the connection.

    A PLAIN FUNCTION rather than part of the fixture below: STX-TQ005 forbids a
    fixture that acquires an external resource and ``return``\\ s instead of
    ``yield``\\ ing, and it reads the fixture body syntactically. Keeping the
    ``connect`` here means the fixture hands back a ``set``, which owns nothing
    and needs no teardown — which is the rule's actual point.
    """
    conn = sqlite3.connect(db_path)
    try:
        return {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    finally:
        conn.close()


@pytest.fixture
def sqlite_table_names(db_path: Path) -> set[str]:
    """Every table a fresh ``state.db`` really has, after ``init_schema``.

    THE POSITIVE CONTROL LIVES HERE, not in the tests below. A fixture that
    never ran the schema would hand them an empty set, and "the diary table is
    absent" would pass for exactly the wrong reason — absence of evidence
    reading as evidence of absence, which is the failure this whole file is
    about. The guard is a ``raise`` rather than an assertion so each test keeps
    one assertion (STX-TQ007), and it sits in a fixture rather than in a
    parametrized body (STX-TQ006).
    """
    from scitex_agent_container._state.state_db import init_schema

    init_schema()
    names = _table_names(db_path)
    if "instances" not in names:
        raise RuntimeError(
            f"init_schema() left no `instances` table in {db_path}; the schema "
            "never ran, so an absent diary table would prove nothing."
        )
    return names


@pytest.fixture
def known_tables() -> set[str]:
    """``KNOWN_TABLES`` as a set, with the same control applied to it.

    ``instances`` must be in there for this to be the whitelist the tests below
    mean to inspect; an empty or wrong tuple would otherwise make every
    "not whitelisted" assertion pass on its own.
    """
    from scitex_agent_container._state.state_db import KNOWN_TABLES

    known = set(KNOWN_TABLES)
    if "instances" not in known:
        raise RuntimeError(
            "KNOWN_TABLES does not contain `instances`; it is not the "
            "whitelist these tests mean to inspect."
        )
    return known


@pytest.mark.parametrize("table", ["turns", "errors", "heartbeats"])
def test_init_schema_no_longer_creates_the_diary_table_in_sqlite(
    sqlite_table_names: set[str], table: str
):
    # Arrange — the fixture ran init_schema and cleared its own control.
    names = sqlite_table_names
    # Act
    created = table in names
    # Assert
    assert not created


@pytest.mark.parametrize("table", ["turns", "errors", "heartbeats"])
def test_the_diary_table_is_no_longer_a_sqlite_known_table(
    known_tables: set[str], table: str
):
    # Arrange — while the name stayed whitelisted, ``sac db show`` reported
    # ``turns 0`` and ``sac db query --table=turns`` returned an empty array,
    # both of them confident and both of them wrong.
    known = known_tables
    # Act
    whitelisted = table in known
    # Assert
    assert not whitelisted


@pytest.mark.parametrize("table", ["turns", "errors", "heartbeats"])
def test_sac_db_query_refuses_the_moved_diary_table(db_path: Path, table: str):
    # Arrange — the end-to-end half. Removing the name from KNOWN_TABLES is
    # only useful if the CLI turns the request into a usage ERROR; exit code 2
    # is what click raises for a rejected ``--table`` choice.
    from click.testing import CliRunner

    from scitex_agent_container.cli_pkg.db_group import db_query

    runner = CliRunner()
    # Act
    result = runner.invoke(db_query, [f"--table={table}"])
    # Assert
    assert result.exit_code == 2, result.output


# ---------------------------------------------------------------------------
# Round-trip — every insert helper writes a row that SELECT can recover.
# ---------------------------------------------------------------------------


def _diary_rows(store_name: str) -> list[dict]:
    """Every visible row of one diary store, read through the module's opener.

    The tests below used to read back with ``open_db()`` and a raw
    ``SELECT * FROM turns``. That named a SQLite table, and once the diary
    moved to PostgreSQL the writes landed in one place and the assertions
    looked in another — so the round-trip tests failed with a bare
    ``TypeError: 'NoneType' object is not iterable`` on ``fetchone()``,
    which says nothing about what actually broke.

    Reading through ``state_db_diary._open`` keeps the reader and the writer
    on the SAME store by construction. A private name is used deliberately:
    the diary exposes no public reader, and inventing one HERE would make the
    test assert against a path production never takes.
    """
    from scitex_agent_container._state import state_db_diary as diary

    schema = {
        diary.TURNS_STORE: diary._turns_schema,
        diary.ERRORS_STORE: diary._errors_schema,
        diary.HEARTBEATS_STORE: diary._heartbeats_schema,
    }[store_name]()
    store = diary._open(schema)
    try:
        return [dict(row.values) for row in store.rows() if not row.hidden]
    finally:
        store.close()


def test_insert_turn_round_trips_status_field(db_path: Path, pg_schema: str):
    # Arrange
    from scitex_agent_container._state.state_db import record_turn

    # Act
    record_turn(
        turn_id="t-1",
        name="alice",
        host="h",
        status="queued",
        prompt_text="hi",
        ts=1700000000.0,
    )
    rows = _diary_rows(TURNS_STORE)
    # Assert
    assert [r["status"] for r in rows] == ["queued"]


def test_insert_error_round_trips_cause_field(db_path: Path, pg_schema: str):
    # Arrange
    from scitex_agent_container._state.state_db import record_error

    # Act
    record_error(name="alice", host="h", cause="sdk-crash", detail="boom")
    rows = _diary_rows(ERRORS_STORE)
    # Assert
    assert [r["cause"] for r in rows] == ["sdk-crash"]


def test_insert_heartbeat_round_trips_state_field(db_path: Path, pg_schema: str):
    # Arrange
    from scitex_agent_container._state.state_db import record_heartbeat

    # Act
    record_heartbeat(name="alice", host="h", pid=4242, state="idle")
    rows = _diary_rows(HEARTBEATS_STORE)
    # Assert
    assert [r["state"] for r in rows] == ["idle"]


# ---------------------------------------------------------------------------
# Reads — filtering via the standard sqlite WHERE clause used by
# ``sac db query --where=...``. The lead expects these filters to work.
# ---------------------------------------------------------------------------


def test_db_query_turns_filter_by_name_returns_only_matching_agent(db_path: Path, pg_schema: str):
    # Arrange
    from scitex_agent_container._state.state_db import record_turn

    record_turn(turn_id="t1", name="alice", host="h", status="queued")
    record_turn(turn_id="t2", name="bob", host="h", status="queued")
    record_turn(turn_id="t3", name="alice", host="h", status="responded")
    # Act
    rows = [r for r in _diary_rows(TURNS_STORE) if r["name"] == "alice"]
    # Assert
    assert {r["turn_id"] for r in rows} == {"t1", "t3"}


def test_db_query_errors_filter_by_cause_returns_only_matching_rows(db_path: Path, pg_schema: str):
    # Arrange
    from scitex_agent_container._state.state_db import record_error

    record_error(name="alice", host="h", cause="auth", detail="x")
    record_error(name="alice", host="h", cause="network", detail="y")
    record_error(name="bob", host="h", cause="auth", detail="z")
    # Act
    rows = [r for r in _diary_rows(ERRORS_STORE) if r["cause"] == "auth"]
    # Assert
    assert {r["name"] for r in rows} == {"alice", "bob"}


def test_db_query_heartbeats_latest_per_name_returns_one_row_per_agent(
    db_path: Path,
    pg_schema: str,
):
    # Arrange — alice has 3 beats, bob has 2.
    from scitex_agent_container._state.state_db import (
        latest_heartbeats_per_name,
        record_heartbeat,
    )

    record_heartbeat(name="alice", host="h", pid=1, state="idle", ts=1.0)
    record_heartbeat(name="alice", host="h", pid=1, state="working", ts=2.0)
    record_heartbeat(name="alice", host="h", pid=1, state="idle", ts=3.0)
    record_heartbeat(name="bob", host="h", pid=2, state="idle", ts=1.5)
    record_heartbeat(name="bob", host="h", pid=2, state="error", ts=2.5)
    # Act
    rows = latest_heartbeats_per_name()
    # Assert — one row per name, holding the LATEST ts for that name.
    assert {(r["name"], r["state"]) for r in rows} == {
        ("alice", "idle"),
        ("bob", "error"),
    }


# ---------------------------------------------------------------------------
# Runner integration — heartbeat tick / SDK crash / turn transitions.
#
# Runners take an injectable ``db_writer`` so the test passes a fake
# writer instead of monkey-patching the module. The fake records
# every call so we can assert on the sequence without touching the
# real state.db.
# ---------------------------------------------------------------------------


class _FakeDBWriter:
    """In-memory recorder substituted for the real ``state_db_diary`` calls."""

    def __init__(self) -> None:
        self.turns: list[dict] = []
        self.errors: list[dict] = []
        self.heartbeats: list[dict] = []

    def record_turn(self, **kwargs):
        self.turns.append(kwargs)

    def record_error(self, **kwargs):
        self.errors.append(kwargs)
        return len(self.errors)

    def record_heartbeat(self, **kwargs):
        self.heartbeats.append(kwargs)
        return len(self.heartbeats)


def test_runner_writes_heartbeat_to_db_on_tick(db_path: Path, tmp_path: Path):
    # Arrange — drive one tick of write_heartbeat through the injectable
    # db_writer surface that the runner uses in production.
    from scitex_agent_container._runners._session_state import write_heartbeat

    state_dir = tmp_path / "agent"
    writer = _FakeDBWriter()
    # Act
    write_heartbeat(
        state_dir,
        pid=4242,
        state="idle",
        name="alice",
        host="h",
        db_writer=writer,
    )
    # Assert
    assert len(writer.heartbeats) == 1


def test_runner_writes_error_row_on_sdk_crash(db_path: Path, tmp_path: Path):
    # Arrange — simulate an SDK runtime exception by invoking the
    # runner's error-reporting seam directly.
    from scitex_agent_container._runners._session_state import report_sdk_error

    writer = _FakeDBWriter()
    # Act
    report_sdk_error(
        name="alice",
        host="h",
        cause="sdk-crash",
        detail="boom",
        db_writer=writer,
    )
    # Assert
    assert writer.errors[0]["cause"] == "sdk-crash"


def test_runner_writes_turn_row_on_each_state_transition(db_path: Path, tmp_path: Path):
    # Arrange — drive the four canonical turn transitions through the
    # runner's per-transition logger.
    from scitex_agent_container._runners._session_state import record_turn_transition

    writer = _FakeDBWriter()
    # Act
    for status in ("queued", "delivered", "read", "responded"):
        record_turn_transition(
            turn_id="t-1",
            name="alice",
            host="h",
            status=status,
            db_writer=writer,
        )
    # Assert — exactly 4 rows, all sharing the same turn_id.
    assert [r["status"] for r in writer.turns] == [
        "queued",
        "delivered",
        "read",
        "responded",
    ]
