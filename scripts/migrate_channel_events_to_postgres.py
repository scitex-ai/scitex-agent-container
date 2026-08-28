#!/usr/bin/env python3
"""Carry ``channel_events`` from SQLite into ``sac_channel_events``.

The LAST of the ten-plus ``migrate_*_to_postgres`` one-shots, and the only
one that does NOT go through :mod:`_migrate_lib`'s ``migrate_rows``. That
helper writes ``scitex_dev.store`` RECORDS; ADR-0023 keeps this table out of
the store (three measured disqualifiers), so the write path here is plain
SQL. The parts that are about the OPERATOR rather than the storage —
``default_db_path``, the read-only ``SqliteSource``, the identical
``--commit`` / ``--db-path`` wording — are reused, because an operator who
has run one of these has run all of them.

RUN IT ON THE HOST, ONCE PER HOST
=================================
``default_db_path`` resolves differently inside a container and on the bare
host. A container gets its own per-agent shard under ``/state/<agent>/
state.db``; the host gets ``~/.scitex/agent-container/runtime/state.db``. A
container run migrates an EMPTY shard, faithfully, and reports success —
the shape that made the diary migration look finished for three days. The
resolved path is printed before any work for exactly that reason.

IDS ARE PRESERVED, NOT REMAPPED — AND THAT IS THE POINT
=======================================================
The id IS the SSE cursor. A consumer that dropped its stream holding
``Last-Event-ID: 41`` reconnects asking for everything after 41, so a
migration that renumbered rows would hand it either a replay or a silent
gap, with no error anywhere.

Preserving them is SAFE because per-target ids do not collide across hosts:
a cross-host send is FORWARDED to the destination host before it is
persisted (``_node_channel.node_message_send`` resolves the target host and
POSTs onward BEFORE ``persist_event``), so a given target's rows only ever
exist on the one host that was serving it. For every target that has not
been relocated, ``new_id == old_id``.

A RELOCATED target is the exception this script has to handle: its rows can
exist on two hosts, each numbering from 1. The later host's rows are offset
above the earlier host's maximum — which shifts that host's ids, so
**import the OLDEST-RESIDENCY host first** and the shift lands on the rows
whose consumers are least likely to still be holding a cursor. The offset
is printed per target; there is no silent renumbering.

RUN THIS BEFORE THE NEW CODE SERVES — THE SCRIPT ENFORCES IT
============================================================
``init_channel_schema`` creates the tables lazily on first connect, so the
ordinary deploy sequence (restart ``sac listen`` on the new code, then run
the one-shot) lets the daemon mint ids from 1 for any target that receives a
message in the gap. Shifting the migrated history above those rows strands
BOTH halves: every SQLite-era ``Last-Event-ID`` would resolve to a different
event, and the post-cutover rows would sit below every live cursor,
unreachable through ``id > cursor`` forever.

So the order is: **stop ``sac listen``, run this, start ``sac listen``** —
and the script REFUSES rather than trusting anyone to remember. See
:func:`_channel_import_guard.refusals` for the discriminator and for the
exact message it prints.
The dry run performs the same check, so the refusal surfaces before the
cutover window rather than inside it.

RESIDENCIES CAN OVERLAP, AND THE FIRST DISCRIMINATOR COULD NOT SEE IT
=====================================================================
That refusal originally keyed on TIME alone: a stored row postdating the whole
import "cannot belong to an older residency". True only for SEQUENTIAL
relocation. compute-04 and compute-03 served the SAME targets over INTERLEAVED
date ranges, so once compute-04's history was imported, compute-03's import
was refused by 145 + 2 + 5 rows of which ZERO had been written after the
cutover — and stopping ``sac listen``, which is what the message told the
operator to do, could not clear it, because the rows were already in the
table. The predicate was unsatisfiable.

Time cannot answer "imported, or written live?"; the answer has to be
RECORDED. Each import now claims the id window it landed in, in
``sac_channel_import`` (see :mod:`_channel_import_provenance`), and the ts test
is applied only to rows NO IMPORT CLAIMS. A store whose history predates that
ledger is backfilled by RE-RUNNING this script against the earlier host's
``state.db`` — a no-op for the rows, which records the window it recognises.

THE TABLES ARE CREATED BY WHOEVER RUNS THIS, AND THAT CAUSED AN OUTAGE
======================================================================
Run as ``ywatanabe__cli`` on 2026-08-28, this script left both tables owned by
that leaf role. Every agent connects as ``ywatanabe__<agent>`` and reaches
``init_channel_schema``, whose ``CREATE INDEX IF NOT EXISTS`` needs OWNERSHIP
of the table; the fleet's channel began failing with ``must be owner of table
sac_channel_events`` three minutes later and stayed down for six. The
post-migration verification reported success throughout, because it ran as the
writer.

Ownership is therefore settled BEFORE any rows move (:mod:`_pg_table_owner`):
the intended owner is DERIVED from the rest of the schema, drifted tables are
handed back to it, and the outcome is verified by asking the catalog about
OTHER roles by name. If that cannot be made true, the migration REFUSES —
failing here is far better than a channel outage three minutes later.

RE-RUNNING IS SAFE
==================
The insert is ``ON CONFLICT (target, id) DO NOTHING`` inside ONE
transaction, and the offset decision (see
:func:`_channel_import_guard.offset_for`) probes
whether the rows already in PostgreSQL are THIS host's before it shifts
anything. A second run of the same host therefore moves nothing and shifts
nothing — INCLUDING a host whose ids were shifted by an earlier residency,
which the first version of that probe got wrong: it asked its question
positionally, so a relocated host re-imported its entire history on every
invocation while the verification printed "MATCHES SQLite". The probe is by
CONTENT now, and ``test_a_second_run_of_a_relocated_host_moves_nothing`` /
``test_a_third_run_of_a_relocated_host_still_moves_nothing`` pin it.

DELIVERED ROWS COME TOO
=======================
All of them, not just the undelivered ones: ``list_since_id`` — the
``Last-Event-ID`` reconnect path — reads regardless of ``delivered_at``. A
migration that carried only the undelivered rows would leave every
reconnecting client resuming into a hole.

SQLITE IS LEFT EXACTLY AS FOUND. No DELETE, no VACUUM, no DROP. Rollback is
"point the code back at SQLite".
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Any, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import _channel_import_guard as guard  # noqa: E402
import _channel_import_provenance as prov  # noqa: E402
import _pg_table_owner as owners  # noqa: E402
from _migrate_lib import (  # noqa: E402
    SqliteSource,
    add_common_arguments,
    default_db_path,
)

TABLE = "channel_events"

#: Every table this script may CREATE, and therefore every table whose owner
#: it is responsible for. The ledger is in here too: a future run by a
#: different role would hit ``must be owner`` on it just as readily.
MANAGED = ("sac_channel_events", "sac_channel_cursor", prov.LEDGER_TABLE)

SOURCE = SqliteSource(
    table=TABLE,
    columns=(
        "id",
        "target",
        "source",
        "kind",
        "content",
        "meta_json",
        "ts",
        "delivered_at",
    ),
    # By (target, id) rather than rowid: the import groups by target, and a
    # stable per-target ordering makes the dry-run listing readable and the
    # offset probe below deterministic.
    order_by="target ASC, id ASC",
)


def _insert(conn: Any, *, entries: list[dict], offset: int) -> None:
    conn.cursor().executemany(
        "INSERT INTO sac_channel_events "
        "(target, id, source, kind, content, meta_json, ts, delivered_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (target, id) DO NOTHING",
        [
            (
                str(row["target"]),
                int(row["id"]) + offset,
                row.get("source"),
                str(row.get("kind") or "message"),
                row.get("content"),
                # NEVER re-encoded. The stored string must stay byte-identical
                # or a replayed frame stops matching the live one (ADR-0023
                # §5.1).
                str(row["meta_json"]),
                float(row["ts"]),
                None if row.get("delivered_at") is None else float(row["delivered_at"]),
            )
            for row in entries
        ],
    )


def _seed_cursor(conn: Any, *, target: str) -> int:
    """Point the counter at the highest id the target now holds.

    ``GREATEST`` rather than a plain assignment: a live daemon may have
    allocated past this while the migration ran, and winding a counter BACK
    would reuse an id a consumer has already seen — the frame-dropping
    failure the per-target cursor exists to prevent.
    """
    top = int(
        conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM sac_channel_events WHERE target = %s",
            (target,),
        ).fetchone()[0]
    )
    conn.execute(
        "INSERT INTO sac_channel_cursor (target, next_id) VALUES (%s, %s) "
        "ON CONFLICT (target) DO UPDATE SET next_id = "
        "GREATEST(sac_channel_cursor.next_id, EXCLUDED.next_id)",
        (target, top),
    )
    return top


def _sqlite_facts(entries: list[dict]) -> tuple[int, int, int, int]:
    ids = [int(r["id"]) for r in entries]
    undelivered = sum(1 for r in entries if r.get("delivered_at") is None)
    return len(entries), min(ids), max(ids), undelivered


def _pg_facts(conn: Any, *, target: str, lo: int, hi: int) -> tuple[int, int, int, int]:
    """The same four numbers, over the id window this host's rows occupy.

    Windowed rather than whole-target, because a relocated target can hold a
    previous host's rows too and comparing totals would report a mismatch
    that is not one.
    """
    row = conn.execute(
        "SELECT COUNT(*), COALESCE(MIN(id), 0), COALESCE(MAX(id), 0), "
        "COUNT(*) FILTER (WHERE delivered_at IS NULL) "
        "FROM sac_channel_events WHERE target = %s AND id BETWEEN %s AND %s",
        (target, lo, hi),
    ).fetchone()
    return int(row[0]), int(row[1]), int(row[2]), int(row[3])


def _settle_ownership(conn: Any, *, owner_override: str | None) -> list[str]:
    """Put the tables in hands the FLEET can use, before a single row moves.

    The steps are ordered by what each one needs from the one before, and the
    two GATES come first so a refusal never leaves anything behind:

    1. derive the intended owner from the rest of the schema;
    2. refuse if this session could not hand a table to it — creating tables
       it cannot give away is how the 2026-08-28 outage started;
    3. refuse if the roles that use the rest of the store could not act as
       that owner. NO DDL HAS RUN YET at this point, deliberately: the
       post-condition check needs the tables to exist, so relying on it alone
       would mean creating the tables in order to discover that nobody can use
       them, and the refusal would leave the hazard it refused;
    4. hand back any table that has ALREADY drifted — the state every host
       left by the previous version of this script is in. Before the DDL,
       because ``CREATE INDEX IF NOT EXISTS`` on a table you do not own raises
       rather than being a no-op;
    5. apply the DDL, hand over what it created, and re-ask (3)'s question of
       the real tables.

    Returns problems; an empty list means the tables are usable. Reowning IS a
    write, so it is reported separately from the all-or-nothing row import and
    the refusal wording says which of the two it is talking about.
    """
    from scitex_agent_container._state.state_db_channel_store import (
        init_channel_schema,
    )

    owner, why = owners.resolve_intended_owner(
        conn, managed=MANAGED, override=owner_override
    )
    print(f"owner:  {owner} ({why})")
    unreachable = owners.owner_is_reachable(conn, owner=owner)
    if unreachable:
        return [unreachable]
    trouble, note = owners.owner_inheritance_problems(
        conn, managed=MANAGED, owner=owner
    )
    print(f"  {note}")
    if trouble:
        return trouble

    for phase in ("before the DDL", "after the DDL"):
        repaired, trouble = owners.ensure_owner(conn, managed=MANAGED, owner=owner)
        for line in repaired:
            print(f"  REOWNED {line} ({phase})")
        if trouble:
            return trouble
        if phase == "before the DDL":
            init_channel_schema(conn)
            prov.ensure_ledger(conn)

    trouble, note = owners.consumer_access_problems(conn, managed=MANAGED)
    print(f"  {note}")
    return trouble


def main(argv: Sequence[str] | None = None) -> int:
    parser = add_common_arguments(
        argparse.ArgumentParser(
            description=(
                "Carry channel_events from SQLite into the shared PostgreSQL "
                "sac_channel_events / sac_channel_cursor tables, preserving "
                "per-target ids. Dry run unless --commit."
            )
        )
    )
    parser.add_argument(
        "--accept-imported-history",
        action="append",
        default=[],
        metavar="TARGET",
        help=(
            "assert that the unattributed rows sitting above TARGET are "
            "another host's imported history whose state.db is gone. NAMED "
            "PER TARGET and repeatable; there is deliberately no --force, "
            "because a blanket bypass restores the corruption the guard "
            "exists to prevent"
        ),
    )
    parser.add_argument(
        "--table-owner",
        metavar="ROLE",
        help=(
            "the role the created tables must end up owned by. Derived from "
            "the rest of the schema when omitted; see _pg_table_owner"
        ),
    )
    args = parser.parse_args(argv)

    db_path = args.db_path or default_db_path()
    print(f"source: {db_path}{'' if db_path.is_file() else '  (ABSENT)'}")
    print(f"mode:   {'COMMIT' if args.commit else 'DRY RUN'}")

    rows = SOURCE.read(db_path)
    grouped = guard.group_by_target(rows)
    print(f"{TABLE}: {len(rows)} row(s) across {len(grouped)} target(s)")

    # A waiver aimed at a target this state.db does not contain waives
    # NOTHING, and would read exactly like one that worked. Refuse the typo.
    accepted = frozenset(args.accept_imported_history)
    unknown = sorted(accepted - set(grouped))
    if unknown:
        parser.error(
            f"--accept-imported-history names {unknown} , which "
            f"{'is' if len(unknown) == 1 else 'are'} not in {db_path}. "
            f"Targets in this file: {sorted(grouped)}"
        )

    if not args.commit:
        for target in sorted(grouped):
            count, lo, hi, undelivered = _sqlite_facts(grouped[target])
            print(
                f"  {target}: {count} row(s), ids {lo}..{hi}, "
                f"{undelivered} undelivered"
            )
        # The refusal check runs HERE TOO, best-effort. A dry run whose only
        # job is "what would move?" is not a preview of the commit if the
        # commit can refuse; and this is the one refusal an operator most
        # needs to see BEFORE a cutover window, not during it. A store that
        # cannot be opened is reported as unchecked rather than as clean —
        # the same shape ``_migrate_lib._preview_collisions`` uses, and for
        # the same reason.
        blocked, waived = guard.dry_run_refusals(grouped, accepted=accepted)
        for line in waived:
            print(f"  ACCEPTED {line}")
        if blocked:
            for line in blocked:
                print(f"  REFUSED {line}")
            print(
                f"\nDRY RUN — {len(rows)} row(s) read, {len(blocked)} target(s) "
                "REFUSED above. --commit would refuse them too."
            )
            return 1
        print(
            f"\nDRY RUN — {len(rows)} row(s) would move, ids preserved. "
            "Re-run with --commit. Run this BEFORE starting `sac listen` on "
            "the new code, and import the OLDEST-RESIDENCY host FIRST for any "
            "target that has moved between hosts."
        )
        return 0

    # Imported here, not at module scope, so a dry run still answers "what
    # would move?" on a host where the write path's dependencies are absent.
    import psycopg

    from scitex_agent_container._state.state_db_channel_store import (
        _resolve_target,
        channel_store_locator,
    )

    print(f"target: {channel_store_locator()}")
    # RAW, not ``new_channel_connection`` — that helper applies the DDL on
    # open, and the DDL is exactly what must wait until ownership is settled.
    conn = psycopg.connect(str(_resolve_target().dsn), autocommit=True)
    problems: list[str] = []
    try:
        unusable = _settle_ownership(conn, owner_override=args.table_owner)
        if unusable:
            for line in unusable:
                print(f"  REFUSED {line}")
            print("REFUSED — no rows were written. SQLite left untouched.")
            return 1

        blocked, waived = guard.refusals(conn, grouped, accepted=accepted)
        for line in waived:
            print(f"  ACCEPTED {line}")
        if blocked:
            for line in blocked:
                print(f"  REFUSED {line}")
            print("REFUSED — nothing was written. SQLite left untouched.")
            return 1

        offsets: dict[str, int] = {}
        # ONE transaction for the whole import: a partial import would make
        # the re-run probe in ``offset_for`` ambiguous, and an ambiguous probe
        # is how a re-run duplicates a target's whole history. The provenance
        # row lands inside it too, so a window is never claimed for rows that
        # did not land.
        with conn.transaction():
            for target in sorted(grouped):
                entries = grouped[target]
                offset, _already = guard.offset_for(
                    conn, target=target, entries=entries
                )
                offsets[target] = offset
                _insert(conn, entries=entries, offset=offset)
                count, lo, hi, _ = _sqlite_facts(entries)
                prov.record_import(
                    conn,
                    target=target,
                    lo_id=lo + offset,
                    hi_id=hi + offset,
                    source_path=str(db_path),
                    row_count=count,
                    offset_applied=offset,
                )
                top = _seed_cursor(conn, target=target)
                note = "" if offset == 0 else f"  OFFSET +{offset} (relocated target)"
                print(f"  {target}: {len(entries)} row(s), cursor -> {top}{note}")

        for target in sorted(grouped):
            entries = grouped[target]
            offset = offsets[target]
            s_count, s_lo, s_hi, s_undelivered = _sqlite_facts(entries)
            p_count, p_lo, p_hi, p_undelivered = _pg_facts(
                conn, target=target, lo=s_lo + offset, hi=s_hi + offset
            )
            mismatches = []
            if p_count != s_count:
                mismatches.append(f"count {s_count} -> {p_count}")
            if p_lo - offset != s_lo:
                mismatches.append(f"min(id) {s_lo} -> {p_lo - offset}")
            if p_hi - offset != s_hi:
                mismatches.append(f"max(id) {s_hi} -> {p_hi - offset}")
            if p_undelivered != s_undelivered:
                mismatches.append(f"undelivered {s_undelivered} -> {p_undelivered}")
            if mismatches:
                problems.append(f"{target}: " + "; ".join(mismatches))
            else:
                print(
                    f"  verify {target}: {p_count} row(s), ids "
                    f"{p_lo - offset}..{p_hi - offset}, "
                    f"{p_undelivered} undelivered — MATCHES SQLite"
                )
    finally:
        conn.close()

    if problems:
        for line in problems:
            print(f"  MISMATCH {line}")
        print("FAILED — the table did not verify. SQLite left untouched.")
        return 1
    print("SQLite untouched — the old table remains as a fallback.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
