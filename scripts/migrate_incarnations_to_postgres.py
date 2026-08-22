#!/usr/bin/env python3
"""One-shot: copy the SQLite ``incarnations`` table into per-host PostgreSQL.

Companion to the ``state_db_incarnations`` port (2026-08-19). The code stopped
reading SQLite; this moves the rows that are already there so the history is
not stranded in a file nothing opens any more.

RUN IT ON THE HOST, NOT IN A CONTAINER
======================================
``DEFAULT_DB_PATH`` resolves differently in the two places: a container gets
its own per-agent shard under ``/state/<agent>/state.db``, the host gets
``~/.scitex/agent-container/runtime/state.db``. The rows are on the HOST —
measured 2026-08-19, compute-04 held 242 of them — so a container run would
faithfully migrate an empty shard and report success.

RUN IT TWICE
============
The table is live: ``sac`` writes a birth certificate on every launch. Run it
once now, restart the daemons so they pick up the new code, then run it again
to sweep anything written through the old path in between. The second run is
expected to find few or no stragglers; that it finds ANY is the reason it
exists.

WHY IT DOES NOT CALL ``record_incarnation_birth``
=================================================
That function stamps ``born_at`` with ``now_iso()`` — correct for a real
launch, destructive here. A migration that rewrote every birth time to the
migration's own clock would look like the whole fleet was born at 00:40 on
2026-08-20, which is precisely the "backfilled timestamp makes old things
look new" failure. So this writes through the store directly, preserving
every recorded value verbatim.

IT IS IDEMPOTENT and it VERIFIES: each record is read back through the same
dialect production reads through, and a mismatch is reported rather than
counted as a success. Nothing is deleted from SQLite — the old table stays
as a fallback for a human, untouched.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

_COLUMNS = (
    "incarnation_id",
    "agent_id",
    "spec_id",
    "spec_git_sha",
    "host",
    "born_at",
    "compiled_spec_json",
    "exit_reason",
    "exit_code",
    "exited_at",
)


def _sqlite_rows(db_path: Path) -> list[dict]:
    """Every incarnations row, read-only. Empty list when the table is gone."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        cols = ", ".join(_COLUMNS)
        return [dict(r) for r in conn.execute(f"SELECT {cols} FROM incarnations")]
    except sqlite3.OperationalError as exc:
        print(f"  no incarnations table in {db_path}: {exc}")
        return []
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--db",
        type=Path,
        default=None,
        help="SQLite state.db to read (default: sac's resolved DEFAULT_DB_PATH)",
    )
    ap.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would move, write nothing",
    )
    args = ap.parse_args()

    from scitex_dev.store import ANY_REVISION

    from scitex_agent_container._state.state_db import DEFAULT_DB_PATH
    from scitex_agent_container._state.state_db_incarnations import (
        get_incarnation,
        incarnation_store_target,
        open_incarnation_store,
    )

    db_path = args.db or DEFAULT_DB_PATH
    print(f"source (sqlite) : {db_path}")
    print(f"target (postgres): {incarnation_store_target().locator}")

    if not Path(db_path).exists():
        print("source does not exist — nothing to migrate")
        return 0

    rows = _sqlite_rows(Path(db_path))
    print(f"rows in sqlite  : {len(rows)}")
    if not rows:
        return 0

    if args.dry_run:
        print("--dry-run: writing nothing")
        return 0

    store = open_incarnation_store()
    written = 0
    try:
        for row in rows:
            store.put(dict(row), expected_revision=ANY_REVISION)
            written += 1
    finally:
        store.close()
    print(f"written         : {written}")

    # VERIFY through the production read path, not the write handle: a store
    # that accepted every write and can be read by nobody is the exact defect
    # scitex-dev 0.49.0 shipped.
    missing = [r["incarnation_id"] for r in rows if get_incarnation(r["incarnation_id"]) is None]
    mismatched = [
        r["incarnation_id"]
        for r in rows
        if (got := get_incarnation(r["incarnation_id"])) is not None
        and got.get("born_at") != r["born_at"]
    ]
    print(f"verified present: {len(rows) - len(missing)} / {len(rows)}")
    if missing:
        print(f"MISSING after write: {missing[:10]}")
    if mismatched:
        print(f"BORN_AT CHANGED (must be empty): {mismatched[:10]}")
    return 1 if (missing or mismatched) else 0


if __name__ == "__main__":
    sys.exit(main())
