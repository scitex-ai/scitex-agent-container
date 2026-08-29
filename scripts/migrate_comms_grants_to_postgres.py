#!/usr/bin/env python3
"""One-shot: copy the SQLite ``comms_grants`` table into PostgreSQL.

Companion to the ``state_db_grants`` port (2026-08-28). The code stopped
reading SQLite; this moves the rows already there so live permissions are not
stranded in a file nothing opens any more.

THIS ONE IS NOT LIKE THE DIARY. The diary held history: losing a heartbeat row
costs an observation. ``comms_grants`` holds AUTHORISATION. A grant that fails
to migrate does not go missing quietly — it DENIES a send that used to be
allowed, and the agent on the other end sees a refusal it cannot explain. So
run this BEFORE the restart that picks up the new code, not after, and read
the verify line rather than the exit code.

A DRY RUN IS THE DEFAULT. Pass ``--commit`` to actually write.

RUN IT ON THE HOST, NOT IN A CONTAINER
======================================
``default_db_path`` resolves differently in the two places: a container gets
its own per-agent shard under ``/state/<agent>/state.db``, the host gets
``~/.scitex/agent-container/runtime/state.db``. A container run would migrate
an empty shard and report success — the same shape that let the state-write
outage look finished for four days.

RUN IT TWICE
============
Grants are written by a live daemon (``_lifecycle/_instances`` auto-grants on
spawn, and the approval path grants on unblock). Run it once now, restart so
the daemons pick up the new code, then run it again to sweep anything the old
path wrote in between.

WHY IT DOES NOT CALL ``grant_send``
===================================
``grant_send`` stamps ``created_at`` with ``time.time()``. Calling it here
would rewrite every grant's age to the migration moment and destroy exactly
the audit data the column exists for — "since when could this agent send
there?" would answer "since the migration", for all of them. The rows are
written through ``Store.put`` with their original timestamps intact.

WHAT IT DOES NOT MIGRATE, and why that is correct
=================================================
Nothing. The SQLite table has no revoked rows to carry — a revoke was a
DELETE, so the history it would have carried is already gone. The new store
hides instead of deleting, so revocations from here on stay auditable; this
script cannot retroactively recover the ones that were deleted before it.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scitex_agent_container._state.state_db_grants import (  # noqa: E402
    GRANTS_STORE,
    _open,
)

TABLE = "comms_grants"
COLUMNS = ("sender_name", "target_name", "created_at", "note")


def default_db_path() -> Path:
    """The host's state.db, or the container's per-agent shard."""
    env = os.environ.get("SCITEX_AGENT_CONTAINER_STATE_DB")
    if env:
        return Path(env)
    return Path.home() / ".scitex" / "agent-container" / "runtime" / "state.db"


def _read_rows(db_path: Path) -> list[dict]:
    """Every grant in the SQLite table, in rowid (insertion) order.

    rowid order on the way OUT matters as much as it did on the way in: the
    new store orders by HLC, and writing the rows in their original sequence
    is what makes the two orders agree.
    """
    if not db_path.is_file():
        return []
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        names = {r["name"] for r in conn.execute("PRAGMA table_info(comms_grants)")}
        if not names:
            return []
        cur = conn.execute(
            f"SELECT {', '.join(COLUMNS)} FROM {TABLE} ORDER BY rowid ASC"
        )
        return [{c: r[c] for c in COLUMNS} for r in cur.fetchall()]
    finally:
        conn.close()


def _migrate(rows: list[dict], commit: bool) -> tuple[int, int]:
    """Returns ``(written, already_present)``."""
    from scitex_dev.store import NEW_RECORD
    from scitex_dev.store._errors import RevisionMismatchError

    if not commit:
        return (len(rows), 0)

    written = present = 0
    store = _open()
    try:
        for row in rows:
            key = {
                "sender_name": row["sender_name"],
                "target_name": row["target_name"],
            }
            if store.get(key, include_hidden=True) is not None:
                present += 1
                continue
            try:
                store.put(
                    {
                        "sender_name": row["sender_name"],
                        "target_name": row["target_name"],
                        "created_at": float(row["created_at"]),
                        "note": row["note"],
                    },
                    expected_revision=NEW_RECORD,
                )
                written += 1
            except RevisionMismatchError:
                # Another writer got there between the read and the put.
                # Not an error: the row exists, which is the goal.
                present += 1
    finally:
        store.close()
    return (written, present)


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
    print(f"target: {GRANTS_STORE}")

    rows = _read_rows(db_path)
    if not rows:
        print(f"{TABLE}: 0 rows in SQLite — nothing to move")
        if args.commit:
            print(f"verify: {_verify()} row(s) in the store")
        return 0

    written, present = _migrate(rows, args.commit)
    if args.commit:
        print(f"{TABLE}: {written} written, {present} already present")
        print(f"verify: {_verify()} row(s) in the store (read back)")
    else:
        print(f"{TABLE}: {written} rows WOULD move (pass --commit)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
