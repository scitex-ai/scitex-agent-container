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

IT IS IDEMPOTENT. Entries are written with ``NEW_RECORD``, so a name already
in the store is LEFT ALONE — a live agent may have re-registered since the
first pass, and that entry is newer than the file's. Nothing is deleted from
SQLite; the old table stays as a fallback.

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
        actor=ACTOR,
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
