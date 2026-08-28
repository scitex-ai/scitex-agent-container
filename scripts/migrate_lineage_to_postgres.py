#!/usr/bin/env python3
"""One-shot: copy the SQLite ``lineage`` table into PostgreSQL.

Companion to the ``_lineage`` port (2026-08-28). The code stopped reading
SQLite; this moves the edges already there so the family tree is not stranded
in a file nothing opens any more.

WHAT AN UNMIGRATED EDGE COSTS, precisely
========================================
A lineage edge is not history the way a heartbeat is. Three gates read it:

  * ``spawn_allowed`` — a node with NO parent edge is a ROOT, and roots may
    spawn. So a missing edge does not deny; it PROMOTES. Every child whose
    edge fails to migrate silently acquires spawn authority it never had.
  * ``check_lineage_acl`` — ``target in descendants_of(caller)``. A missing
    edge here DENIES: the parent loses the ability to manage its own child.
  * ``derive_group`` — the default-ACL mesh. A missing edge collapses an
    agent to a singleton group, so intra-group sends start being refused.

One direction over-permits and the other under-permits, which is why the
verify line matters more than the exit code. Run this BEFORE the restart that
picks up the new code, and read what it printed.

A DRY RUN IS THE DEFAULT. Pass ``--commit`` to actually write.

RUN IT ON THE HOST, NOT IN A CONTAINER
======================================
``default_db_path`` resolves differently in the two places: a container gets
its own per-agent shard under ``/state/<agent>/state.db``, the host gets
``~/.scitex/agent-container/runtime/state.db``. A container run would migrate
an empty shard and report success — the same shape that let the state-write
outage look finished for four days. Measured on scitex-compute-04: the
in-container shard held ``lineage 0`` while the bare host held ``lineage 4``.

RUN IT TWICE
============
Edges are written by a live daemon (``_lifecycle/_spawn_gate`` records one on
every accepted spawn, and ``_listen/_agent_exec`` on every brokered start).
Run it once now, restart so the daemons pick up the new code, then run it
again to sweep anything the old path wrote in between.

WHY IT DOES NOT CALL ``record_lineage``
=======================================
``record_lineage`` stamps ``created_at`` with ``time.time()``. Calling it here
would rewrite every edge's age to the migration moment and destroy exactly the
audit data the column exists for — "when was this agent spawned, and by
whom?" would answer "at the migration", for all of them. The rows are written
through ``Store.put`` with their original timestamps intact.

KEEP-FIRST-PARENT IS PRESERVED, NOT RE-DECIDED
==============================================
An edge already in the store WINS. This script never overwrites one, even when
SQLite disagrees about the parent: ``record_lineage``'s rule is that the first
parent recorded is the parent, and a migration is not the place to change who
that was. A disagreement is REPORTED (``conflicts:``) rather than resolved,
because "two databases claim different parents for one child" is a fact an
operator needs to see, not one a script should quietly pick a side in.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scitex_agent_container._state._lineage import (  # noqa: E402
    LINEAGE_STORE,
    _open,
)

TABLE = "lineage"
COLUMNS = ("child_name", "parent_name", "created_at")


def default_db_path() -> Path:
    """The host's state.db, or the container's per-agent shard."""
    env = os.environ.get("SCITEX_AGENT_CONTAINER_STATE_DB")
    if env:
        return Path(env)
    return Path.home() / ".scitex" / "agent-container" / "runtime" / "state.db"


def _read_rows(db_path: Path) -> list[dict]:
    """Every edge in the SQLite table, in rowid (insertion) order.

    rowid order on the way OUT matters as much as it did on the way in: the
    new store orders by HLC, and writing the edges in their original sequence
    is what makes the two orders agree.

    A missing file, or a file whose ``lineage`` table was already dropped by
    the schema change, yields no rows rather than an error — this script has
    to be safe to run on a host that has nothing to move.
    """
    if not db_path.is_file():
        return []
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        names = {r["name"] for r in conn.execute(f"PRAGMA table_info({TABLE})")}
        if not names:
            return []
        cur = conn.execute(
            f"SELECT {', '.join(COLUMNS)} FROM {TABLE} ORDER BY rowid ASC"  # noqa: S608
        )
        return [{c: r[c] for c in COLUMNS} for r in cur.fetchall()]
    finally:
        conn.close()


def _migrate(rows: list[dict], commit: bool) -> tuple[int, int, list[str]]:
    """Returns ``(written, already_present, conflicts)``.

    ``conflicts`` names each child whose stored parent differs from the one
    SQLite holds. Reported, never resolved — see the module docstring.
    """
    from scitex_dev.store import NEW_RECORD, RevisionMismatchError

    if not commit:
        return (len(rows), 0, [])

    written = present = 0
    conflicts: list[str] = []
    store = _open()
    try:
        for row in rows:
            child = str(row["child_name"])
            parent = str(row["parent_name"])
            existing = store.get({"child_name": child}, include_hidden=True)
            if existing is not None:
                present += 1
                recorded = str(existing.values["parent_name"])
                if recorded != parent:
                    conflicts.append(
                        f"{child}: store says parent={recorded!r}, "
                        f"sqlite says parent={parent!r} (store KEPT)"
                    )
                continue
            try:
                store.put(
                    {
                        "child_name": child,
                        "parent_name": parent,
                        "created_at": float(row["created_at"]),
                    },
                    expected_revision=NEW_RECORD,
                )
                written += 1
            except RevisionMismatchError:
                # Another writer got there between the read and the put.
                # Not an error: the edge exists, which is the goal.
                present += 1
    finally:
        store.close()
    return (written, present, conflicts)


def _verify() -> int:
    """Count what is actually IN the store, by reading it back."""
    store = _open()
    try:
        return len(store.rows(include_hidden=True))
    finally:
        store.close()


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
    print(f"target: {LINEAGE_STORE}")

    rows = _read_rows(db_path)
    if not rows:
        print(f"{TABLE}: 0 rows in SQLite — nothing to move")
        if args.commit:
            print(f"verify: {_verify()} edge(s) in the store")
        return 0

    written, present, conflicts = _migrate(rows, args.commit)
    if args.commit:
        print(f"{TABLE}: {written} written, {present} already present")
        for line in conflicts:
            print(f"conflict: {line}")
        print(f"verify: {_verify()} edge(s) in the store (read back)")
    else:
        print(f"{TABLE}: {written} edges WOULD move (pass --commit)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
