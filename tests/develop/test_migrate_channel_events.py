#!/usr/bin/env python3
"""``scripts/migrate_channel_events_to_postgres.py`` — the ids must survive.

This migration is the one whose correctness is observable by a CLIENT rather
than only by an operator: the id it carries across IS the SSE cursor a
disconnected subscriber echoes back as ``Last-Event-ID``. Renumber a row and
that subscriber silently replays or silently skips. So the properties pinned
here are not "did it move rows" but:

* ``new_id == old_id`` when the destination target is free (the normal case);
* ``meta_json`` is byte-identical, Japanese content included — it is never
  re-encoded, only carried;
* delivered AND undelivered rows both come across, because ``list_since_id``
  reads regardless of ``delivered_at``;
* the cursor is seeded so the next live event gets a FRESH id;
* a re-run of the same host moves nothing and shifts nothing;
* a target whose ids are ALREADY occupied by an earlier host's residency is
  offset above them rather than colliding or overwriting.

LIVES IN tests/develop/, NOT the mirror tree, for the reason its sibling
``test_migrate_scripts_do_not_write_by_default.py`` states: these scripts sit
at the repo root under ``scripts/`` with no ``src/`` counterpart, so a
``test_*.py`` under ``tests/<pkg>/`` is an orphan and PS-204 §2 fails the
build.

NO MOCKS: a real SQLite file under ``tmp_path``, the real script's ``main``,
and a real throwaway PostgreSQL schema via ``pg_schema``.
"""

from __future__ import annotations

import contextlib
import importlib.util
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any, Iterator

import pytest

from scitex_agent_container._state.state_db_channel_store import (
    new_channel_connection,
    reset_channel_connection,
)

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "migrate_channel_events_to_postgres.py"

JAPANESE = "作業中断はしてほしくない"


@pytest.fixture(autouse=True)
def _drop_cached_connection() -> Iterator[None]:
    """Close the process-wide handle around every test in this module."""
    reset_channel_connection()
    yield
    reset_channel_connection()


@contextlib.contextmanager
def _loaded() -> Iterator[Any]:
    """Import the script by path — it has no importable package home."""
    if not SCRIPT.exists():
        pytest.skip(f"{SCRIPT.name} not present in this checkout")
    spec = importlib.util.spec_from_file_location(SCRIPT.stem, SCRIPT)
    module = importlib.util.module_from_spec(spec)
    saved = list(sys.path)
    # The script reaches its sibling ``_migrate_lib`` the way every migration
    # in that directory does — by being RUN from there, which puts its own
    # directory on ``sys.path``. Importing it by path skips that, so the test
    # supplies what the shell would. Restored afterwards; no monkeypatch.
    sys.path.insert(0, str(SCRIPT.parent))
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.path[:] = saved


def _legacy_db(tmp_path: Path, rows: list[tuple]) -> Path:
    """A SQLite file with the pre-migration ``channel_events`` schema."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    db = tmp_path / "state.db"
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE channel_events (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            target        TEXT NOT NULL,
            source        TEXT,
            kind          TEXT NOT NULL DEFAULT 'message',
            content       TEXT,
            meta_json     TEXT NOT NULL,
            ts            REAL NOT NULL,
            delivered_at  REAL
        );
        """
    )
    conn.executemany(
        "INSERT INTO channel_events (target, source, kind, content, meta_json, "
        "ts, delivered_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        rows,
    )
    conn.commit()
    conn.close()
    return db


def _seed_rows() -> list[tuple]:
    """Two rows for ``lead`` (one delivered) and one for ``ci`` (never)."""
    return [
        (
            "lead",
            "alice",
            "message",
            "hi",
            json.dumps({"msg_id": "a", "content": "hi"}, ensure_ascii=False),
            1.0,
            2.0,
        ),
        (
            "lead",
            "alice",
            "message",
            JAPANESE,
            json.dumps({"msg_id": "b", "content": JAPANESE}, ensure_ascii=False),
            2.0,
            None,
        ),
        (
            "ci",
            None,
            "message",
            "x",
            json.dumps({"msg_id": "c"}, ensure_ascii=False),
            3.0,
            None,
        ),
    ]


def _run(db: Path, *extra: str) -> int:
    with _loaded() as module:
        return int(module.main(["--db-path", str(db), *extra]))


def _query(sql: str, params: tuple) -> list[tuple]:
    conn = new_channel_connection()
    try:
        return list(conn.execute(sql, params).fetchall())
    finally:
        conn.close()


def _raw_query(sql: str, params: tuple) -> list[tuple]:
    """Ask the database WITHOUT applying the DDL first.

    ``new_channel_connection`` creates the tables on open, which is right for
    every other caller and useless for the dry-run test: it would create the
    very tables that test is asserting do not exist yet.
    """
    import psycopg

    from scitex_agent_container._state.state_db_channel_store import (
        _resolve_target,
    )

    with psycopg.connect(str(_resolve_target().dsn), autocommit=True) as conn:
        return list(conn.execute(sql, params).fetchall())


def test_dry_run_is_the_default_and_writes_nothing(
    tmp_path: Path, pg_schema: str
) -> None:
    """The bare invocation previews; it must not touch the store."""
    # Arrange
    db = _legacy_db(tmp_path, _seed_rows())
    # Act
    _run(db)
    # Assert — the tables do not even exist yet, so a count is the wrong
    # question; ask the catalog instead.
    assert _raw_query("SELECT to_regclass('sac_channel_events') IS NULL", ()) == [
        (True,)
    ]


def test_commit_preserves_every_id(tmp_path: Path, pg_schema: str) -> None:
    """``new_id == old_id`` — the property a live consumer's cursor rests on."""
    # Arrange
    db = _legacy_db(tmp_path, _seed_rows())
    # Act
    _run(db, "--commit")
    # Assert
    assert _query(
        "SELECT id FROM sac_channel_events WHERE target = %s ORDER BY id",
        ("lead",),
    ) == [(1,), (2,)]


def test_commit_carries_delivered_rows_too(tmp_path: Path, pg_schema: str) -> None:
    """Delivered rows are NOT dropped: ``list_since_id`` reads regardless."""
    # Arrange
    db = _legacy_db(tmp_path, _seed_rows())
    # Act
    _run(db, "--commit")
    # Assert
    assert _query(
        "SELECT delivered_at FROM sac_channel_events "
        "WHERE target = %s AND id = %s",
        ("lead", 1),
    ) == [(2.0,)]


def test_commit_keeps_meta_json_byte_identical(
    tmp_path: Path, pg_schema: str
) -> None:
    """Japanese survives because nothing re-encodes the stored string."""
    # Arrange
    rows = _seed_rows()
    expected = rows[1][4]
    db = _legacy_db(tmp_path, rows)
    # Act
    _run(db, "--commit")
    # Assert
    assert _query(
        "SELECT meta_json FROM sac_channel_events WHERE target = %s AND id = %s",
        ("lead", 2),
    ) == [(expected,)]


def test_commit_seeds_the_cursor_at_the_highest_id(
    tmp_path: Path, pg_schema: str
) -> None:
    """The next live event must get a fresh id, not collide with a carried one."""
    # Arrange
    db = _legacy_db(tmp_path, _seed_rows())
    # Act
    _run(db, "--commit")
    # Assert
    assert _query(
        "SELECT next_id FROM sac_channel_cursor WHERE target = %s", ("lead",)
    ) == [(2,)]


def test_the_next_persisted_event_gets_a_fresh_id(
    tmp_path: Path, pg_schema: str
) -> None:
    """End to end: migrate, then publish, and nothing is overwritten."""
    # Arrange
    from scitex_agent_container._state.state_db_channel import persist_event

    db = _legacy_db(tmp_path, _seed_rows())
    _run(db, "--commit")
    # Act
    minted = persist_event(target="lead", event={"msg_id": "d", "ts": 9.0})
    # Assert
    assert minted == 3


def test_a_second_run_moves_nothing(tmp_path: Path, pg_schema: str) -> None:
    """Idempotent: the re-run probe recognises this host's own rows."""
    # Arrange
    db = _legacy_db(tmp_path, _seed_rows())
    _run(db, "--commit")
    # Act
    _run(db, "--commit")
    # Assert
    assert _query(
        "SELECT COUNT(*) FROM sac_channel_events WHERE target = %s", ("lead",)
    ) == [(2,)]


def test_a_second_host_is_offset_above_the_first(
    tmp_path: Path, pg_schema: str
) -> None:
    """A relocated target's later rows go ABOVE, never over, the earlier ones.

    ``(target, id)`` is the primary key, so two hosts numbering from 1 cannot
    merge. The first host keeps 1 and 2; the second host's single row lands at
    3 rather than silently colliding with 1.
    """
    # Arrange
    first = _legacy_db(tmp_path / "host-a", _seed_rows())
    other = _legacy_db(
        tmp_path / "host-b",
        [
            (
                "lead",
                "bob",
                "message",
                "elsewhere",
                json.dumps({"msg_id": "z"}, ensure_ascii=False),
                4.0,
                None,
            )
        ],
    )
    _run(first, "--commit")
    # Act
    _run(other, "--commit")
    # Assert
    assert _query(
        "SELECT id FROM sac_channel_events WHERE target = %s ORDER BY id",
        ("lead",),
    ) == [(1,), (2,), (3,)]
