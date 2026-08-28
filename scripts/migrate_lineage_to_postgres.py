#!/usr/bin/env python3
"""One-shot: copy the SQLite ``lineage`` table into PostgreSQL.

Companion to the ``state_db_lineage_store`` port (2026-08-28). The code
stopped reading SQLite; this moves the edges already there so the spawn DAG
is not stranded in a file nothing opens any more.

THIS IS THE ONE WHERE A MISSED ROW GRANTS AUTHORITY
===================================================
The three migrations before this each failed in a direction. A missed
``comms_nodes`` row made an agent unroutable. A missed ``comms_grants`` row
DENIED a send. A missed ``node_comms_policy`` row could deny OR silently
un-isolate. ``lineage`` fails one way only, and it is the wrong way:

    an agent with no lineage edge is a ROOT, and a root MAY SPAWN.

``spawn_allowed`` reads exactly one fact — "does this caller have a
parent?" — and answers "allowed" when it does not. So a row this script
fails to move does not degrade into a denial that someone notices and
reports; it degrades into PERMISSION, silently. The same absence collapses
``derive_group`` to a singleton (isolating agents that should mesh) and
``descendants_of`` to nothing (so a parent loses ``check_lineage_acl``
authority over agents it actually spawned).

So run this BEFORE the restart that picks up the new code, and read the
verify line rather than the exit code.

IT REFUSES TO PICK A WINNER, AND ON THIS FLEET THAT IS NOT HYPOTHETICAL
======================================================================
Every host migrates its OWN SQLite file into ONE shared store, and those
files were never synced with each other, so the same child can carry
different parents on different machines. That was MEASURED across all four
hosts on 2026-08-28, before a line of this script was written:

    scitex-compute-01   0 edges
    scitex-compute-03   7 edges
    scitex-compute-04  16 edges
    scitex-nas-03       0 edges          (23 total)

and exactly one child disagreed::

    scitex-cards -> proj-scitex-hub          (scitex-compute-03)
    scitex-cards -> scitex-agent-container   (scitex-compute-04)

"Skip what is already there" would resolve that in favour of whichever
host ran the script first — an arbitrary winner, chosen by run order,
invisible in the output, and a silent edit to the ACL, since
``derive_group`` puts ``scitex-cards`` in a different group under each
answer. So a name already in the store with a DIFFERENT parent is a
COLLISION: it is reported, it makes the run exit non-zero, and nothing is
written for that name. The house rule from the ``comms_nodes`` rollout,
where it fired correctly on its first real run.

The operator resolves it by deciding which parent is true and, if the
stored one is wrong, correcting it deliberately — there is no way to do it
by accident, because ``parent_name`` is IMMUTABLE in the store and the
first value is kept forever.

``created_at`` IS CARRIED VERBATIM, never re-stamped. It is IMMUTABLE in
the store, so re-stamping would both destroy the record of when the spawn
happened and make the first genuine collision unreportable — the same
argument ``registered_at`` carries in the ``comms_nodes`` migration.

Nothing is deleted from SQLite; the old table stays as a fallback.

RUN IT TWICE. Edges are written by live daemons: every ``sac agents
start`` funnels through ``check_spawn`` and every brokered spawn through
the host listen. Run it once now, restart so the daemons pick up the new
code, then run it again to sweep whatever the old path wrote in between.

A DRY RUN IS THE DEFAULT and it must be RUN ON THE HOST — ``_migrate_lib``
owns both rules and the ``$HOME`` trap behind the second one.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from _migrate_lib import SqliteSource, run_migration  # noqa: E402

from scitex_agent_container._state.state_db_lineage_store import (  # noqa: E402
    ACTOR,
    LINEAGE_STORE,
    new_lineage_store,
    read_edges,
)

SOURCE = SqliteSource(
    table="lineage",
    columns=("child_name", "parent_name", "created_at"),
    # ``child_name`` rather than rowid: it is the identity, so a listing
    # ordered by it is the one an operator reads the DAG by, and it stays
    # stable across a re-run.
    order_by="child_name ASC",
)


def _record(row: Mapping[str, Any]) -> dict[str, Any]:
    """One SQLite row as the store's three-field record."""
    return {
        "child_name": str(row["child_name"]),
        "parent_name": str(row["parent_name"]),
        "created_at": float(row["created_at"]),
    }


def _key(record: Mapping[str, Any]) -> dict[str, Any]:
    return {"child_name": record["child_name"]}


def _collides_with(existing: Any, row: Mapping[str, Any]) -> "str | None":
    """Does the stored edge disagree with this file about WHO THE PARENT IS?

    Compares ``parent_name`` only. ``created_at`` deliberately does NOT
    participate: two hosts can hold the same edge stamped at different
    moments (each recorded it when it observed the spawn), and that is
    agreement about the thing the ACL reads, not a conflict.
    """
    stored_parent = str(existing.values["parent_name"])
    incoming_parent = str(row["parent_name"])
    if stored_parent == incoming_parent:
        return None
    child = str(row["child_name"])
    return (
        f"the store already says {child}'s parent is {stored_parent!r} "
        f"(written by {str(existing.origin)!r}); this file says "
        f"{incoming_parent!r}. These put {child} in DIFFERENT ACL groups, so "
        f"there is no safe default. Decide which parent is true; the stored "
        f"one cannot be overwritten (parent_name is IMMUTABLE) and both "
        f"values remain in the oplog."
    )


def _describe(row: Mapping[str, Any]) -> str:
    return f"{row['child_name']} -> {row['parent_name']}"


def _verify_factory(rows: list[Mapping[str, Any]]):
    """Build a verifier that asserts THIS HOST's edges are readable.

    NOT a global row count, and that distinction is a bug found during the
    ``comms_nodes`` rollout rather than a preference. ``_migrate_lib``
    compares ``verify() < len(rows)``, so a verifier returning "how many
    records does the store hold" is VACUOUSLY TRUE the moment a second host
    migrates: the shared store already holds the first host's rows, the
    count exceeds this host's row count no matter what happened, and a run
    that wrote nothing at all still verifies green.

    So this counts how many of THE ROWS THIS RUN READ are visible through
    the production reader (:func:`read_edges`, the same index the ACL
    walks), which is the question the operator is actually asking.
    """

    def _verify() -> int:
        edges = read_edges()
        return sum(
            1
            for row in rows
            if edges.parent(str(row["child_name"])) == str(row["parent_name"])
        )

    return _verify


def main(argv: list[str] | None = None) -> int:
    # ``run_migration`` reads the rows itself, and the verifier needs the
    # same list. Reading them twice is cheap (23 rows fleet-wide) and keeps
    # the library's single-source-of-truth read path unchanged.
    captured: list[Mapping[str, Any]] = []

    class _CapturingSource(SqliteSource):
        def read(self, db_path: Path, *, log: Any = print) -> list[dict]:
            rows = super().read(db_path, log=log)
            captured.clear()
            captured.extend(rows)
            return rows

    source = _CapturingSource(
        table=SOURCE.table, columns=SOURCE.columns, order_by=SOURCE.order_by
    )
    return run_migration(
        argv=argv,
        description=__doc__,
        source=source,
        store_name=LINEAGE_STORE,
        # ``new_...``, not the shared ``open_...``: the library closes the
        # handle it is given, and closing the process-wide one would leave
        # the verification read below holding a dead connection.
        open_store=new_lineage_store,
        to_record=_record,
        key_of=_key,
        verify=_verify_factory(captured),
        describe_row=_describe,
        collides_with=_collides_with,
        actor=ACTOR,
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
