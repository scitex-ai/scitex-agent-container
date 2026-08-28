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
:func:`_refusals` for the discriminator (a stored row whose ``ts`` postdates
the whole import cannot belong to an older residency) and for the exact
message it prints. The dry run performs the same check, so the refusal
surfaces before the cutover window rather than inside it.

RE-RUNNING IS SAFE
==================
The insert is ``ON CONFLICT (target, id) DO NOTHING`` inside ONE
transaction, and the offset decision (see :func:`_offset_for`) probes
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
from typing import Any, Mapping, Sequence

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from _migrate_lib import SqliteSource, add_common_arguments, default_db_path  # noqa: E402

TABLE = "channel_events"

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


def _group_by_target(rows: Sequence[Mapping[str, Any]]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for row in rows:
        grouped.setdefault(str(row["target"]), []).append(dict(row))
    for entries in grouped.values():
        entries.sort(key=lambda r: int(r["id"]))
    return grouped


def _offset_for(conn: Any, *, target: str, entries: list[dict]) -> tuple[int, bool]:
    """``(offset, already_imported)`` for this host's rows on this target.

    THE PROBE IS BY CONTENT, NOT BY POSITION, and that is a correction. The
    first version asked "is the row sitting at the source's own top id
    mine?" — a question that is only valid when the previous run applied
    offset 0. For a RELOCATED target the previous run shifted this host's
    rows above an earlier residency, so the probe landed on the EARLIER
    host's row, saw a mismatch, and concluded "not mine". It then returned
    the (now higher) ``pg_max``, so ``ON CONFLICT (target, id) DO NOTHING``
    had no id to conflict on and the whole history was inserted AGAIN at
    fresh ids — one extra copy per invocation, with the verification below
    unable to see it because it windows on the shifted range it just wrote.

    Asking WHERE THIS ENVELOPE ALREADY SITS answers the same question
    without assuming the answer. A match yields the offset the previous run
    used, so the re-import conflicts on every id and moves nothing.

    The candidate is confirmed against the source's FIRST row as well as its
    last: ``meta_json`` is not unique by construction (the at-least-once
    retry path can duplicate an envelope), so a lookalike top row alone must
    not be allowed to imply an offset for the whole run.

    ``already_imported`` distinguishes "we have been here before" from "these
    ids belong to somebody else", which is what :func:`_refusals` needs to
    tell a relocation apart from a daemon that has moved on.
    """
    pg_max = int(
        conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM sac_channel_events WHERE target = %s",
            (target,),
        ).fetchone()[0]
    )
    if pg_max == 0:
        return 0, False

    first, top = entries[0], entries[-1]
    landed_at = [
        int(r[0])
        for r in conn.execute(
            "SELECT id FROM sac_channel_events "
            "WHERE target = %s AND meta_json = %s ORDER BY id",
            (target, top["meta_json"]),
        ).fetchall()
    ]
    for landed in landed_at:
        offset = landed - int(top["id"])
        if offset < 0:
            continue
        anchor = conn.execute(
            "SELECT meta_json FROM sac_channel_events WHERE target = %s AND id = %s",
            (target, int(first["id"]) + offset),
        ).fetchone()
        if anchor is not None and anchor[0] == first["meta_json"]:
            return offset, True
    return pg_max, False


def _newer_rows_than_source(conn: Any, *, target: str, entries: list[dict]) -> int:
    """How many rows this target already holds that POSTDATE the import."""
    newest = max(float(row["ts"]) for row in entries)
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM sac_channel_events "
            "WHERE target = %s AND ts > %s",
            (target, newest),
        ).fetchone()[0]
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


def _dry_run_refusals(grouped: dict[str, list[dict]]) -> list[str]:
    """:func:`_refusals` for the dry run — READ-ONLY, and never fatal.

    IT MUST NOT RUN THE DDL. ``new_channel_connection`` applies the schema on
    open, so reaching for it here would make the dry run CREATE the two
    tables — a write, in the mode whose entire contract is that it writes
    nothing. It connects raw instead and treats an absent table as "nothing
    to refuse", which is exactly true: a store with no rows cannot have moved
    on past this import.

    A store that cannot be opened is SAID OUT LOUD and skipped rather than
    reported as "nothing refused" — the reading that would make a clean dry
    run mean nothing. Same shape as ``_migrate_lib._preview_collisions``.
    """
    try:
        import psycopg

        from scitex_agent_container._state.state_db_channel_store import (
            _resolve_target,
        )

        conn = psycopg.connect(str(_resolve_target().dsn), autocommit=True)
    except Exception as exc:  # noqa: BLE001 - reported, not swallowed
        print(f"  (ordering check SKIPPED — cannot open the store: {exc!r})")
        return []
    try:
        exists = conn.execute(
            "SELECT to_regclass('sac_channel_events') IS NOT NULL"
        ).fetchone()[0]
        if not exists:
            return []
        return _refusals(conn, grouped)
    finally:
        conn.close()


def _refusals(conn: Any, grouped: dict[str, list[dict]]) -> list[str]:
    """Targets this migration MUST NOT touch, with the remedy named.

    THE ORDERING THIS ENFORCES: run the one-shot BEFORE the new code starts
    serving, per target. ``init_channel_schema`` creates the tables lazily on
    first connect, so the ordinary deploy sequence — restart ``sac listen``
    with the new code, then run this — has the daemon minting ids from 1 for
    any target that receives a message in the gap. Those rows are genuinely
    new, but to an id-shifting importer they are indistinguishable from an
    earlier host's residency, and shifting the migrated history ABOVE them
    produces exactly the failure this design exists to prevent: every
    SQLite-era ``Last-Event-ID`` resolves to a DIFFERENT event, and the
    post-cutover rows sit BELOW every live cursor, unreachable through
    ``id > cursor`` forever.

    So the script REFUSES rather than guessing. The discriminator is time: a
    row already in the store whose ``ts`` postdates everything we are about
    to import cannot be part of an OLDER residency, and is therefore the
    daemon having moved on. A genuine relocation imported
    oldest-residency-first never trips this, because the earlier host's rows
    are older than the later host's by construction.

    A target we have ALREADY imported is exempt: its own rows are trivially
    not newer than themselves, and a re-run must stay a no-op.
    """
    refused: list[str] = []
    for target in sorted(grouped):
        entries = grouped[target]
        _, already = _offset_for(conn, target=target, entries=entries)
        if already:
            continue
        newer = _newer_rows_than_source(conn, target=target, entries=entries)
        if newer:
            refused.append(
                f"{target}: the store already holds {newer} row(s) NEWER than "
                f"anything in this state.db — the daemon has served this "
                f"target since the cutover. Importing now would shift this "
                f"history above them and strand both. STOP `sac listen` on "
                f"every host serving {target!r}, re-run this, then start it."
            )
    return refused


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
    args = parser.parse_args(argv)

    db_path = args.db_path or default_db_path()
    print(f"source: {db_path}{'' if db_path.is_file() else '  (ABSENT)'}")
    print(f"mode:   {'COMMIT' if args.commit else 'DRY RUN'}")

    rows = SOURCE.read(db_path)
    grouped = _group_by_target(rows)
    print(f"{TABLE}: {len(rows)} row(s) across {len(grouped)} target(s)")

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
        blocked = _dry_run_refusals(grouped)
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
    from scitex_agent_container._state.state_db_channel_store import (
        channel_store_locator,
        new_channel_connection,
    )

    print(f"target: {channel_store_locator()}")
    conn = new_channel_connection()
    problems: list[str] = []
    try:
        blocked = _refusals(conn, grouped)
        if blocked:
            for line in blocked:
                print(f"  REFUSED {line}")
            print("REFUSED — nothing was written. SQLite left untouched.")
            return 1

        offsets: dict[str, int] = {}
        # ONE transaction for the whole import: a partial import would make
        # the re-run probe in ``_offset_for`` ambiguous, and an ambiguous
        # probe is how a re-run duplicates a target's whole history.
        with conn.transaction():
            for target in sorted(grouped):
                entries = grouped[target]
                offset, _already = _offset_for(conn, target=target, entries=entries)
                offsets[target] = offset
                _insert(conn, entries=entries, offset=offset)
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
