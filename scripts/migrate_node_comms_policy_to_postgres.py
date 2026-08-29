#!/usr/bin/env python3
"""One-shot: copy the SQLite ``node_comms_policy`` table into PostgreSQL.

Companion to the ``state_db_acl_policy`` port (2026-08-28). The code stopped
reading SQLite; this moves the rows already there so live ACL policy is not
stranded in a file nothing opens any more.

THIS IS THE ONE WHERE A MISSED ROW IS A PRIVILEGE CHANGE
=======================================================
The diary held history: losing a heartbeat row costs an observation.
``comms_grants`` held authorisation, and a missed grant DENIES a send.
``node_comms_policy`` is worse than either, because it fails in BOTH
directions and one of them is silent:

  * A missing row makes ``read_comms_policy`` return the all-allow defaults,
    so a capsule authored ``spec.comms.inbound.siblings=deny`` becomes
    REACHABLE by its siblings. Nothing logs. Nothing 403s. The isolation
    simply is not there.
  * A missing row also resolves to NO named group, so an agent whose spec
    says ``groups: [developer]`` is refused ``host_exec``, ``check_spawn``
    and agent CRUD — the exact 2026-08-09 escalation, where three agents
    were denied and spent fifteen minutes reading their own labels.

So run this BEFORE the restart that picks up the new code, and read the
verify line rather than the exit code.

A DRY RUN IS THE DEFAULT. Pass ``--commit`` to actually write.

RUN IT ON THE HOST, NOT IN A CONTAINER
======================================
``default_db_path`` resolves differently in the two places: a container gets
its own per-agent shard under ``/state/<agent>/state.db``, the host gets
``~/.scitex/agent-container/runtime/state.db``. This table is the one that
was MEASURED empty in a container and full on the host (scitex-compute-04,
2026-08-11), so a container run would migrate nothing and report success.

RUN IT TWICE
============
Policy rows are written by a live daemon: every ``agent_start`` upserts one
through ``persist_acl_policy``. Run it once now, restart so the daemons pick
up the new code, then run it again to sweep anything the old path wrote in
between.

WHY IT DOES NOT CALL ``record_comms_policy``
============================================
``record_comms_policy`` stamps ``updated_at`` with ``time.time()``. Calling
it here would rewrite every row's age to the migration moment and destroy
the only evidence of WHEN a policy last came from a spec — which is what
``sac agents refresh-acl`` staleness is diagnosed from. It would also run
the validators against values that are already in the table, so one
historically out-of-domain row would abort a migration rather than move.
The rows go through ``Store.put`` with their original stamps intact.

WHAT IT DOES NOT MIGRATE, and why that is correct
=================================================
Nothing is skipped, and nothing is retired. The SQLite table had no
tombstones — a policy was only ever upserted, never deleted — so there is no
retired history to carry. From here on ``retire_comms_policy`` hides rather
than deletes, and those retirements stay auditable; this script cannot
retroactively recover retirements that were never recorded.

IT NEVER TOUCHES THE SQLITE SIDE. The old table is left exactly as found, so
a re-run is safe and a rollback is "point the code back at SQLite".
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from scitex_agent_container._state.state_db_acl_policy import (  # noqa: E402
    POLICY_STORE,
    _open,
)

TABLE = "node_comms_policy"
COLUMNS = (
    "name",
    "outbound_siblings",
    "outbound_parent",
    "inbound_siblings",
    "inbound_parent",
    "lineage_group",
    "may_spawn",
    "group_name",
    "group_names",
    "updated_at",
)


def default_db_path() -> Path:
    """The host's state.db, or the container's per-agent shard."""
    env = os.environ.get("SCITEX_AGENT_CONTAINER_STATE_DB")
    if env:
        return Path(env)
    return Path.home() / ".scitex" / "agent-container" / "runtime" / "state.db"


def _read_rows(db_path: Path) -> list[dict]:
    """Every policy row in the SQLite table, in rowid (insertion) order.

    Columns are read through ``PRAGMA table_info`` rather than assumed: this
    table grew ``group_name`` and then ``group_names`` by ALTER TABLE, so a
    host that never ran those migrations has a narrower table and a literal
    SELECT of all ten columns would raise instead of migrating what is there.
    A missing column falls back to the same default its DDL declared.
    """
    if not db_path.is_file():
        return []
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    try:
        present = [
            r["name"] for r in conn.execute(f"PRAGMA table_info({TABLE})")  # noqa: S608
        ]
        if not present:
            return []
        selected = [c for c in COLUMNS if c in present]
        cur = conn.execute(
            f"SELECT {', '.join(selected)} FROM {TABLE} ORDER BY rowid ASC"  # noqa: S608
        )
        return [{c: r[c] for c in selected} for r in cur.fetchall()]
    finally:
        conn.close()


def _record(row: dict) -> dict:
    """One SQLite row as the store's record, defaults filled for absent cols."""
    return {
        "name": str(row["name"]),
        "outbound_siblings": str(row.get("outbound_siblings") or "allow"),
        "outbound_parent": str(row.get("outbound_parent") or "allow"),
        "inbound_siblings": str(row.get("inbound_siblings") or "allow"),
        "inbound_parent": str(row.get("inbound_parent") or "allow"),
        "lineage_group": str(row.get("lineage_group") or ""),
        # SQLite stored 0/1; the store's field is a real BOOL.
        "may_spawn": bool(row.get("may_spawn", 1)),
        "group_name": str(row.get("group_name") or ""),
        "group_names": str(row.get("group_names") or ""),
        "updated_at": float(row.get("updated_at") or 0.0),
    }


def _migrate(rows: list[dict], commit: bool) -> tuple[int, int]:
    """Returns ``(written, already_present)``.

    Run-twice safe by READING FIRST: a name already in the store is left
    exactly as it stands. That direction is deliberate — the store's copy may
    have been written by a live ``agent_start`` since the first pass, and
    that copy is NEWER than the SQLite one. Overwriting it would roll a
    fresh spec back to whatever the abandoned file remembers.
    """
    from scitex_dev.store import NEW_RECORD, RevisionMismatchError

    if not commit:
        return (len(rows), 0)

    written = present = 0
    store = _open()
    try:
        for row in rows:
            key = {"name": str(row["name"])}
            if store.get(key, include_hidden=True) is not None:
                present += 1
                continue
            try:
                store.put(_record(row), expected_revision=NEW_RECORD)
                written += 1
            except RevisionMismatchError:
                # Another writer arrived between the read and the put. Not an
                # error: the record exists, which is the goal.
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
    print(f"target: {POLICY_STORE}")

    rows = _read_rows(db_path)
    if not rows:
        print(f"{TABLE}: 0 rows in SQLite — nothing to move")
        if args.commit:
            print(f"verify: {_verify()} record(s) in the store")
        return 0

    written, present = _migrate(rows, args.commit)
    if args.commit:
        print(f"{TABLE}: {written} written, {present} already present")
        print(f"verify: {_verify()} record(s) in the store (read back)")
    else:
        print(f"{TABLE}: {written} rows WOULD move (pass --commit)")
        for row in rows:
            groups = row.get("group_names") or row.get("group_name") or "(ungrouped)"
            print(f"  {row['name']}: groups={groups}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
