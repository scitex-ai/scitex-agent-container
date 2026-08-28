#!/usr/bin/env python3
"""One-shot: copy the SQLite ``comms_nodes`` directory into PostgreSQL.

Companion to the ``state_db_comms_nodes`` port (2026-08-28). The code
stopped reading SQLite; this moves the entries already there so the fleet's
cross-host routing is not stranded in a file nothing opens any more.

THIS IS THE TABLE THAT WAS NEVER REALLY SHARED
==============================================
Every other migration in this directory moved a table from one storage
engine to another. This one also DELETES A SYNC LAYER, and that is why the
operator asked for it: each host held its own ``comms_nodes``, and
``sac registry sync`` ssh-pulled peers' ``sac db export --tables
comms_nodes`` and fed the payload to ``import_state``, which is
``INSERT OR IGNORE`` on the ``name`` primary key. That statement carries
neither an UPDATE nor a deletion, so a node that MOVED and a node that LEFT
both arrived at the peer as ``rowcount == 0`` — indistinguishable from a
byte-identical duplicate. The old module said so itself: the deletion "will
need an UPDATE-shaped sync (future work)".

On the shared primary store every host reads and writes the SAME directory,
so cross-host a2a resolution works with no sync layer at all. Which means a
row this script fails to move is not "stale on one host" — it is absent
from the only directory anybody reads.

WHAT MOVES, AND WHAT THE FOUR-FIELD SCHEMA DROPS
================================================
The store declares ``name``, ``host``, ``a2a_port``, ``registered_at``. The
SQLite table had three more columns, and none of the three is carried as a
value. Stated plainly, because each is a real (accepted) change rather than
a lossless rename:

``updated_at`` — NOT PRESERVED.
    The store's hybrid logical clock restamps a record on every op, so a
    migrated entry's ``updated_at`` reads as THE MIGRATION MOMENT, not as
    when the node last registered. This is accepted rather than worked
    around: ``updated_at`` is a freshness hint for a directory that every
    agent re-registers on every start, so the real value returns within one
    agent lifecycle. It is not an audit fact, and nothing decides anything
    from it. (Contrast ``registered_at``, which IS carried verbatim and is
    IMMUTABLE in the store — see below.)

``source_host`` — NOT PRESERVED, and it would be wrong to fake it.
    Its successor is the reserved ``_origin`` column, which the primitive
    stamps from the WRITING NODE. So every record this script creates has
    the origin of the HOST THAT RAN THE MIGRATION, whatever host the row
    originally came from. There is no way to set it otherwise, and writing
    the old string into some other column would invent a provenance the
    store does not believe. In practice the loss is small: the column was
    NULL for every locally registered row and was set only on the pull path
    that this migration retires.

``ended_at`` — PRESERVED, as a hidden record.
    A tombstoned SQLite row MUST NOT come back as a live directory entry;
    that would resurrect a node the fleet stopped and hand peers an address
    that answers nothing. Dropping such rows instead would erase the
    difference between "was never registered" and "was registered and
    stopped", which is the difference ``hide()`` exists to keep. So the
    entry is written and then hidden, and the counts report it separately.

``registered_at`` IS CARRIED VERBATIM, and that is load-bearing. The field
is IMMUTABLE in the store: two different values for one name is a reported
MergeConflict, which is exactly how ADR-0014's "names are globally unique"
survives replication. Stamping ``time.time()`` here would erase the
evidence AND make the first real collision unreportable.

IT IS IDEMPOTENT, AND IT REFUSES TO PICK A WINNER
=================================================
A name already in the store with the SAME ``(host, a2a_port)`` is left alone:
that is the ordinary re-run, or a live agent that re-registered itself since
the first pass.

A name already in the store with a DIFFERENT ``(host, a2a_port)`` is a
COLLISION, and this script will not resolve it. It is reported, it makes the
run exit non-zero, and nothing is written for that name.

That is not caution for its own sake — it is the direct consequence of what
this migration is FOR. Every host migrates its OWN SQLite file into ONE
shared store, and those files were allowed to diverge precisely because the
sync that was supposed to reconcile them could not carry an update. So two
hosts disagreeing about where ``lead`` lives is the EXPECTED input here, not
an edge case. "Skip what is already there" would resolve every one of those
disagreements in favour of whichever host happened to run the script first —
an arbitrary winner, chosen by run order, invisible in the output, and
wrong half the time for a table whose only job is to say where to send a
message.

WHY NOT "NEWEST ``updated_at`` WINS", WHICH IS THE OBVIOUS RULE
---------------------------------------------------------------
Because it cannot be implemented correctly, and implementing it
approximately would be worse than refusing. The comparison needs both sides'
ORIGINAL ``updated_at``. The store has no such column — the successor to
``updated_at`` is the hybrid logical clock, and THIS SCRIPT stamps it: the
moment host A migrates, A's rows carry A's migration time, which is newer
than anything still sitting in host B's file. A latest-wins rule would
therefore resolve every collision in favour of whichever host ran first —
the same arbitrary winner as skipping, wearing a justification.

The operator resolves a collision with the information the report gives
them (both targets, and which host each came from), using
``sac registry register --name <n> --host <h> --a2a-port <p>``.

Nothing is deleted from SQLite; the old table stays as a fallback.

RUN IT TWICE. Entries are written by live daemons: every ``sac start``, every
``sac listen`` boot, every ``sac mcp channel`` refresh writes one. Run it
once now, restart so the daemons pick up the new code, then run it again to
sweep whatever the old path wrote in between.

A DRY RUN IS THE DEFAULT and it must be RUN ON THE HOST — ``_migrate_lib``
owns both rules and the ``$HOME`` trap behind the second one.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from _migrate_lib import SqliteSource, run_migration  # noqa: E402

from scitex_agent_container._state.state_db_comms_nodes import (  # noqa: E402
    list_comms_nodes,
)
from scitex_agent_container._state.state_db_comms_nodes_store import (  # noqa: E402
    ACTOR,
    COMMS_NODES_STORE,
    new_comms_nodes_store,
)

SOURCE = SqliteSource(
    table="comms_nodes",
    columns=(
        "name",
        "host",
        "a2a_port",
        "registered_at",
        "updated_at",
        "source_host",
        "ended_at",
    ),
    # ``name`` rather than rowid: the identity is the name, so a listing
    # ordered by it is the one an operator reads the directory by, and it
    # stays stable across a re-run even if rows were rewritten in place.
    order_by="name ASC",
)


def _record(row: Mapping[str, Any]) -> dict[str, Any]:
    """One SQLite row as the store's four-field record."""
    return {
        "name": str(row["name"]),
        "host": str(row["host"]),
        "a2a_port": int(row["a2a_port"]),
        "registered_at": float(row["registered_at"]),
    }


def _key(record: Mapping[str, Any]) -> dict[str, Any]:
    return {"name": record["name"]}


def _is_tombstone(row: Mapping[str, Any]) -> bool:
    """``ended_at`` set means the node was unregistered — carry it as hidden."""
    return row.get("ended_at") is not None


def _collides_with(existing: Any, row: Mapping[str, Any]) -> "str | None":
    """Does the stored entry disagree with this SQLite row about the target?

    Compares the routing tuple only. ``registered_at`` deliberately does NOT
    participate: two hosts can hold the same placement stamped at different
    moments (a re-register refreshes one side's row), and that is agreement
    about the thing that matters — where to send a message — not a conflict.
    """
    stored_host = str(existing.values["host"])
    stored_port = int(existing.values["a2a_port"])
    incoming_host = str(row["host"])
    incoming_port = int(row["a2a_port"])
    if stored_host == incoming_host and stored_port == incoming_port:
        return None
    return (
        f"the store already has {stored_host}:{stored_port} "
        f"(written by {str(existing.origin)!r}); this file says "
        f"{incoming_host}:{incoming_port}. Resolve with `sac registry "
        f"register --name {str(row['name'])} --host <winner> --a2a-port "
        f"<port>` and re-run."
    )


def _describe(row: Mapping[str, Any]) -> str:
    mark = "  [WITHDRAWN]" if _is_tombstone(row) else ""
    return f"{row['name']}: {row['host']}:{row['a2a_port']}{mark}"


def _verify() -> int:
    """Count what the PRODUCTION reader can see, tombstones included."""
    return len(list_comms_nodes(include_ended=True))


def main(argv: list[str] | None = None) -> int:
    return run_migration(
        argv=argv,
        description=__doc__,
        source=SOURCE,
        store_name=COMMS_NODES_STORE,
        # ``new_...``, not the shared ``open_...``: the library closes the
        # handle it is given, and closing the process-wide one would leave the
        # verification read below holding a dead connection.
        open_store=new_comms_nodes_store,
        to_record=_record,
        key_of=_key,
        verify=_verify,
        describe_row=_describe,
        should_hide=_is_tombstone,
        collides_with=_collides_with,
        actor=ACTOR,
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
