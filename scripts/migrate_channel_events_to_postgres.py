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

RE-RUNNING IS SAFE
==================
The insert is ``ON CONFLICT (target, id) DO NOTHING`` inside ONE
transaction, and the offset decision (see :func:`_offset_for`) probes
whether the rows already in PostgreSQL are THIS host's before it shifts
anything. A second run of the same host therefore moves nothing and shifts
nothing.

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


def _offset_for(conn: Any, *, target: str, entries: list[dict]) -> int:
    """How far THIS host's ids must shift to clear what PostgreSQL holds.

    Zero in two distinct cases, and telling them apart is what makes a
    re-run safe:

    * the target has no rows yet — nothing to clear;
    * the rows already there are OURS (a previous run of this same host),
      identified by probing the source's HIGHEST id and comparing
      ``meta_json`` byte for byte. The whole import is one transaction, so a
      partial import cannot exist and that single probe is conclusive.

    Otherwise the rows belong to an EARLIER host's residency of this target,
    and ours go above them.
    """
    pg_max = int(
        conn.execute(
            "SELECT COALESCE(MAX(id), 0) FROM sac_channel_events WHERE target = %s",
            (target,),
        ).fetchone()[0]
    )
    if pg_max == 0:
        return 0
    top = entries[-1]
    probe = conn.execute(
        "SELECT meta_json FROM sac_channel_events WHERE target = %s AND id = %s",
        (target, int(top["id"])),
    ).fetchone()
    if probe is not None and probe[0] == top["meta_json"]:
        return 0
    return pg_max


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
        print(
            f"\nDRY RUN — {len(rows)} row(s) would move, ids preserved. "
            "Re-run with --commit. Import the OLDEST-RESIDENCY host FIRST "
            "for any target that has moved between hosts."
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
        offsets: dict[str, int] = {}
        # ONE transaction for the whole import: a partial import would make
        # the re-run probe in ``_offset_for`` ambiguous, and an ambiguous
        # probe is how a re-run duplicates a target's whole history.
        with conn.transaction():
            for target in sorted(grouped):
                entries = grouped[target]
                offset = _offset_for(conn, target=target, entries=entries)
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
