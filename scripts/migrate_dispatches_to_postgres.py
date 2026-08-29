#!/usr/bin/env python3
"""One-shot: copy the SQLite ``dispatches`` ledger into PostgreSQL.

Companion to the ``dispatch_ledger`` port. That change stops the code
READING SQLite; without this the rows already there stay in a file nothing
opens any more. Measured on scitex-compute-04 while writing this: 593 rows in
the host ``state.db``. They are the ledger's whole point — it exists so a
dispatch is not sent twice and so there is a record of what was sent — and a
cutover that silently starts from empty gives up both.

``agent`` IS CARRIED AS THE EMPTY STRING, ON PURPOSE
====================================================
The store identifies a row by ``(agent, dispatch_id)``. SQLite had no
``agent`` column: the FILE was the scope, so which agent a row belonged to
was never recorded. There is no way to recover it FROM THE MAIN DATABASE,
and the plausible guess -- ``from_agent`` -- is wrong often enough to
matter, because ``from_agent`` is who SENT the dispatch, not whose ledger it
was written into. Inventing a scope would fabricate exactly the fact the
migration cannot know.

The empty string is not a workaround, it is the value the code already uses:
``record_dispatch`` writes ``"agent": agent or ""``, and ``find_dispatch``
falls back to scanning every row when it has no agent to key on. So carried
rows are found by dispatch_id, which is what the old SQLite lookup did.

...BUT A SHARD KNOWS ITS AGENT, AND ``--agent`` IS HOW YOU SAY SO
=================================================================
The paragraph above was written about ``~/.scitex/agent-container/runtime/
state.db`` and is still true of it. It is NOT true of the per-agent overlay
shards, which sac keeps one directory down as
``~/.scitex/agent-container/runtime/<agent>/state.db``. Those were never
swept by any of these migrations, and for a shard the fact the main
database lost is right there in the path: the FILE was the scope, and the
file is named after the agent.

So ``--agent`` supplies it. The default is ``""`` -- byte-identical to the
behaviour above -- so every invocation written before this flag existed
means exactly what it meant then::

    # the main database, unchanged
    python3 scripts/migrate_dispatches_to_postgres.py --db-path ~/.scitex/agent-container/runtime/state.db

    # the shards, each carrying its own scope
    for db in ~/.scitex/agent-container/runtime/*/state.db; do
        python3 scripts/migrate_dispatches_to_postgres.py \
            --db-path "$db" --agent "$(basename "$(dirname "$db")")"
    done

THE SCOPE IS PRINTED ON EVERY RUN, dry or not, because ``agent`` is an
IMMUTABLE IDENTITY field: a row written under the wrong scope is a
different record forever, and cannot be re-scoped in place. Reading one
line of the dry run is the cheap way to catch a forgotten flag; there is no
cheap way to catch it afterwards.

There is deliberately no path-sniffing that infers the agent from
``--db-path``. A guess that is usually right is the worst kind here: the
one time the layout differs it would stamp a plausible wrong scope, and the
operator would have no reason to look.

RUN IT ON THE HOST, NOT IN A CONTAINER
======================================
``DEFAULT_DB_PATH`` resolves differently in the two places: a container gets
its own per-agent shard under ``/state/<agent>/state.db`` (and ``$HOME`` is
``/home/agent``, which is ephemeral), the host gets
``~/.scitex/agent-container/runtime/state.db``. The rows are on the HOST, so
a container run would faithfully migrate an empty shard and report success --
the same shape that made the diary migration look finished for three days.

A DRY RUN IS THE DEFAULT. Pass ``--commit`` to actually write.

IT IS IDEMPOTENT and it VERIFIES. Rows are written with ``NEW_RECORD``, so a
key already present is left ALONE rather than overwritten: re-running after a
partial move is safe, and a re-run can never re-stamp a ``status`` that has
moved on since. Nothing is deleted from SQLite -- the old table stays as a
fallback.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path
from typing import Any, Mapping

TABLE = "dispatches"
COLUMNS = (
    "dispatch_id",
    "from_agent",
    "to_agent",
    "conversation_id",
    "text_summary",
    "status",
    "ts",
)


def default_db_path() -> Path:
    """The HOST state.db. See the module docstring on why this matters."""
    return Path.home() / ".scitex" / "agent-container" / "runtime" / "state.db"


def read_rows(db_path: Path) -> list[dict]:
    """Every dispatch, read-only.

    A missing table is reported distinctly from an empty one: "no such table"
    and "no rows" are different facts, and collapsing them is how a migration
    silently skips a host.
    """
    if not db_path.is_file():
        print(f"  {db_path}: no such file")
        return []
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        cur = conn.execute(f"SELECT {', '.join(COLUMNS)} FROM {TABLE}")
        return [dict(r) for r in cur.fetchall()]
    except sqlite3.OperationalError as exc:
        print(f"  no {TABLE} in {db_path}: {exc}")
        return []
    finally:
        conn.close()


def _record(row: Mapping[str, Any], agent: str) -> dict:
    """One SQLite row as the store's record, scoped to ``agent``.

    Extracted from :func:`migrate` when ``--agent`` arrived, so the one
    decision this script makes that a caller can get wrong -- WHICH SCOPE
    the rows are stamped with -- is a pure function that can be asserted on
    without a PostgreSQL server.

    ``agent`` is passed in rather than defaulted here: the empty string is a
    real choice about the main database (see the module docstring), not an
    absence, and a default would let a caller forget to make it.
    """
    return {
        "agent": agent,
        "dispatch_id": row["dispatch_id"],
        "from_agent": row["from_agent"] or "",
        "to_agent": row["to_agent"] or "",
        "conversation_id": row["conversation_id"] or "",
        "text_summary": row["text_summary"] or "",
        "status": row["status"] or "",
        "ts": float(row["ts"]),
    }


def migrate(rows: list[dict], commit: bool, agent: str = "") -> int:
    """Write through the production store. Returns rows written, or -1.

    ``agent`` defaults to ``""`` -- the scope this script has always
    written, and the only honest one for the main database. A shard sweep
    passes the directory name instead; see the module docstring.
    """
    print(f"{TABLE}: {len(rows)} row(s)")
    if not rows or not commit:
        # scitex-dev is imported only on the WRITE path, so "what would move?"
        # still answers on a host where it is not installed. The first host run
        # of the relocation script died exactly there.
        return 0

    from scitex_dev.store import NEW_RECORD, RevisionMismatchError

    from scitex_agent_container._state.dispatch_ledger_store import (
        IDENTITY_FIELDS,
        open_dispatch_store,
    )

    store = open_dispatch_store()
    written = 0
    already = 0
    try:
        for row in rows:
            values = _record(row, agent)
            key = {k: values[k] for k in IDENTITY_FIELDS}
            if store.get(key, include_hidden=True) is not None:
                already += 1
                continue
            try:
                # NEW_RECORD, never ANY_REVISION: `status` is the one field
                # that moves, and re-running with ANY_REVISION would push a
                # delivered dispatch back to whatever SQLite last saw.
                store.put(values, expected_revision=NEW_RECORD)
                written += 1
            except RevisionMismatchError:
                # Another writer won between the get and the put. The row
                # exists, which is the goal -- not an error.
                already += 1
    finally:
        store.close()
    if already:
        print(f"  {already} row(s) already present, left untouched")

    # VERIFY through the production read path rather than trusting the write
    # count: a put that silently no-opped would otherwise report as success.
    store = open_dispatch_store()
    try:
        present = len(list(store.rows()))
    finally:
        store.close()
    if present < len(rows):
        print(
            f"  MISMATCH — wrote {written}, store holds {present} "
            f"(expected at least {len(rows)}). NOT a success."
        )
        return -1
    print(f"  {written} row(s) moved, {present} verified present")
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
    parser.add_argument(
        "--agent",
        default="",
        help=(
            "scope to stamp on every carried row (default: '', the "
            "unscoped value the main state.db has always been migrated "
            "with). Pass the agent name when reading a per-agent shard at "
            "runtime/<agent>/state.db, where the file name IS the scope the "
            "table never recorded. It is an IMMUTABLE identity field: a row "
            "written under the wrong scope cannot be re-scoped."
        ),
    )
    args = parser.parse_args(argv)

    db_path = args.db_path or default_db_path()
    print(f"source: {db_path}{'' if db_path.is_file() else '  (ABSENT)'}")
    print(f"mode:   {'COMMIT' if args.commit else 'DRY RUN'}")
    # Printed on EVERY run, dry included, and before anything is read.
    # ``agent`` is an immutable identity field, so this is the last cheap
    # moment to notice a forgotten (or a mistaken) --agent.
    if args.agent:
        print(
            f"scope:  agent={args.agent!r} — every carried row is stamped "
            f"with it (immutable; cannot be re-scoped afterwards)"
        )
    else:
        print(
            "scope:  agent='' — UNSCOPED, the main state.db's only honest "
            "value. For a runtime/<agent>/state.db shard, pass --agent."
        )

    rows = read_rows(db_path)
    if migrate(rows, args.commit, agent=args.agent) < 0:
        print("FAILED — the table did not verify. SQLite left untouched.")
        return 1
    if not args.commit:
        print(f"\nDRY RUN — {len(rows)} row(s) would move. Re-run with --commit.")
        return 0
    print("SQLite untouched — the old table remains as a fallback.")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
