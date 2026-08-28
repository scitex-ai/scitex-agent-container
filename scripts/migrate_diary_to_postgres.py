#!/usr/bin/env python3
"""One-shot: copy the SQLite diary tables into per-host PostgreSQL.

Companion to the ``state_db_diary`` port (2026-08-28). The code stopped
reading SQLite; this moves the rows that are already there so the history is
not stranded in a file nothing opens any more.

Covers all three diary tables — ``heartbeats``, ``turns``, ``errors``.
Measured on compute-04 the night of the port: heartbeats 32, turns 0,
errors 0. The empty two are migrated anyway; "empty today" is a fact about
one host at one moment, not a property of the table.

RUN IT ON THE HOST, NOT IN A CONTAINER
======================================
``DEFAULT_DB_PATH`` resolves differently in the two places: a container gets
its own per-agent shard under ``/state/<agent>/state.db``, the host gets
``~/.scitex/agent-container/runtime/state.db``. A container run would
faithfully migrate an empty shard and report success — the same shape that
made this whole migration look finished for three days.

RUN IT TWICE
============
The tables are live: every heartbeat tick writes one. Run it once now,
restart the daemons so they pick up the new code, then run it again to sweep
anything written through the old path in between. The second run is expected
to find few or no stragglers; that it finds ANY is the reason it exists.

WHY IT DOES NOT CALL ``record_heartbeat`` / ``record_turn`` / ``record_error``
=============================================================================
Those stamp ``ts`` with ``time.time()`` when the caller omits it — correct for
a real tick, destructive here. A migration that rewrote every beat to the
migration's own clock would make the whole fleet look like it started beating
at the moment of the migration, which is the "backfilled timestamp makes old
things look new" failure. So this writes through the store directly,
preserving every recorded value verbatim.

AND IT DOES NOT SYNTHESISE THE IDS IT CANNOT KNOW
=================================================
SQLite gave ``heartbeat_id``/``error_id`` from ``lastrowid``. The Postgres
schema identifies a heartbeat by (name, host, ts) and an error by its own
``error_id`` column — so historical error ids are carried across verbatim,
and heartbeat rowids are DROPPED rather than invented. Nothing read them:
``record_heartbeat``'s return value is unused by every caller (checked), and
``latest_heartbeats_per_name`` selects on ts. Inventing a surrogate id here
would create a number that looks authoritative and means nothing.

A DRY RUN IS THE DEFAULT. Pass ``--commit`` to actually write. The bare
invocation reports what would move and touches nothing.

IT IS IDEMPOTENT and it VERIFIES: each record is read back through the same
dialect production reads through, and a mismatch is reported rather than
counted as a success. Nothing is deleted from SQLite — the old tables stay
as a fallback for a human, untouched.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

_HEARTBEAT_COLUMNS = ("name", "host", "pid", "state", "ts")
_TURN_COLUMNS = (
    "turn_id",
    "name",
    "host",
    "status",
    "prompt_text",
    "response_text",
    "ts",
    "session_id",
    "input_tokens",
    "output_tokens",
)
_ERROR_COLUMNS = ("error_id", "name", "host", "cause", "detail", "ts", "turn_id")


def default_db_path() -> Path:
    """The HOST state.db. See the module docstring on why this matters."""
    return Path.home() / ".scitex" / "agent-container" / "runtime" / "state.db"


def _read_rows(db_path: Path, table: str, columns: tuple[str, ...]) -> list[dict]:
    """Read one table, or return [] when it does not exist.

    A missing table is not an error: an older host may predate it. It is
    reported distinctly from an empty one, because "no such table" and
    "no rows" are different facts and collapsing them is how a migration
    silently skips a host.
    """
    if not db_path.is_file():
        return []
    conn = sqlite3.connect(db_path)
    try:
        conn.row_factory = sqlite3.Row
        try:
            cur = conn.execute(f"SELECT {', '.join(columns)} FROM {table}")
        except sqlite3.OperationalError as exc:
            if "no such table" in str(exc):
                print(f"  {table}: no such table in {db_path}")
                return []
            raise
        return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _migrate(table: str, rows: list[dict], schema_fn, commit: bool) -> int:
    """Write rows through the production store. Returns the verified count."""
    if not rows:
        print(f"  {table}: 0 rows — nothing to move")
        return 0
    if not commit:
        print(f"  {table}: {len(rows)} rows WOULD move (dry run)")
        return 0

    from scitex_dev.store import NEW_RECORD, RevisionMismatchError

    from scitex_agent_container._state.state_db_diary import _open

    # `expected_revision` is KEYWORD-ONLY WITH NO DEFAULT
    # (Store.put(self, values, *, expected_revision, owner=None, actor=None)).
    # This loop used to call `store.put({...})` with no revision, so `--commit`
    # raised TypeError on its FIRST row and this script had never carried a
    # single row on any host. The dry-run path returns above, so the failure
    # was invisible to anyone who ran the default.
    #
    # NEW_RECORD, not ANY_REVISION: a migration must never overwrite a row that
    # is already there. These tables order by their timestamps, so re-running
    # with ANY_REVISION would silently re-stamp history. Skipping a present key
    # makes the script IDEMPOTENT — safe to re-run after a partial move, which
    # is exactly what you want in the middle of a cutover.
    schema = schema_fn()
    identity = list(schema.identity_fields)
    store = _open(schema)
    written = 0
    already = 0
    try:
        for row in rows:
            values = {k: v for k, v in row.items()}
            key = {k: values[k] for k in identity}
            if store.get(key, include_hidden=True) is not None:
                already += 1
                continue
            try:
                store.put(values, expected_revision=NEW_RECORD)
                written += 1
            except RevisionMismatchError:
                # Another writer won the race between the get and the put.
                # The row exists, which is the goal — not an error.
                already += 1
    finally:
        store.close()
    if already:
        print(f"  {table}: {already} row(s) already present, left untouched")

    # VERIFY through the same dialect production reads through, rather than
    # trusting the write count. A put that silently no-ops would otherwise be
    # reported as a successful migration.
    store = _open(schema_fn())
    try:
        present = len(list(store.rows()))
    finally:
        store.close()
    if present < len(rows):
        print(
            f"  {table}: MISMATCH — wrote {written}, store holds {present} "
            f"(expected at least {len(rows)}). NOT a success."
        )
        return -1
    print(f"  {table}: {written} rows moved, {present} verified present")
    return written


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help="SQLite state.db to read (default: the HOST runtime state.db)",
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="actually write; without it this is a dry run that touches nothing",
    )
    args = parser.parse_args(argv)

    db_path = args.db_path or default_db_path()
    print(f"source: {db_path}{'' if db_path.is_file() else '  (ABSENT)'}")
    print(f"mode:   {'COMMIT' if args.commit else 'DRY RUN'}")

    from scitex_agent_container._state.state_db_diary import (
        _errors_schema,
        _heartbeats_schema,
        _turns_schema,
    )

    plan = (
        ("heartbeats", _HEARTBEAT_COLUMNS, _heartbeats_schema),
        ("turns", _TURN_COLUMNS, _turns_schema),
        ("errors", _ERROR_COLUMNS, _errors_schema),
    )

    failed = False
    for table, columns, schema_fn in plan:
        rows = _read_rows(db_path, table, columns)
        if _migrate(table, rows, schema_fn, args.commit) < 0:
            failed = True

    if failed:
        print("FAILED — at least one table did not verify. SQLite left untouched.")
        return 1
    if not args.commit:
        print("dry run complete — nothing was written. Re-run with --commit.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
