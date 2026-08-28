#!/usr/bin/env python3
"""One-shot: copy the SQLite ``instances`` + ``events`` rows into PostgreSQL.

Companion to the ``state_db_instances`` port (2026-08-28). The code stopped
reading SQLite; this moves the rows already there so the fleet's record of
who ran where is not stranded in a file nothing opens any more.

WHAT IS AT STAKE HERE IS DIFFERENT AGAIN
========================================
The diary held observations and ``comms_grants`` held authorisation. This
table holds the LEASE. ``ended_at IS NULL`` is what tells ``sac agents
start`` an agent is already running, what tells the forwarder where to POST,
and what tells the reconciler not to restart something that is alive. A
migration that loses an ACTIVE row does not merely forget history — it
un-leases a running agent, and the next start will happily launch a second
copy of it.

So the order matters, and it is the opposite of the grants script's:

  1. Run this (with ``--commit``) BEFORE restarting the daemons, so the
     live leases exist in PostgreSQL when the new code first looks.
  2. Restart, so the writers pick up the new code.
  3. Run it AGAIN, to sweep whatever the old path wrote in between.

Read the verify line, not the exit code.

A DRY RUN IS THE DEFAULT. Pass ``--commit`` to actually write.

RUN IT ON THE HOST, NOT IN A CONTAINER
======================================
``default_db_path`` resolves differently in the two places: a container gets
its own per-agent shard under ``/state/<agent>/state.db``, the host gets
``~/.scitex/agent-container/runtime/state.db``. A container run would migrate
an empty shard and report success — the same shape that let the state-write
outage look finished for four days.

WHY IT DOES NOT CALL ``record_instance_start`` / ``record_instance_stop``
========================================================================
Both mint their own values. ``record_instance_start`` generates a FRESH
uuid7 ``id`` and stamps ``started_at`` with ``now_iso()``; ``record_instance_
stop`` stamps ``ended_at`` the same way. Calling them here would give every
migrated agent a new identity and a start time of "the migration", which
destroys the two things this table is read for — the lineage edges that join
``spawned_by`` to an ``id``, and the placement history the #192 fail-loud
resolver reports. Rows go through ``put_instance_record``, which writes what
it is handed.

WHY THE EVENTS ARE MIGRATED AT ALL
==================================
Their SQLite ``id`` was an AUTOINCREMENT nothing read, and their content is
two lines per instance. They come anyway, because ``instance_events`` is the
only place a ``stop`` carries its ``exit_reason`` as a timestamped record
rather than as a mutable column, and dropping it would leave the new store
with a lifecycle log that starts abruptly on migration day.

An event whose ``instance_id`` names no migrated instance is still copied.
The alternative — dropping it — would quietly delete the evidence of exactly
the case worth investigating: an event for an instance row somebody removed
by hand.

NOTHING IS DELETED FROM SQLITE. The old tables stay as a human's fallback.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scitex_agent_container._state.state_db_instance_events import (  # noqa: E402
    EVENTS_STORE,
)
from scitex_agent_container._state.state_db_instance_events import (
    _open as _open_events,
)
from scitex_agent_container._state.state_db_instances import (  # noqa: E402
    INSTANCES_STORE,
    put_instance_record,
)
from scitex_agent_container._state.state_db_instances_store import (  # noqa: E402
    INSTANCE_DEFAULTS,
    open_instances_store,
)

INSTANCE_COLUMNS = tuple(INSTANCE_DEFAULTS)
EVENT_COLUMNS = ("ts", "instance_id", "definition_id", "kind", "actor", "payload_json")


def default_db_path() -> Path:
    """The host's state.db, or the container's per-agent shard."""
    env = os.environ.get("SCITEX_AGENT_CONTAINER_STATE_DB")
    if env:
        return Path(env)
    return Path.home() / ".scitex" / "agent-container" / "runtime" / "state.db"


def _read_rows(db_path: Path, table: str, columns: tuple[str, ...]) -> list[dict]:
    """Every row of ``table``, in rowid (insertion) order.

    "No such table" and "no rows" are distinguished deliberately — the
    caller prints them differently, because collapsing them is how a
    migration silently skips a host.

    Only columns the table ACTUALLY has are selected. A pre-family-tree
    ``instances`` (no ``bound_port`` / ``remote`` / ``spawned_by``) exists on
    hosts that never ran the ADD COLUMN migration, and a blind ``SELECT
    bound_port`` would abort the whole migration on those.
    """
    if not db_path.is_file():
        return []
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        present = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if not present:
            return []
        cols = [c for c in columns if c in present]
        cur = conn.execute(
            f"SELECT {', '.join(cols)} FROM {table} ORDER BY rowid ASC"  # noqa: S608
        )
        return [{c: r[c] for c in cols} for r in cur.fetchall()]
    finally:
        conn.close()


def _migrate_instances(rows: list[dict], commit: bool) -> tuple[int, int]:
    """Returns ``(written, already_present)``."""
    if not commit:
        return (len(rows), 0)
    written = present = 0
    for row in rows:
        if put_instance_record(row):
            written += 1
        else:
            present += 1
    return (written, present)


def _migrate_events(rows: list[dict], commit: bool) -> tuple[int, int]:
    """Returns ``(written, already_present)``.

    Rows with no ``instance_id`` are skipped and counted as present: the
    new identity is ``(instance_id, kind, ts)`` and a NULL cannot key a
    record. The SQLite DDL allowed the column to be NULL for
    definition-scoped events; measured, nothing ever wrote one.
    """
    if not commit:
        return (len([r for r in rows if r.get("instance_id")]), 0)

    from scitex_dev.store import NEW_RECORD, RevisionMismatchError

    written = present = 0
    store = _open_events()
    try:
        for row in rows:
            instance_id = row.get("instance_id")
            if not instance_id:
                present += 1
                continue
            key = {
                "instance_id": instance_id,
                "kind": row.get("kind"),
                "ts": row.get("ts"),
            }
            if store.get(key, include_hidden=True) is not None:
                present += 1
                continue
            try:
                store.put(
                    {
                        **key,
                        "definition_id": row.get("definition_id"),
                        "actor": row.get("actor"),
                        "payload_json": row.get("payload_json"),
                    },
                    expected_revision=NEW_RECORD,
                )
                written += 1
            except RevisionMismatchError:
                # Another writer got there between the read and the put.
                # Not an error: the record exists, which is the goal.
                present += 1
    finally:
        store.close()
    return (written, present)


def _verify() -> tuple[int, int]:
    """Count what is actually IN the stores, by reading them back."""
    store = open_instances_store()
    try:
        instances = len(store.rows(include_hidden=True))
    finally:
        store.close()
    store = _open_events()
    try:
        events = len(store.rows(include_hidden=True))
    finally:
        store.close()
    return (instances, events)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--commit",
        action="store_true",
        help="actually write; without it this is a dry run",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help="SQLite state.db to read from (default: the host's)",
    )
    args = parser.parse_args(argv)

    db_path = args.db_path or default_db_path()
    print(f"mode:   {'COMMIT' if args.commit else 'DRY RUN'}")
    print(f"source: {db_path}")
    print(f"target: {INSTANCES_STORE}, {EVENTS_STORE}")

    instances = _read_rows(db_path, "instances", INSTANCE_COLUMNS)
    events = _read_rows(db_path, "events", EVENT_COLUMNS)

    for table, rows, migrate in (
        ("instances", instances, _migrate_instances),
        ("events", events, _migrate_events),
    ):
        if not rows:
            print(f"{table}: 0 rows in SQLite — nothing to move")
            continue
        written, present = migrate(rows, args.commit)
        if args.commit:
            print(f"{table}: {written} written, {present} already present")
        else:
            print(f"{table}: {written} rows WOULD move (pass --commit)")

    if args.commit:
        n_instances, n_events = _verify()
        print(
            f"verify: {n_instances} instance(s), {n_events} event(s) "
            f"in the stores (read back)"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
