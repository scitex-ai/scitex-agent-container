#!/usr/bin/env python3
"""One-shot: copy the SQLite ``verdict_delivered`` rows into PostgreSQL.

WHY THIS EXISTS AND WHY IT MUST RUN BEFORE THE PORT IS DEPLOYED
===============================================================
``_state/state_db_verdict_dedup`` moved from SQLite to PostgreSQL (via
``scitex_dev.store``). The delivered-set is the ONLY thing standing between
the CI poller and re-delivering verdicts that agents have already seen. An
empty PostgreSQL store therefore does not mean "nothing delivered yet" — it
means "deliver everything again".

Measured 2026-08-19, rows at risk per host:

    scitex-compute-04   722        scitex-compute-02   114
    scitex-compute-03   358        scitex-compute-01   104
    fleet                             1,298

Most are for head SHAs that are no longer any open PR's head, so they would
never be re-polled. The dangerous remainder is the currently-open PRs whose
current verdict was already sent — those WOULD be re-delivered, and the
failure streak would reset, un-capping PRs the cap had deliberately
silenced. Neither failure is loud. Hence this script.

IT LIVES IN scripts/, NOT IN src/, ON PURPOSE
=============================================
It imports ``sqlite3``, and ``tests/develop/test_sqlite_footprint_frozen.py``
scans ``src/`` and fails on any new SQLite user there. That gate is right and
this file must not weaken it: a migration tool is a one-time act, not a
capability the package ships. Delete it once every host has run it.

IDEMPOTENT, AND NON-DESTRUCTIVE
===============================
Writes go through ``record_verdict_delivered``, which is INSERT-OR-IGNORE and
preserves ``delivered_at``, so re-running copies nothing new and never moves
a timestamp. The SQLite file is opened READ-ONLY and is not modified or
deleted — if the port has to be reverted, the old state is still there.

RUN IT ON THE HOST, NOT INSIDE AN AGENT CONTAINER
=================================================
``DEFAULT_DB_PATH`` resolves to a DIFFERENT FILE depending on where you are,
and both answers are correct for their own environment:

    on the host          ~/.scitex/agent-container/runtime/state.db
                         <- the real one; `sac listen` opens this
    inside a container    /state/scitex-agent-container/state.db
                         <- that agent's private shard, from
                            $SCITEX_AGENT_CONTAINER_STATE_DB

Measured 2026-08-19: the host db held 722 delivered-set rows and one
container shard held NONE — it does not even have the table. So a migration
run from inside a container reports "nothing to migrate", exits 0, and
leaves every real row behind. The script PRINTS the source path it resolved
for exactly this reason: check that line before trusting the result.

WHERE THIS SITS IN THE DEPLOYMENT, AND WHY THAT ORDER IS SAFE
=============================================================
This script IMPORTS the ported module, so it cannot run before the code is
on the host. An earlier draft of the PR said "migrate, then deploy"; that
order is not merely risky, it is impossible, and a dry run piped to three
peer hosts proved it with an identical
``ImportError: cannot import name 'verdict_store_target'`` on each.

    1. git -C ~/proj/scitex-agent-container pull      (new code on disk)
    2. python3 scripts/migrate_verdict_delivered_to_postgres.py --commit
    3. restart `sac listen`                            (new code in effect)

Step 1 does NOT change the running daemon — it holds the modules it imported
at boot and keeps reading SQLite through step 2. That is what makes the
window safe by construction rather than merely short: the migration finishes
while the only live reader is still the old one. Step 3 switches the reader
over, and by then the rows are there.

Each host's ``.env-sac`` resolves sac through an EDITABLE install pointing at
``~/proj/scitex-agent-container/src``, so step 1 really is a ``git pull`` and
not a ``pip install``.

RUN IT TWICE — THE TABLE IS LIVE AND A SNAPSHOT IS ALWAYS SLIGHTLY STALE
========================================================================
The delivered-set accrues while you work. Measured 2026-08-19, forty minutes
apart, with nothing of mine touching it:

    compute-04   722 -> 724      compute-02   114 -> 116
    compute-03   358 -> 359      compute-01   104 -> 106

So the old daemon keeps writing SQLite rows between step 2 and step 3, and
those rows would be absent from PostgreSQL when the new daemon takes over —
each one a verdict re-delivered once. At this rate that is zero or one row
per host, which is small but not nothing, and it is silent.

The script is IDEMPOTENT, so the fix is free: run it again AFTER the
restart. The second pass sees whatever the old daemon wrote in the gap and
copies it; from the restart onward nothing writes SQLite at all, so a third
pass would find nothing.

    1. git -C ~/proj/scitex-agent-container pull      new code on disk
    2. migrate ... --commit                            bulk
    3. restart `sac listen`                            new code in effect
    4. migrate ... --commit                            the stragglers

USAGE (per host, after step 1 above)

    python3 scripts/migrate_verdict_delivered_to_postgres.py            # dry run
    python3 scripts/migrate_verdict_delivered_to_postgres.py --commit   # do it

The columns are IDENTICAL on all four hosts — repo, pr, head_sha,
conclusion, dispatch_id, delivered_at — verified before relying on one
SELECT everywhere, because the table COUNT differs per host (26/22/21/21;
tables are created lazily by whichever module ran there) and a uniform
schema could not be assumed from that.

Exits non-zero on any failure. There is no partial-success-reported-as-
success path: a row that cannot be written raises.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path


def _sqlite_rows(db_path: Path) -> list[tuple]:
    """Every delivered-set row in the SQLite state db, read-only.

    A missing file or missing table is reported as such and yields nothing —
    those are legitimate states (a host that never polled CI), and they are
    DISTINGUISHED from an error rather than both landing on an empty list.
    """
    if not db_path.exists():
        print(f"  no SQLite state db at {db_path} — nothing to migrate")
        return []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        present = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='verdict_delivered'"
        ).fetchone()
        if not present:
            print("  SQLite db has no verdict_delivered table — nothing to migrate")
            return []
        return conn.execute(
            "SELECT repo, pr, head_sha, conclusion, dispatch_id, delivered_at "
            "FROM verdict_delivered"
        ).fetchall()
    finally:
        conn.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state-db",
        type=Path,
        default=None,
        help="SQLite state.db (default: the package's DEFAULT_DB_PATH)",
    )
    parser.add_argument(
        "--commit",
        action="store_true",
        help="actually write; without it this is a dry run that writes nothing",
    )
    args = parser.parse_args(argv)

    if args.state_db is None:
        from scitex_agent_container._state.state_db import DEFAULT_DB_PATH

        args.state_db = Path(DEFAULT_DB_PATH)

    from scitex_agent_container._state.state_db_verdict_dedup import (
        record_verdict_delivered,
        verdict_already_delivered,
        verdict_store_target,
    )

    print(f"SOURCE (SQLite) : {args.state_db}")
    print(f"TARGET (Postgres): {verdict_store_target().locator}")

    rows = _sqlite_rows(args.state_db)
    print(f"rows in SQLite  : {len(rows)}")
    if not rows:
        return 0

    missing = [
        r
        for r in rows
        if not verdict_already_delivered(
            repo=r[0], pr=int(r[1]), head_sha=r[2], conclusion=r[3]
        )
    ]
    print(f"absent in Postgres: {len(missing)}  (already there: {len(rows) - len(missing)})")

    if not args.commit:
        print("DRY RUN — nothing written. Re-run with --commit.")
        return 0

    for repo, pr, head_sha, conclusion, dispatch_id, delivered_at in missing:
        record_verdict_delivered(
            repo=repo,
            pr=int(pr),
            head_sha=head_sha,
            conclusion=conclusion,
            dispatch_id=dispatch_id,
            delivered_at=float(delivered_at),
        )

    # Verify by RE-READING rather than by counting what we sent. A write loop
    # that completed is not evidence the rows are there.
    still_missing = [
        r
        for r in rows
        if not verdict_already_delivered(
            repo=r[0], pr=int(r[1]), head_sha=r[2], conclusion=r[3]
        )
    ]
    print(f"after commit, still absent: {len(still_missing)}")
    if still_missing:
        print("MIGRATION INCOMPLETE — the rows above did not land.", file=sys.stderr)
        return 1
    print("OK — every SQLite row is present in PostgreSQL.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
