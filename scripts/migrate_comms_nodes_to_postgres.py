#!/usr/bin/env python3
"""One-shot: copy the SQLite ``comms_nodes`` table into PostgreSQL.

Companion to the ``state_db_comms_nodes`` port (2026-08-28). The code stopped
reading SQLite; this moves the rows already there so the live A2A peer
directory is not stranded in a file nothing opens any more.

WHAT IS AT STAKE HERE IS ROUTING, NOT HISTORY. The diary held observations —
losing a heartbeat row costs a data point. ``comms_nodes`` answers "where do I
POST to reach agent X?". A row that fails to migrate does not go missing
quietly: every cross-host send to that name resolves to ``None`` and the
caller reports ``unknown_target``, which reads as "that agent does not exist"
rather than as "the directory lost it". So run this BEFORE the restart that
picks up the new code, and read the verify line rather than the exit code.

A DRY RUN IS THE DEFAULT. Pass ``--commit`` to actually write.

RUN IT ON THE HOST, NOT IN A CONTAINER
======================================
``default_db_path`` resolves differently in the two places: a container gets
its own per-agent shard under ``/state/<agent>/state.db``, the host gets
``~/.scitex/agent-container/runtime/state.db``. A container run would migrate
an empty shard and report success — the same shape that let the state-write
outage look finished for four days.

RUN IT ONCE PER HOST, AND THEN AGAIN
====================================
Two separate reasons, and they compound:

* comms_nodes is the one sac table that was genuinely PER-HOST-DIVERGENT by
  design — every host wrote its own rows and ``sac registry sync`` ssh-pulled
  the others. So each host's state.db holds rows no other host has, and this
  script must run on EVERY host that ever registered a node. The target is one
  shared store, so the runs converge rather than conflict.
* Rows are written by live daemons (``sac listen`` self-register, the
  ``sac mcp channel`` refresh task, the spec-driven paired write on agent
  start). Run it once now, restart so the daemons pick up the new code, then
  run it again to sweep anything the old path wrote in between.

Run-twice safety comes from reading the target first: a name already present
is counted and skipped, never overwritten. That also makes the SECOND host's
run non-destructive when two hosts happen to hold the same name.

WHY IT DOES NOT CALL ``register_comms_node``
============================================
Three independent reasons, any one of which is sufficient:

* It stamps ``registered_at``/``updated_at`` with ``time.time()``, which would
  rewrite every node's age to the migration moment and destroy the audit data
  those columns exist for.
* It RAISES ``CommsNodeConflictError`` on a name whose stored target differs —
  precisely the case a multi-host migration produces — so a legitimate
  cross-host difference would abort a row instead of being reported.
* It refuses to write a TOMBSTONE at all. Rows are written through
  ``Store.put`` with their original timestamps, and a row whose ``ended_at``
  is set is additionally ``hide()``-den, so a tombstone stays a tombstone.

WHAT IT DOES NOT MIGRATE, and why that is correct
=================================================
Nothing is skipped. Tombstoned rows are carried over as HIDDEN records rather
than dropped: under the old schema ``ended_at`` was deliberately preserved
("so the next export_state carries the deletion to peers"), and dropping them
here would resurrect every stopped agent as absent-and-therefore-registrable,
losing the record that the name was ever claimed.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scitex_agent_container._state.state_db_comms_nodes import (  # noqa: E402
    COMMS_NODES_STORE,
    _open,
)

TABLE = "comms_nodes"
COLUMNS = (
    "name",
    "host",
    "a2a_port",
    "registered_at",
    "updated_at",
    "source_host",
    "ended_at",
)


def default_db_path() -> Path:
    """The host's state.db, or the container's per-agent shard."""
    env = os.environ.get("SCITEX_AGENT_CONTAINER_STATE_DB")
    if env:
        return Path(env)
    return Path.home() / ".scitex" / "agent-container" / "runtime" / "state.db"


def read_rows(db_path: Path) -> list[dict]:
    """Every comms_nodes row, live and tombstoned, in rowid order.

    Tombstoned rows are INCLUDED — see the module docstring. rowid order is
    kept for reproducibility of the log, not because the target depends on it:
    the store's listing is ordered by ``name``, which this cannot disturb.
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
            f"SELECT {', '.join(COLUMNS)} FROM {TABLE} ORDER BY rowid ASC"
        )
        return [{c: r[c] for c in COLUMNS} for r in cur.fetchall()]
    finally:
        conn.close()


def migrate(rows: list[dict], commit: bool) -> tuple[int, int, int]:
    """Returns ``(written, tombstoned, already_present)``."""
    from scitex_dev.store import ANY_REVISION, NEW_RECORD, RevisionMismatchError

    if not commit:
        tombstones = sum(1 for r in rows if r["ended_at"] is not None)
        return (len(rows), tombstones, 0)

    written = tombstoned = present = 0
    store = _open()
    try:
        for row in rows:
            key = {"name": row["name"]}
            # include_hidden: a tombstoned record still occupies the identity,
            # so a default read would call it absent and the put would collide.
            if store.get(key, include_hidden=True) is not None:
                present += 1
                continue
            try:
                store.put(
                    {
                        "name": row["name"],
                        "host": row["host"],
                        "a2a_port": int(row["a2a_port"]),
                        "registered_at": float(row["registered_at"]),
                        "updated_at": float(row["updated_at"]),
                        "source_host": row["source_host"],
                        "ended_at": (
                            float(row["ended_at"])
                            if row["ended_at"] is not None
                            else None
                        ),
                    },
                    expected_revision=NEW_RECORD,
                )
            except RevisionMismatchError:
                # Another writer got there between the read and the put.
                # Not an error: the record exists, which is the goal.
                present += 1
                continue
            written += 1
            if row["ended_at"] is not None:
                # ``hidden`` is the liveness truth in the new store; the
                # ``ended_at`` value above is only the audit stamp. Writing
                # one without the other would resurrect a stopped node.
                store.hide(key, expected_revision=ANY_REVISION)
                tombstoned += 1
    finally:
        store.close()
    return (written, tombstoned, present)


def verify() -> tuple[int, int]:
    """Read back what is actually IN the store: ``(total, live)``."""
    store = _open()
    try:
        total = len(store.rows(include_hidden=True))
        live = len(store.rows())
        return (total, live)
    finally:
        store.close()


def run(argv: list[str] | None = None) -> int:
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
    print(f"target: {COMMS_NODES_STORE}")

    rows = read_rows(db_path)
    if not rows:
        print(f"{TABLE}: 0 rows in SQLite — nothing to move")
        if args.commit:
            total, live = verify()
            print(f"verify: {total} record(s) in the store, {live} live")
        return 0

    written, tombstoned, present = migrate(rows, args.commit)
    if args.commit:
        print(
            f"{TABLE}: {written} written ({tombstoned} of them tombstoned), "
            f"{present} already present"
        )
        total, live = verify()
        print(f"verify: {total} record(s) in the store, {live} live (read back)")
    else:
        print(
            f"{TABLE}: {written} rows WOULD move "
            f"({tombstoned} of them tombstoned) — pass --commit"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
