#!/usr/bin/env python3
"""Which id windows in ``sac_channel_events`` were IMPORTED, and from where.

THE GUARD THAT COULD NOT BE SATISFIED (measured 2026-08-28)
===========================================================
``migrate_channel_events_to_postgres.py`` refuses to import a target when the
store already holds a row NEWER than everything in the source ``state.db``,
reasoning that such a row cannot belong to an OLDER residency and is therefore
the live daemon having moved on. That reasoning assumes SEQUENTIAL relocation:
host A serves a target until date X, then host B takes over.

This fleet violates the assumption. compute-04 and compute-03 served the SAME
targets over OVERLAPPING periods — ``scitex-agent-container`` ran 08-09..08-28
on compute-04 and 08-18..08-22 on compute-03 — so once compute-04's history
was imported, compute-03's import was refused by rows that were not daemon
traffic at all. Measured directly against ``sac_channel_events``:

    scitex-agent-container   145 rows newer   0 written after the cutover
    scitex-cards               2 rows newer   0 written after the cutover
    figrecipe                  5 rows total   all of it history

The predicate is structurally unsatisfiable in that shape — stopping
``sac listen`` on both hosts changes nothing, because the offending rows were
already written and are not going away. Nothing an operator can do clears it.

WHAT ACTUALLY DISCRIMINATES: WAS THE ROW IMPORTED, OR WRITTEN LIVE?
==================================================================
Time cannot answer that, and no amount of sharpening the ts comparison will
make it: an imported row and a daemon row can carry the same timestamp. The
answer has to be RECORDED at import time, and that is all this module does.

The guard then keeps the ts test — the negative controls that pin the real
post-cutover hazard still pass for the reason they always did — and applies it
only to rows NO IMPORT CLAIMS. A daemon row minted in the deploy gap belongs
to no import window, is newer than the source, and is still refused.

A WINDOW PER IMPORT, NOT A COLUMN PER ROW — AND WHY
===================================================
The obvious alternative is a provenance column on ``sac_channel_events``.
Rejected on three measured grounds:

1. ``meta_json`` IS NOT AVAILABLE as a home. Its stored bytes must stay
   IDENTICAL or a replayed SSE frame stops matching the live one
   (``state_db_channel_store`` states this three ways: ``sort_keys``,
   ``ensure_ascii`` and ``jsonb`` normalisation each break it). Writing
   provenance into the envelope would corrupt the exact property the
   migration exists to preserve.
2. A NEW COLUMN CANNOT BE BACKFILLED from the data. The 7,980 rows already
   imported carry no provenance and nothing in them says where they came
   from, so a column would start life NULL — and NULL would have to mean
   "daemon wrote it", which is precisely the wrong answer for every one of
   them. The re-run backfill below has the same cost and does not touch the
   hot table.
3. The table is written once per message and read on every SSE connect. A
   migration-only fact does not belong in it.

WHY THE WINDOWS STAY TRUE. Ids for a target only ever move UP: ``_seed_cursor``
advances the counter with ``GREATEST`` and never winds it back, and a later
host's import is OFFSET ABOVE the existing maximum. So no row is ever inserted
INSIDE a recorded window after the fact, and a window recorded once keeps
meaning what it meant.

BACKFILLING AN IMPORT THAT ALREADY HAPPENED
===========================================
Re-run the migration with ``--commit`` against the EARLIER host's ``state.db``.
That run is already proven to move nothing (``_offset_for`` recognises its own
rows by content), and it now records the window it recognised. The file can be
read from anywhere — ``--db-path`` takes a path — so this does not require
going back to the other machine, only to its ``state.db``.
"""

from __future__ import annotations

import socket
import time
from typing import Any

#: The ledger. Named like the two tables it describes so an operator reading
#: ``\dt sac_*`` sees the set together.
LEDGER_TABLE = "sac_channel_import"

#: ``PRIMARY KEY (target, lo_id, hi_id)`` makes re-recording the same window a
#: no-op, which is what a re-run of an already-imported host must be.
LEDGER_DDL = """
CREATE TABLE IF NOT EXISTS sac_channel_import (
    target          TEXT   NOT NULL,
    lo_id           BIGINT NOT NULL,
    hi_id           BIGINT NOT NULL,
    source_host     TEXT   NOT NULL,
    source_path     TEXT   NOT NULL,
    row_count       BIGINT NOT NULL,
    offset_applied  BIGINT NOT NULL,
    imported_at     DOUBLE PRECISION NOT NULL,
    imported_by     TEXT   NOT NULL,
    PRIMARY KEY (target, lo_id, hi_id)
);
"""


def source_host() -> str:
    """The machine whose ``state.db`` is being read.

    ``gethostname`` is honest for the documented usage — the module docstring
    of the migration says RUN IT ON THE HOST, ONCE PER HOST — and the
    ``source_path`` recorded alongside it disambiguates the backfill case
    where a copied file is imported from somewhere else.
    """
    return socket.gethostname()


def ensure_ledger(conn: Any) -> None:
    """Create the ledger if missing. Idempotent.

    Deliberately NOT part of ``state_db_channel_store._DDL``: no runtime
    reader or writer touches this table, and adding it there would make every
    agent's connect apply DDL for a one-shot's bookkeeping.
    """
    conn.execute(LEDGER_DDL)


def ledger_exists(conn: Any) -> bool:
    """Is the ledger present? A dry run must not create it to find out."""
    return bool(
        conn.execute(
            "SELECT to_regclass(%s) IS NOT NULL", (LEDGER_TABLE,)
        ).fetchone()[0]
    )


def record_import(
    conn: Any,
    *,
    target: str,
    lo_id: int,
    hi_id: int,
    source_path: str,
    row_count: int,
    offset_applied: int,
) -> None:
    """Claim ``[lo_id, hi_id]`` on ``target`` as an import, not daemon traffic.

    The ids recorded are POST-OFFSET — where the rows actually sit — because
    that is the space the guard's ``BETWEEN`` is evaluated in.
    """
    conn.execute(
        "INSERT INTO sac_channel_import (target, lo_id, hi_id, source_host, "
        "source_path, row_count, offset_applied, imported_at, imported_by) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, current_user) "
        "ON CONFLICT (target, lo_id, hi_id) DO NOTHING",
        (
            target,
            lo_id,
            hi_id,
            source_host(),
            source_path,
            row_count,
            offset_applied,
            time.time(),
        ),
    )


def newer_rows(conn: Any, *, target: str, newest_ts: float) -> tuple[int, int]:
    """``(unattributed, attributed)`` rows on ``target`` postdating the import.

    ``unattributed`` — no recorded import claims them — is the number the
    guard acts on. ``attributed`` is reported so the operator can see that the
    script looked and found provenance, rather than wondering whether it
    checked at all.

    A store with NO ledger yet answers ``(all, 0)``, which is exactly the
    pre-provenance behaviour. That is deliberate: a first run against such a
    store has no grounds to be more permissive than the old guard was.
    """
    total = int(
        conn.execute(
            "SELECT COUNT(*) FROM sac_channel_events "
            "WHERE target = %s AND ts > %s",
            (target, newest_ts),
        ).fetchone()[0]
    )
    if not total or not ledger_exists(conn):
        return total, 0
    unattributed = int(
        conn.execute(
            "SELECT COUNT(*) FROM sac_channel_events e "
            "WHERE e.target = %s AND e.ts > %s AND NOT EXISTS ("
            "  SELECT 1 FROM sac_channel_import i "
            "  WHERE i.target = e.target AND e.id BETWEEN i.lo_id AND i.hi_id)",
            (target, newest_ts),
        ).fetchone()[0]
    )
    return unattributed, total - unattributed
