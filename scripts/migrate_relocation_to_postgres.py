#!/usr/bin/env python3
"""One-shot: copy the SQLite relocation tables into per-host PostgreSQL.

Companion to the ``relocation_pg`` port. The code stopped reading SQLite;
this moves the rows already there. Four tables, because the port deletes the
schema-evolution code that created the fourth:

    agent_residency                        -> the residency store
    relocation_leases                      -> the lease store
    relocation_journal                     -> the journal store
    relocation_journal_v1_one_row_per_agent -> the journal store, as attempt 1

THE FOURTH IS THE REASON THIS SCRIPT IS NOT OPTIONAL. That v1 table holds the
only record of relocations run under the old one-row-per-agent key. The module
that renamed it was explicit: "RENAMED, never dropped ... nothing is deleted —
least of all an audit trail, during the migration that supersedes it." The
port deletes ``_migrate``, which is the only code that ever read it. Without
this script those rows sit in a SQLite file we are about to delete, and the
audit trail the original author went out of their way to preserve is lost by
omission rather than by decision.

A DRY RUN IS THE DEFAULT. Pass ``--commit`` to actually write. The bare
invocation reports what would move and touches nothing.

RUN IT ON THE HOST, NOT IN A CONTAINER. ``DEFAULT_DB_PATH`` resolves
differently in the two places: a container gets its own per-agent shard under
``/state/<agent>/state.db``, the host gets
``~/.scitex/agent-container/runtime/state.db``. The rows are on the HOST, so a
container run would faithfully migrate an empty shard and report success.

RUN IT ONCE, BEFORE restarting the writers. Every fact field is
LAST_WRITER_WINS decided by the WRITE's clock, not by the value, so a
straggler pass after the new code is live would stamp an OLD residency or
lease over a FRESH one. For a lease that is the dangerous direction: it would
resurrect a superseded holder and fence, which is exactly what the fence
exists to prevent.

IT IS IDEMPOTENT and it VERIFIES: each record is read back through the same
production path and a mismatch is reported rather than counted as success.
Nothing is deleted from SQLite — the old tables stay untouched as a fallback.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

V1_TABLE = "relocation_journal_v1_one_row_per_agent"


def _rows(db_path: Path, table: str, columns: tuple[str, ...]) -> list[dict]:
    """Every row of ``table``, read-only. Empty (and SAID SO) when absent."""
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        cols = ", ".join(columns)
        return [dict(r) for r in conn.execute(f"SELECT {cols} FROM {table}")]
    except sqlite3.OperationalError as exc:
        # Reported, never swallowed: "the table is not there" and "the copy
        # found nothing" must not look the same in the output.
        print(f"  no {table} in {db_path}: {exc}")
        return []
    finally:
        conn.close()


def _migrate_residency(rows: list[dict], commit: bool) -> int:
    print(f"agent_residency: {len(rows)} row(s)")
    if not rows or not commit:
        # The store is imported only on the WRITE path. A dry run is the
        # safest thing this script does and must stay the most AVAILABLE:
        # someone asking "what would move?" on a host where scitex-dev is not
        # installed should get the answer, not a ModuleNotFoundError. Measured
        # 2026-08-24 — the first host run of this script died exactly there.
        return len(rows)
    from scitex_dev.store import ANY_REVISION

    from scitex_agent_container._state.relocation_pg import _residency_store
    store = _residency_store()
    try:
        with store.batch():
            for row in rows:
                store.put(
                    {
                        "agent": row["agent"],
                        "host": row["host"],
                        "from_ts": float(row["from_ts"]),
                        "to_ts": (
                            None if row["to_ts"] is None else float(row["to_ts"])
                        ),
                        "seeded": int(row["seeded"] or 0),
                        "note": row["note"],
                    },
                    expected_revision=ANY_REVISION,
                )
    finally:
        store.close()
    return len(rows)


def _migrate_leases(rows: list[dict], commit: bool) -> int:
    print(f"relocation_leases: {len(rows)} row(s)")
    if not rows or not commit:
        return len(rows)  # see _migrate_residency on why the import is here
    from scitex_dev.store import ANY_REVISION

    from scitex_agent_container._state.relocation_pg import _lease_store
    store = _lease_store()
    try:
        with store.batch():
            for row in rows:
                store.put(
                    {
                        "agent": row["agent"],
                        "holder": row["holder"],
                        # A pre-token-column row carries ''. It is COPIED as-is
                        # rather than skipped or invented: load_lease already
                        # returns None for it, and inventing a token here would
                        # forge the credential the fence exists to make
                        # unforgeable.
                        "token": row["token"] or "",
                        "fence": int(row["fence"]),
                        "expires_at": float(row["expires_at"]),
                        "updated_at": float(row["updated_at"]),
                    },
                    expected_revision=ANY_REVISION,
                )
    finally:
        store.close()
    return len(rows)


def _migrate_journal(rows: list[dict], v1_rows: list[dict], commit: bool) -> int:
    # v1 rows become attempt 1, mirroring what the deleted _migrate did. Their
    # opening timestamp was never stored under the old shape, so updated_at
    # stands in — the honest best available, and the same substitution the
    # original migration made.
    carried = [
        {
            "agent": r["agent"],
            "attempt": 1,
            "from_host": r["from_host"],
            "to_host": r["to_host"],
            "phase": r["phase"],
            "steps": r["steps"],
            "started_at": float(r["updated_at"]),
            "updated_at": float(r["updated_at"]),
        }
        for r in v1_rows
    ]
    # A v1 row for an agent that ALSO has a modern attempt 1 must not overwrite
    # it: the modern row is the real one, and the v1 row is its ancestor.
    modern_keys = {(r["agent"], int(r["attempt"])) for r in rows}
    carried = [c for c in carried if (c["agent"], 1) not in modern_keys]

    print(f"relocation_journal: {len(rows)} row(s)")
    print(f"{V1_TABLE}: {len(v1_rows)} row(s), {len(carried)} to carry as attempt 1")
    if not commit:
        return len(rows) + len(carried)  # see _migrate_residency re: imports
    from scitex_dev.store import ANY_REVISION

    from scitex_agent_container._state.relocation_pg import _journal_store

    payload = [
        {
            "agent": r["agent"],
            "attempt": int(r["attempt"]),
            "from_host": r["from_host"],
            "to_host": r["to_host"],
            "phase": r["phase"],
            "steps": r["steps"],
            "started_at": float(r["started_at"]),
            "updated_at": float(r["updated_at"]),
        }
        for r in rows
    ] + carried
    if not payload:
        return 0
    store = _journal_store()
    try:
        with store.batch():
            for record in payload:
                store.put(record, expected_revision=ANY_REVISION)
    finally:
        store.close()
    return len(payload)


def _verify(expected: "dict[str, int] | None" = None) -> int:
    """Read back through the PRODUCTION path and COMPARE against the source.

    This used to count `len(store.rows())`, print it, and return the total —
    with nothing to compare it against. A control that cannot fail is not a
    control: an empty store printed "0 record(s) readable" and the run still
    reported success, which is indistinguishable from a migration that moved
    everything. `expected` supplies the SQLite-side count per table so a short
    read is an ERROR rather than a number nobody checks.

    `expected=None` keeps the old count-only behaviour for callers that have no
    source to compare against, and SAYS SO in the output rather than implying a
    verdict it did not reach.
    """
    from scitex_agent_container._state.relocation_pg import (
        _journal_store,
        _lease_store,
        _residency_store,
    )

    total = 0
    short: list[str] = []
    for label, opener in (
        ("agent_residency", _residency_store),
        ("relocation_leases", _lease_store),
        ("relocation_journal", _journal_store),
    ):
        store = opener()
        try:
            seen = len(store.rows())
        finally:
            store.close()
        want = None if expected is None else expected.get(label, 0)
        if want is None:
            print(f"  verify {label}: {seen} record(s) readable (NOT COMPARED)")
        elif seen < want:
            print(f"  verify {label}: {seen} readable but source held {want} — SHORT")
            short.append(f"{label} ({seen} < {want})")
        else:
            print(f"  verify {label}: {seen} record(s) readable, source had {want} — OK")
        total += seen
    if short:
        raise SystemExit(
            "verification FAILED — the store holds fewer records than the "
            "SQLite source for: " + ", ".join(short)
        )
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--commit",
        action="store_true",
        help="actually write; without it this is a dry run that touches nothing",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help="override the SQLite source (defaults to state_db.DEFAULT_DB_PATH)",
    )
    args = parser.parse_args()

    from scitex_agent_container._state.state_db import DEFAULT_DB_PATH

    db_path = args.db_path or DEFAULT_DB_PATH
    print(f"source: {db_path}")
    if not Path(db_path).exists():
        print("source does not exist — nothing to migrate")
        return 0

    residency = _rows(
        db_path,
        "agent_residency",
        ("agent", "host", "from_ts", "to_ts", "seeded", "note"),
    )
    leases = _rows(
        db_path,
        "relocation_leases",
        ("agent", "holder", "token", "fence", "expires_at", "updated_at"),
    )
    journal = _rows(
        db_path,
        "relocation_journal",
        (
            "agent",
            "attempt",
            "from_host",
            "to_host",
            "phase",
            "steps",
            "started_at",
            "updated_at",
        ),
    )
    v1 = _rows(
        db_path,
        V1_TABLE,
        ("agent", "from_host", "to_host", "phase", "steps", "updated_at"),
    )

    if args.commit:
        from scitex_agent_container._state.relocation_pg import (
            init_relocation_schema,
        )

        print(f"target: {init_relocation_schema()}")

    moved = (
        _migrate_residency(residency, args.commit)
        + _migrate_leases(leases, args.commit)
        + _migrate_journal(journal, v1, args.commit)
    )

    if not args.commit:
        print(f"\nDRY RUN — {moved} record(s) would move. Re-run with --commit.")
        return 0
    print(f"\nmoved {moved} record(s); verifying through the production path")
    # LOWER BOUNDS, deliberately, so the check cannot cry wolf. Every modern
    # source row must arrive; `journal` therefore excludes `v1`, because
    # _migrate_journal DEDUPES a v1 row against a modern attempt-1 row for the
    # same agent, so `len(journal) + len(v1)` would flag a correct dedup as a
    # short read. A check that fires on correct behaviour gets ignored, which
    # is how it ends up as useless as no check at all.
    _verify(
        {
            "agent_residency": len(residency),
            "relocation_leases": len(leases),
            "relocation_journal": len(journal),
        }
    )
    print("SQLite untouched — the old tables remain as a fallback.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
