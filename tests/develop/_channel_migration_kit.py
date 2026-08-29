#!/usr/bin/env python3
"""Shared fixtures for the ``migrate_channel_events_to_postgres.py`` suites.

Extracted when the overlapping-residency and table-ownership cases arrived:
both new modules build the same real SQLite file and run the same real script,
and the alternative to one shared module is two more copies of it.

``test_migrate_channel_events.py`` DELIBERATELY STILL CARRIES ITS OWN COPY.
Those tests are the negative control for this whole change — they pin the
post-cutover hazard the guard exists for — and a reviewer has to be able to
see that not one character of them moved. Rewriting them to import from here
would put that reassurance behind a diff. The duplication is the price, and it
is the cheaper half of the trade.

NO MOCKS ANYWHERE IN HERE. A real ``sqlite3`` file under ``tmp_path``, the
real script loaded from ``scripts/``, and a real PostgreSQL reached through
the ``pg_schema`` fixture. The whole point of these suites is that the moving
parts are the ones an operator runs.
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

REPO = Path(__file__).resolve().parents[2]
SCRIPT = REPO / "scripts" / "migrate_channel_events_to_postgres.py"

JAPANESE = "作業中断はしてほしくない"


@contextlib.contextmanager
def loaded() -> Iterator[Any]:
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


def legacy_db(tmp_path: Path, rows: list[tuple]) -> Path:
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


def event_row(target: str, msg_id: str, ts: float, *, delivered: float | None = None):
    """One ``channel_events`` row, with a distinct envelope per ``msg_id``.

    The envelope has to differ per row: ``_offset_for`` probes by CONTENT, so
    two rows sharing a ``meta_json`` would make the re-run probe ambiguous —
    which is a real property of the production data, not a fixture detail.
    """
    return (
        target,
        "alice",
        "message",
        msg_id,
        json.dumps({"msg_id": msg_id}, ensure_ascii=False),
        ts,
        delivered,
    )


def seed_rows() -> list[tuple]:
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


def run(db: Path, *extra: str) -> int:
    with loaded() as module:
        return int(module.main(["--db-path", str(db), *extra]))


def query(sql: str, params: tuple = ()) -> list[tuple]:
    from scitex_agent_container._state.state_db_channel_store import (
        new_channel_connection,
    )

    conn = new_channel_connection()
    try:
        return list(conn.execute(sql, params).fetchall())
    finally:
        conn.close()


def execute(sql: str, params: tuple = ()) -> None:
    """Run a statement that returns no rows. Separate from :func:`query`
    because psycopg raises rather than handing back an empty list."""
    from scitex_agent_container._state.state_db_channel_store import (
        new_channel_connection,
    )

    conn = new_channel_connection()
    try:
        conn.execute(sql, params)
    finally:
        conn.close()


@contextlib.contextmanager
def raw_conn() -> Iterator[Any]:
    """A connection to the store with NO DDL applied.

    ``new_channel_connection`` creates the tables on open, which is right for
    every other caller and wrong for anything asserting about what exists or
    who owns it — it would create, and own, the very thing under test.
    """
    import psycopg

    from scitex_agent_container._state.state_db_channel_store import (
        _resolve_target,
    )

    with psycopg.connect(str(_resolve_target().dsn), autocommit=True) as conn:
        yield conn


def raw_query(sql: str, params: tuple = ()) -> list[tuple]:
    """Ask the database WITHOUT applying the DDL first."""
    with raw_conn() as conn:
        return list(conn.execute(sql, params).fetchall())


def script_module(name: str) -> Any:
    """Import one of the migration's sibling helpers from ``scripts/``.

    They have no package home — the migrations reach each other by being RUN
    from that directory — so a test that wants to exercise one directly has to
    supply the ``sys.path`` entry the shell would.
    """
    import importlib

    saved = list(sys.path)
    sys.path.insert(0, str(SCRIPT.parent))
    try:
        return importlib.import_module(name)
    finally:
        sys.path[:] = saved
