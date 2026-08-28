#!/usr/bin/env python3
"""The shape every ``migrate_*_to_postgres.py`` script was re-typing.

By 2026-08-28 this directory held ten one-shot migrations and roughly
nineteen hundred lines, and the DIFFERENCES between them were: a table
name, a column list, a row→record mapping, and which store to open. The
other ninety percent — the read-only SQLite open, the ``--db-path``
resolution, the dry-run-by-default flag, the get-then-put loop, the
read-back verification — was copied. Copied scaffolding is not merely
verbose: a lesson learned in the fifth script does not reach the first
four, and this scaffolding is where the lessons ARE. Every rule below was
paid for once already:

* ``read_rows`` reports "no such file", "no such table" and "no rows" as
  THREE facts. ``migrate_dispatches_to_postgres`` states why: collapsing
  them "is how a migration silently skips a host".
* ``NEW_RECORD``, never ``ANY_REVISION``, and a get-first check. The
  store's copy may have been written by a live daemon SINCE the first
  pass, and that copy is newer than the file's; overwriting it "would
  roll a fresh spec back to whatever the abandoned file remembers"
  (``migrate_node_comms_policy_to_postgres``). It is also what makes a
  re-run after a partial move safe.
* ``RevisionMismatchError`` is NOT an error here. Another writer won the
  race between the get and the put; the record exists, which is the goal.
* Verification reads back through the PRODUCTION read path. Trusting the
  write count means "a put that silently no-opped would otherwise report
  as success" (``migrate_dispatches_to_postgres``).
* ``scitex_dev.store`` is imported ONLY on the write path, so a dry run
  still answers "what would move?" on a host where scitex-dev is absent.
  The first host run of the relocation migration died exactly there.

WHY A DRY RUN IS THE DEFAULT
============================
These scripts are run by hand, on production hosts, usually once, often
under time pressure during a cutover. The default has to be the one whose
worst case is a wasted minute. ``--commit`` is the whole opt-in.

WHY ``--db-path`` KEEPS COMING UP
=================================
:func:`default_db_path` resolves differently inside a container and on the
bare host, and every one of these migrations reads rows that live on the
HOST. A container gets its own per-agent shard under ``/state/<agent>/
state.db`` (and ``$HOME`` is ``/home/agent``, which is ephemeral); the host
gets ``~/.scitex/agent-container/runtime/state.db``. A container run
therefore migrates an EMPTY shard, faithfully, and reports success — the
shape that made the diary migration look finished for three days. The
resolved path is printed before any work for exactly this reason: the
operator can see which file was opened without reading the code.

WHAT THIS LIBRARY DOES NOT DO
=============================
It never touches the SQLite side. Every consumer leaves the old table
exactly as found, so a re-run is safe and a rollback is "point the code
back at SQLite".

The nine existing scripts are NOT rewritten onto this library in the same
change that introduces it. They are one-shot scripts, several already run
on live hosts, and re-plumbing them would put churn in a migration PR
whose diff must stay readable. The library plus one consumer proves the
shape; a later pass can adopt it where it pays.
"""

from __future__ import annotations

import argparse
import os
import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

__all__ = [
    "MigrationReport",
    "SqliteSource",
    "add_common_arguments",
    "default_db_path",
    "migrate_rows",
    "run_migration",
]


def default_db_path() -> Path:
    """The state.db these migrations read — the HOST's, unless overridden.

    ``SCITEX_AGENT_CONTAINER_STATE_DB`` wins when set, because that is what
    a container sets to name its own shard and what a test harness sets to
    name a fixture. Otherwise the bare host's runtime file. See the module
    docstring for why the difference matters more than it looks.
    """
    env = os.environ.get("SCITEX_AGENT_CONTAINER_STATE_DB")
    if env:
        return Path(env)
    return Path.home() / ".scitex" / "agent-container" / "runtime" / "state.db"


@dataclass(frozen=True)
class SqliteSource:
    """A read-only reader for ONE legacy table.

    ``columns`` is the full column list the consumer wants. Columns are
    intersected with ``PRAGMA table_info`` rather than assumed present:
    several of these tables grew columns by ``ALTER TABLE``, so a host that
    never ran a later migration has a NARROWER table, and a literal
    ``SELECT`` of every column would raise instead of migrating what is
    there. The consumer's mapping fills an absent column with the default
    its DDL declared.
    """

    table: str
    columns: Sequence[str]
    #: Ordering for the read. ``rowid`` is insertion order, which is the
    #: right default: it makes a partial run resumable in a predictable
    #: place and makes the dry-run listing stable between invocations.
    order_by: str = "rowid ASC"

    def read(self, db_path: Path, *, log: Callable[[str], None] = print) -> list[dict]:
        """Every row, read-only. Missing file / missing table → ``[]`` + a line.

        The three empty cases are REPORTED DIFFERENTLY on purpose (see the
        module docstring); they are all returned as an empty list, but the
        operator can tell from the output which one happened.
        """
        if not db_path.is_file():
            log(f"  {db_path}: no such file")
            return []
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        try:
            present = [
                str(r["name"])
                for r in conn.execute(f"PRAGMA table_info({self.table})")  # noqa: S608
            ]
            if not present:
                log(f"  no {self.table} table in {db_path}")
                return []
            selected = [c for c in self.columns if c in present]
            missing = [c for c in self.columns if c not in present]
            if missing:
                log(
                    f"  {self.table}: columns {missing} absent on this host — "
                    f"the mapping's defaults will be used"
                )
            cur = conn.execute(
                f"SELECT {', '.join(selected)} FROM {self.table} "  # noqa: S608
                f"ORDER BY {self.order_by}"
            )
            rows = [{c: r[c] for c in selected} for r in cur.fetchall()]
            if not rows:
                log(f"  {self.table}: table exists, 0 rows")
            return rows
        finally:
            conn.close()


@dataclass
class MigrationReport:
    """What one :func:`migrate_rows` pass actually did.

    ``failed`` carries ``(row, exception)`` pairs rather than a count: a row
    that could not be written is the one thing an operator has to act on,
    and a bare number tells them nothing about which.
    """

    written: int = 0
    already_present: int = 0
    hidden: int = 0
    repaired_hidden: int = 0
    failed: list[tuple[Mapping[str, Any], BaseException]] = field(default_factory=list)
    #: ``(row, description)`` for every source row the store already holds
    #: under the same identity but with DIFFERENT values. Never resolved
    #: silently — see :func:`migrate_rows`.
    collisions: list[tuple[Mapping[str, Any], str]] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failed and not self.collisions


def migrate_rows(
    rows: Iterable[Mapping[str, Any]],
    *,
    open_store: Callable[[], Any],
    to_record: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    key_of: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    should_hide: Callable[[Mapping[str, Any]], bool] | None = None,
    collides_with: Callable[[Any, Mapping[str, Any]], "str | None"] | None = None,
    actor: str | None = None,
) -> MigrationReport:
    """Put every row into the store. Already-present records are LEFT ALONE.

    ``to_record`` maps one SQLite row to the store's values;
    ``key_of`` maps a RECORD to its identity dict.

    ``should_hide`` lets a consumer carry a soft-deleted legacy row across
    as a HIDDEN record. That matters wherever the old table had a tombstone
    column: re-inserting such a row as live would resurrect something the
    operator withdrew, and dropping it would erase the difference between
    "never existed" and "existed and stopped". The record is written first
    and hidden second, so its values and its history survive.

    A RE-RUN REPAIRS A HALF-DONE HIDE, and it has to. The write above is two
    ops, and only the first is protected by ``NEW_RECORD``: if the ``put``
    lands and the ``hide`` then fails (connection dropped, server restarted),
    the record exists and is LIVE. Without the repair below, the re-run this
    library advertises as safe would see "already present" and leave it that
    way permanently — a withdrawn node resurrected in the directory, which
    for a routing table means peers dialling an address nothing answers.
    "Idempotent" has to mean CONVERGES, not "does nothing the second time".

    ``collides_with`` decides whether an already-present record AGREES with
    the source row. Returning a description marks a COLLISION, which is
    never resolved silently and never counted as success. This matters for
    any table whose per-host copies were allowed to diverge: with several
    hosts migrating their own file into one shared store, "skip what is
    already there" quietly resolves every disagreement in favour of
    whichever host ran first, which is an arbitrary winner chosen by run
    order and invisible in the output.

    One row's failure never aborts the pass. A migration that stops at the
    first bad row strands every row after it, and the operator finds out by
    counting.
    """
    from scitex_dev.store import ANY_REVISION, NEW_RECORD, RevisionMismatchError

    report = MigrationReport()
    store = open_store()
    try:
        for row in rows:
            record = to_record(row)
            key = key_of(record)
            wants_hidden = should_hide is not None and should_hide(row)
            try:
                existing = store.get(key, include_hidden=True)
                if existing is not None:
                    description = (
                        collides_with(existing, row)
                        if collides_with is not None
                        else None
                    )
                    if description is not None:
                        report.collisions.append((row, description))
                        continue
                    report.already_present += 1
                    # Converge a half-done hide from an earlier pass.
                    if wants_hidden and not existing.hidden:
                        store.hide(key, expected_revision=ANY_REVISION, actor=actor)
                        report.repaired_hidden += 1
                    continue
                store.put(record, expected_revision=NEW_RECORD)
                report.written += 1
                if wants_hidden:
                    store.hide(key, expected_revision=ANY_REVISION, actor=actor)
                    report.hidden += 1
            except RevisionMismatchError:
                # Another writer arrived between the read and the put. Not an
                # error: the record exists, which is the goal.
                report.already_present += 1
            except Exception as exc:  # noqa: BLE001 - reported, not swallowed
                report.failed.append((row, exc))
    finally:
        store.close()
    return report


def add_common_arguments(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    """Attach ``--commit`` and ``--db-path``, worded identically everywhere.

    Two flags, one meaning each, spelled the same in every migration — so an
    operator who has run one of these has run all of them.
    """
    parser.add_argument(
        "--commit",
        action="store_true",
        help="actually write; without it this is a dry run that touches nothing",
    )
    parser.add_argument(
        "--db-path",
        type=Path,
        default=None,
        help=(
            "SQLite state.db to read (default: the HOST runtime state.db, or "
            "$SCITEX_AGENT_CONTAINER_STATE_DB when set). Run this on the "
            "HOST: a container resolves its own per-agent shard and would "
            "migrate an empty file while reporting success."
        ),
    )
    return parser


def _preview_collisions(
    rows: Sequence[Mapping[str, Any]],
    *,
    open_store: Callable[[], Any],
    to_record: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    key_of: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    collides_with: Callable[[Any, Mapping[str, Any]], "str | None"],
    log: Callable[[str], None],
) -> int:
    """Report, without writing, which source rows disagree with the store.

    Returns the number found. A store that cannot be opened is NOT an error
    here: a dry run's job is to answer "what would move?", and it must keep
    answering that on a host where scitex-dev is absent or PostgreSQL is
    down. The inability to check is said out loud rather than reported as
    "no collisions", which is the reading that would make a green dry run
    mean nothing.
    """
    try:
        store = open_store()
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        log(f"  (collision check SKIPPED — cannot open the store: {exc!r})")
        return 0
    found = 0
    try:
        for row in rows:
            key = key_of(to_record(row))
            existing = store.get(key, include_hidden=True)
            if existing is None:
                continue
            description = collides_with(existing, row)
            if description is not None:
                found += 1
                log(f"  COLLISION {key}: {description}")
    finally:
        store.close()
    return found


def run_migration(
    *,
    argv: Sequence[str] | None,
    description: str,
    source: SqliteSource,
    store_name: str,
    open_store: Callable[[], Any],
    to_record: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    key_of: Callable[[Mapping[str, Any]], Mapping[str, Any]],
    verify: Callable[[], int],
    describe_row: Callable[[Mapping[str, Any]], str],
    should_hide: Callable[[Mapping[str, Any]], bool] | None = None,
    collides_with: Callable[[Any, Mapping[str, Any]], "str | None"] | None = None,
    actor: str | None = None,
    log: Callable[[str], None] = print,
) -> int:
    """Parse, read, (maybe) write, verify. Returns the process exit code.

    ``verify`` is the consumer's PRODUCTION reader — the same function the
    application uses — returning how many records the store holds. It is
    called only after a commit, and a count below the number of rows read is
    reported as a MISMATCH and a FAILURE. Verifying through the production
    path rather than the write count is the difference between "the puts
    returned" and "the application can see them".
    """
    parser = add_common_arguments(argparse.ArgumentParser(description=description))
    args = parser.parse_args(argv)

    db_path = args.db_path or default_db_path()
    log(f"source: {db_path}{'' if db_path.is_file() else '  (ABSENT)'}")
    log(f"target: {store_name}")
    log(f"mode:   {'COMMIT' if args.commit else 'DRY RUN'}")

    rows = source.read(db_path, log=log)
    log(f"{source.table}: {len(rows)} row(s)")

    if not args.commit:
        for row in rows:
            log(f"  {describe_row(row)}")
        # A dry run that cannot see collisions is not a preview of the
        # commit, and this is the one thing an operator most needs to know
        # BEFORE writing: which names the shared store already holds with
        # different values. Reading the store here is safe — it opens the
        # target read-only in effect (get, never put) — and it is skipped
        # entirely when scitex-dev is absent, which is the other thing a dry
        # run has to survive.
        if collides_with is not None:
            found = _preview_collisions(
                rows,
                open_store=open_store,
                to_record=to_record,
                key_of=key_of,
                collides_with=collides_with,
                log=log,
            )
            if found:
                log(
                    f"\nDRY RUN — {len(rows)} row(s) read, {found} COLLISION(S) "
                    f"above must be resolved before --commit will succeed."
                )
                return 1
        log(f"\nDRY RUN — {len(rows)} row(s) would move. Re-run with --commit.")
        return 0

    report = migrate_rows(
        rows,
        open_store=open_store,
        to_record=to_record,
        key_of=key_of,
        should_hide=should_hide,
        collides_with=collides_with,
        actor=actor,
    )
    log(
        f"  {report.written} written "
        f"({report.hidden} of them carried across as WITHDRAWN), "
        f"{report.already_present} already present and left untouched"
    )
    if report.repaired_hidden:
        log(
            f"  {report.repaired_hidden} already-present record(s) were LIVE "
            f"but should be withdrawn — re-hidden (a half-done pass repaired)"
        )
    for row, exc in report.failed:
        log(f"  FAILED {describe_row(row)}: {exc!r}")
    for row, description in report.collisions:
        log(f"  COLLISION {describe_row(row)}: {description}")

    present = verify()
    log(f"  verify: {present} record(s) visible through the production reader")
    if present < len(rows) or not report.ok:
        log(
            f"  MISMATCH — read {len(rows)} row(s), store shows {present}, "
            f"{len(report.failed)} row(s) failed. NOT a success."
        )
        log("FAILED — the table did not verify. SQLite left untouched.")
        return 1
    log("SQLite untouched — the old table remains as a fallback.")
    return 0
