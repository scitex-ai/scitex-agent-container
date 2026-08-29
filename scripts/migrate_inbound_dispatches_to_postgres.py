#!/usr/bin/env python3
"""One-shot: copy the SQLite ``inbound_dispatches`` ledger into PostgreSQL.

THE MIGRATION THAT WAS NEVER WRITTEN
====================================
:mod:`_state.inbound_ledger` was cut over to PostgreSQL by #1169 on
2026-08-20. Every other table that moved got a
``scripts/migrate_*_to_postgres.py`` companion — thirteen of them by
2026-08-29 — and this one did not. The code stopped reading SQLite and
nothing carried the rows across, so they have been sitting in a file
nothing opens since the cutover. Measured fleet-wide 2026-08-29: 5,200+
rows, of which 133 are still ``pending`` or ``reporting``.

Those 133 are the reason this is not merely a history-preservation
exercise. A ``pending`` row is an inbound dispatch whose requester is still
owed a completion report; :func:`claim_oldest_pending` is the only thing
that can discharge it, and it reads PostgreSQL now. Until the row is here,
that debt is invisible to the code that exists to pay it.

IT SWEEPS ONE FILE. THE SHARDS ARE FILES TOO.
=============================================
The sibling migrations were all run against the MAIN host database,
``~/.scitex/agent-container/runtime/state.db``. sac also gives each agent
its own overlay shard, and a shard is a whole separate SQLite file with the
same tables in it. Those were never swept: measured on scitex-compute-04
2026-08-29, 1,908 dispatch rows sit in shards and are absent from
PostgreSQL. (That count was taken across the dispatch ledgers rather than
per table, so read it as the size of the shard blind spot, not as this
table's share of it — which is a number this script's own dry run is the
right instrument for.)

There is no ``--all-shards`` flag here on purpose. ``--db-path`` already
names one file, the run is idempotent, and a shell loop is a thing an
operator can read, interrupt and re-run::

    for db in ~/.scitex/agent-container/runtime/*/state.db; do
        python3 scripts/migrate_inbound_dispatches_to_postgres.py --db-path "$db"
    done

(dry run first — drop nothing, add ``--commit`` only once the listing above
looks right). A flag that walked the tree itself would decide the glob for
the operator and hide which file each row came from, which is the one fact
they need when a row fails.

NO ``--agent`` FLAG, AND THAT IS THE DIFFERENCE FROM THE SIBLING
================================================================
``migrate_dispatches_to_postgres.py`` — the OUTBOUND ledger — has to stamp
``agent=""`` on every row and says so at length: its SQLite table had no
``agent`` column, the FILE was the scope, and the fact is unrecoverable
from the main database. It grew an ``--agent`` option so a shard sweep can
supply the scope the file name knows and the row does not.

This table is the other case. ``inbound_dispatches.agent`` is ``TEXT NOT
NULL`` and always was; :func:`record_inbound` refuses an empty one. So the
scope is IN THE ROW, on the main database and in every shard alike, and it
is carried verbatim. An ``--agent`` flag here could only mean "overwrite
the recorded owner", which would fabricate exactly the fact this table did
not lose.

A STATUS DISAGREEMENT IS NOT A COLLISION
========================================
Several of these migrations pass ``collides_with`` and refuse when the
store already holds a record with different values. This one does NOT, and
the difference is real rather than an omission.

``status`` is the field whose entire purpose is to move: pending ->
reporting -> reported/failed, written by a live Stop hook. A row that reads
``pending`` in SQLite and ``reported`` in the store is not two hosts
disagreeing about a fact — it is one row, correctly ahead of the file. So
the already-present record is LEFT ALONE (``NEW_RECORD``, never
``ANY_REVISION``, which the library owns), exactly as the outbound sibling
argues: "re-running with ANY_REVISION would push a delivered dispatch back
to whatever SQLite last saw."

``reporting`` IS CARRIED AS ``reporting``, NOT REWOUND TO ``pending``
====================================================================
Tempting, because a ``reporting`` row is stuck: :func:`claim_oldest_pending`
only ever claims ``pending``, so a row abandoned mid-report will never be
claimed again and its requester never hears back. Rewinding it would look
like a repair.

It is not one. ``reporting`` means a Stop hook CLAIMED the row and then
died — before pushing the completion, or after pushing it and before
settling. The two are indistinguishable from here, and rewinding gambles a
DOUBLE report against a missing one. A migration is the wrong place to take
that bet: it is a copy, and the operator who decides to re-arm those rows
should do it deliberately, against a store where they can see them. So they
move verbatim, and the dry run COUNTS them so the decision is at least
visible.

``reported_ts`` IS OMITTED WHEN THE ROW NEVER SETTLED
=====================================================
``NULL`` in SQLite, absent in the record. The schema makes it
``required=False`` for the reason its own docstring gives: "a row that has
not settled has no settle time, and inventing one would make 'never
reported' indistinguishable from 'reported at epoch'."

DUPLICATE IDENTITIES COLLAPSE, AND ARE COUNTED
==============================================
The store's identity is ``(agent, from_agent, dispatch_id, ts)``. SQLite's
was an ``AUTOINCREMENT`` id, so two wakes for the same agent from the same
peer, with no dispatch id, in the same float instant were FREE there and
are ONE record here — which is why :func:`record_inbound` now advances
``ts`` by a microsecond on collision. Rows already in the file predate that
retry, so the collapse is reported rather than discovered as an
unexplained shortfall in the verify line.

VERIFICATION COUNTS THIS RUN'S ROWS, NOT THE STORE'S
====================================================
``_migrate_lib`` compares ``verify()`` against the number of rows read, so
a verifier returning "how many records does the store hold" is vacuously
true the moment anything else has migrated — and this script is designed to
be run once per shard into ONE host store, so from the second file onward
a global count would be green no matter what happened. The bug was found
during the ``comms_nodes`` rollout and re-stated by ``lineage`` and
``instances``; it applies here with more force, not less.

RUN IT ON THE HOST, NOT IN A CONTAINER
======================================
``_migrate_lib.default_db_path`` reads ``SCITEX_AGENT_CONTAINER_STATE_DB``
first, which inside a container names THAT CONTAINER'S OWN SHARD. A
container run would therefore migrate one small file, faithfully, and
report success — the shape that made the diary migration look finished for
three days.

A DRY RUN IS THE DEFAULT. ``--commit`` is the whole opt-in. Nothing is
deleted from SQLite; the old table stays as a fallback.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Iterable, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
# ...and this script's OWN directory, which a direct ``python scripts/x.py``
# supplies for free and an ``importlib.util.spec_from_file_location`` load
# does not. The develop-tier test that pins "a bare invocation writes
# nothing" loads these scripts the second way.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _migrate_lib import SqliteSource, run_migration  # noqa: E402

from scitex_agent_container._state.inbound_ledger import (  # noqa: E402
    IDENTITY_FIELDS,
    STATUS_PENDING,
    STATUS_REPORTING,
    STORE_NAME,
    VALID_STATUSES,
    open_inbound_store,
)

#: The legacy SQLite table. Its DDL, before #1169 deleted it::
#:
#:     id           INTEGER PRIMARY KEY AUTOINCREMENT
#:     agent        TEXT NOT NULL
#:     from_agent   TEXT NOT NULL
#:     dispatch_id  TEXT
#:     status       TEXT NOT NULL DEFAULT 'pending'
#:     ts           REAL NOT NULL
#:     reported_ts  REAL
TABLE = "inbound_dispatches"

#: The two statuses that mean the dispatch is still owed a report. Named
#: rather than spelled inline: the dry-run listing, the summary and the
#: reader all have to agree on what "unfinished" means.
UNFINISHED = (STATUS_PENDING, STATUS_REPORTING)

SOURCE = SqliteSource(
    table=TABLE,
    # ``id`` is SELECTed but never carried: the PostgreSQL row is these
    # columns minus the surrogate. It is here so a failure line can name
    # WHICH SQLite row failed, which is the one thing an operator needs to
    # go back to the file with.
    columns=("id", "agent", "from_agent", "dispatch_id", "status", "ts", "reported_ts"),
    # ``ts`` rather than rowid: FIFO by ``ts`` is the order
    # ``claim_oldest_pending`` reads this ledger in, so it is the order the
    # dry-run listing should be read in too. ``rowid`` rather than ``id``
    # for the tiebreak — identical here, and it survives a host whose table
    # is narrower than this column list.
    order_by="ts ASC, rowid ASC",
)


def _as_float(value: Any) -> "float | None":
    """``float(value)``, or ``None`` when it is not a number.

    NEVER RAISES, and that is a requirement rather than defensiveness:
    ``_migrate_lib.migrate_rows`` calls ``to_record`` OUTSIDE its per-row
    ``try``, so an exception raised in the mapping does not fail one row —
    it aborts the pass and strands every row after it. The library's own
    docstring makes that promise ("one row's failure never aborts the
    pass"), and keeping it is the consumer's job.

    ``bool`` is excluded because ``float(True)`` is ``1.0``, which would
    turn a nonsense value into a plausible timestamp.
    """
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_text(value: Any) -> "str | None":
    """``str(value)``, or ``None`` when the column was NULL/absent.

    ``None`` is propagated rather than stringified: ``str(None)`` is
    ``"None"``, which the store would accept as a perfectly good agent name.
    A dropped key fails loudly at the ``put`` instead, naming the row.
    """
    return None if value is None else str(value)


def _record(row: Mapping[str, Any]) -> dict[str, Any]:
    """One SQLite row as the store's record.

    ``None`` values are DROPPED. For the required fields (``agent``,
    ``from_agent``, ``ts``, ``status``) that turns a NULL or an absent
    column into a loud per-row ``put`` failure that names the row, rather
    than a silently plausible value. For ``reported_ts`` it is the correct
    representation of "never settled" — see the module docstring.

    ``dispatch_id`` is the exception: NULL becomes ``""``, because the
    store's identity fields must be present and ``inbound_ledger`` already
    rules that ``""`` is how "this wake carried no dispatch id" is spelled.
    """
    values: dict[str, Any] = {
        "agent": _as_text(row.get("agent")),
        "from_agent": _as_text(row.get("from_agent")),
        "dispatch_id": _as_text(row.get("dispatch_id")) or "",
        "ts": _as_float(row.get("ts")),
        "status": _as_text(row.get("status")),
        "reported_ts": _as_float(row.get("reported_ts")),
    }
    return {key: value for key, value in values.items() if value is not None}


def _key(record: Mapping[str, Any]) -> dict[str, Any]:
    """The identity mapping. ``.get`` so a defective record cannot raise here.

    Same reason as :func:`_as_float`: ``key_of`` is called outside the
    library's per-row ``try``.
    """
    return {field: record.get(field) for field in IDENTITY_FIELDS}


def _identity(record: Mapping[str, Any]) -> tuple:
    """The identity as a HASHABLE tuple, for set membership in the verify."""
    return tuple(record.get(field) for field in IDENTITY_FIELDS)


def _describe(row: Mapping[str, Any]) -> str:
    """One dry-run line. Leads with whether the dispatch still owes a report.

    An ``UNKNOWN!`` marker is not decoration: ``status`` is free text in the
    store, so a value outside :data:`VALID_STATUSES` would migrate happily
    and then match no reader's filter. Better seen in the dry run.
    """
    status = str(row.get("status"))
    if status in UNFINISHED:
        mark = "UNFINISHED"
    elif status in VALID_STATUSES:
        mark = "settled   "
    else:
        mark = "UNKNOWN!  "
    return (
        f"{mark} {status:<9} ts={row.get('ts')}  {row.get('agent')} "
        f"<- {row.get('from_agent')}  "
        f"dispatch_id={row.get('dispatch_id') or '-'}  sqlite_id={row.get('id')}"
    )


def _summarise(rows: Iterable[Mapping[str, Any]], log) -> None:
    """Report the facts about the rows AS A SET, which no per-row line shows.

    Emitted from the source read because that is where ``_migrate_lib``
    already reports source-shaped facts ("no such file", "columns absent",
    "0 rows"). Each line carries its own count so it reads correctly
    wherever the library places its own row-count line relative to these.
    """
    rows = list(rows)
    if not rows:
        return
    counts: dict[str, int] = {}
    for row in rows:
        status = str(row.get("status"))
        counts[status] = counts.get(status, 0) + 1
    breakdown = "  ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    log(f"  status breakdown of the {len(rows)} row(s) read: {breakdown}")

    unfinished = sum(count for status, count in counts.items() if status in UNFINISHED)
    if unfinished:
        log(
            f"  UNFINISHED: {unfinished} of them are {'/'.join(UNFINISHED)} — "
            f"each is an inbound dispatch whose requester is still owed a "
            f"completion report, and only PostgreSQL is read for those now. "
            f"They move VERBATIM: a 'reporting' row is NOT rewound to "
            f"'pending' (see the module docstring — that would gamble a "
            f"double report against a missing one)."
        )

    identities = [_identity(_record(row)) for row in rows]
    collapsed = len(identities) - len(set(identities))
    if collapsed:
        log(
            f"  {collapsed} row(s) share an identity with another row in this "
            f"file: SQLite kept them apart with its AUTOINCREMENT id, and the "
            f"store's identity is {IDENTITY_FIELDS}. They collapse into one "
            f"record each. Counted here so the verify line is not read as a "
            f"shortfall."
        )


def main(argv: "list[str] | None" = None) -> int:
    """Read one SQLite file, (maybe) write, verify THIS RUN's rows.

    No ``collides_with``: a status disagreement is the store being correctly
    ahead of the file, not two writers disagreeing (module docstring).

    No ``should_hide``: this table has no tombstone column. A ``reported``
    row is settled history, not a withdrawal, and hiding it would make it
    read as absent to ``list_inbound``.

    No ``actor``: the library passes it only to ``Store.hide``, which is
    never reached here, and ``open_inbound_store`` already stamps the
    store's declared single writer on every put it makes.
    """
    # Captured by the source below and read by the verify. A local rather
    # than a module global so a second call to ``main`` in one process — a
    # test, a wrapper, an operator's REPL — cannot verify against the
    # previous run's rows.
    captured: list[Mapping[str, Any]] = []

    class _CapturingSource(SqliteSource):
        """The library's reader, plus "remember what was read" and a summary.

        ``run_migration`` owns the read and hands ``verify`` nothing, so
        this is the seam through which the verifier learns which rows the
        run is responsible for. Same shape as the ``lineage`` migration.
        """

        def read(self, db_path: Path, *, log: Any = print) -> list[dict]:
            rows = super().read(db_path, log=log)
            captured.clear()
            captured.extend(rows)
            _summarise(rows, log)
            return rows

    source = _CapturingSource(
        table=SOURCE.table, columns=SOURCE.columns, order_by=SOURCE.order_by
    )

    def _verify() -> int:
        """How many of THIS run's rows the production reader can see.

        ``store.rows()`` is the same scan ``claim_oldest_pending`` and
        ``list_inbound`` perform, so a record the store accepted but cannot
        serve counts as absent — which is the whole reason verification
        reads back rather than trusting the write count.

        Counted per SOURCE ROW, not per distinct identity: two rows that
        collapsed into one record are both genuinely represented, and
        counting identities would report that correct outcome as a
        shortfall. The collapse itself is reported by :func:`_summarise`.
        """
        store = open_inbound_store()
        try:
            present = {
                tuple(row.values.get(field) for field in IDENTITY_FIELDS)
                for row in store.rows()
            }
        finally:
            store.close()
        return sum(1 for row in captured if _identity(_record(row)) in present)

    return run_migration(
        argv=argv,
        description=__doc__,
        source=source,
        store_name=STORE_NAME,
        open_store=open_inbound_store,
        to_record=_record,
        key_of=_key,
        verify=_verify,
        describe_row=_describe,
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
