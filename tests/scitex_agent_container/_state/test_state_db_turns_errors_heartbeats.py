"""Tests for the diary tables: turns / errors / heartbeats (2026-05-17).

Foundation for fleet-wide visibility before fanout: every agent
writes timestamped state-transition rows to state.db like a journal;
the lead reads + filters them. Three new tables — ``turns``,
``errors``, ``heartbeats`` — plus runner write-paths.

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
# Migration — the three new tables exist on a fresh DB.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("table", ["turns", "errors", "heartbeats"])
def test_migration_creates_diary_table(db_path: Path, table: str):
    # Arrange
    from scitex_agent_container._state.state_db import init_schema

    # Act
    init_schema()
    with sqlite3.connect(db_path) as conn:
        names = {
            r[0]
            for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    # Assert
    assert table in names


# ---------------------------------------------------------------------------
# Round-trip — every insert helper writes a row that SELECT can recover.
# ---------------------------------------------------------------------------


def test_insert_turn_round_trips_status_field(db_path: Path, pg_schema: str):
    # Arrange
    from scitex_agent_container._state.state_db import open_db, record_turn

    # Act
    record_turn(
        turn_id="t-1",
        name="alice",
        host="h",
        status="queued",
        prompt_text="hi",
        ts=1700000000.0,
    )
    with open_db() as conn:
        row = dict(conn.execute("SELECT * FROM turns").fetchone())
    # Assert
    assert row["status"] == "queued"


def test_insert_error_round_trips_cause_field(db_path: Path, pg_schema: str):
    # Arrange
    from scitex_agent_container._state.state_db import open_db, record_error

    # Act
    record_error(name="alice", host="h", cause="sdk-crash", detail="boom")
    with open_db() as conn:
        row = dict(conn.execute("SELECT * FROM errors").fetchone())
    # Assert
    assert row["cause"] == "sdk-crash"


def test_insert_heartbeat_round_trips_state_field(db_path: Path, pg_schema: str):
    # Arrange
    from scitex_agent_container._state.state_db import open_db, record_heartbeat

    # Act
    record_heartbeat(name="alice", host="h", pid=4242, state="idle")
    with open_db() as conn:
        row = dict(conn.execute("SELECT * FROM heartbeats").fetchone())
    # Assert
    assert row["state"] == "idle"


# ---------------------------------------------------------------------------
# Reads — filtering via the standard sqlite WHERE clause used by
# ``sac db query --where=...``. The lead expects these filters to work.
# ---------------------------------------------------------------------------


def test_db_query_turns_filter_by_name_returns_only_matching_agent(db_path: Path, pg_schema: str):
    # Arrange
    from scitex_agent_container._state.state_db import open_db, record_turn

    record_turn(turn_id="t1", name="alice", host="h", status="queued")
    record_turn(turn_id="t2", name="bob", host="h", status="queued")
    record_turn(turn_id="t3", name="alice", host="h", status="responded")
    # Act
    with open_db() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM turns WHERE name=? ORDER BY ts ASC",
                ("alice",),
            ).fetchall()
        ]
    # Assert
    assert {r["turn_id"] for r in rows} == {"t1", "t3"}


def test_db_query_errors_filter_by_cause_returns_only_matching_rows(db_path: Path, pg_schema: str):
    # Arrange
    from scitex_agent_container._state.state_db import open_db, record_error

    record_error(name="alice", host="h", cause="auth", detail="x")
    record_error(name="alice", host="h", cause="network", detail="y")
    record_error(name="bob", host="h", cause="auth", detail="z")
    # Act
    with open_db() as conn:
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT * FROM errors WHERE cause=? ORDER BY error_id ASC",
                ("auth",),
            ).fetchall()
        ]
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
