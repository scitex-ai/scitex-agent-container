#!/usr/bin/env python3
"""One-shot: copy the SQLite ``instances`` table into PostgreSQL.

Companion to the ``state_db_instances`` port (2026-08-28). The code stopped
reading SQLite; this moves the rows already there so a host's whole agent
lifecycle history is not stranded in a file nothing opens any more.

EVERY ROW MOVES, ENDED ONES INCLUDED
====================================
It is tempting to carry only the live rows — they are the ones
``list_active_instances`` returns and the ones an operator sees. That would
be wrong, and three readers say so:

* ``last_known_instance`` exists PRECISELY to answer from an ended row: the
  #192 fail-loud message names the last known placement of an agent that is
  not running now.
* ``cli_pkg/lifecycle/_restart_verify`` reads an ended row for the tmux
  session name it pairs with ``#{session_created}``. Without the ended row
  it abstains on every restart it is asked to verify.
* ``_reconcile/_rule`` reads ``exit_reason`` off an ended row to decide
  whether a managed agent should be restarted. A missing row is
  ``NEVER_STARTED``, which is a DIFFERENT verdict — so dropping history
  would not merely lose detail, it would change decisions.

So the source query is unfiltered and ``ended_at`` / ``exit_reason`` /
``started_at`` / ``last_heartbeat_at`` are carried VERBATIM. Never
re-stamped: ``started_at`` and ``ended_at`` are IMMUTABLE in the store, and
stamping ``now()`` would both erase the evidence and make the first real
contradiction unreportable.

WHAT DOES NOT MOVE, AND WHY EACH IS A REAL (ACCEPTED) LOSS
==========================================================
``definition_id`` / ``scope`` / ``ppid``
    Dropped from the schema. No reader in ``src/`` ever touched them and no
    writer ever set the first or the last; ``scope`` was the literal
    ``'global'`` on every row. See ``_store_plugin.INSTANCES``.
``bound_port``
    FOLDED, not dropped: ``COALESCE(a2a_port, bound_port)`` becomes the
    store's single ``a2a_port``. Both columns always carried one value
    written twice, and the split is what let two routing readers give
    different answers about the same row. The production reader mirrors the
    value back out under both KEYS, so no caller changes shape.

IDENTITY IS ``{id, host}``, VERBATIM, AND CROSS-HOST DUPLICATES ARE REAL
========================================================================
``id`` is a uuid7 minted at the origin, so two hosts cannot collide. What
looks like a duplicate is not one: when the lead dispatches an agent to a
peer it writes its OWN row with ``remote=1`` and the peer's hostname, while
the peer writes its own local row for the same agent. Those are two
observations by two hosts and PER_HOST truth keeps them as two records —
which is exactly why ``host`` is in the identity. This script must not
"deduplicate" them and does not try.

THAT IS ALSO WHY THERE IS NO COLLISION CHECK
--------------------------------------------
``migrate_comms_nodes_to_postgres`` refuses to pick a winner when two hosts
disagree about one NAME, because that table's identity is the name and the
files were allowed to diverge. Here the identity already carries the host,
so two hosts' rows are different records and there is nothing to disagree
about. A re-run finds its own rows already present and leaves them alone;
that is the ordinary path, not a conflict.

VERIFICATION ASKS ABOUT THIS HOST, NOT ABOUT THE STORE
=======================================================
``_migrate_lib``'s default verify compares the source row count against the
store's TOTAL, which cannot hold for a SHARED store: once compute-04 has
migrated, spartan's run reads (say) 200 local rows and sees 800 in the
store. That comparison would pass by accident here and would fail for the
FIRST host if any peer had gone first with fewer rows — a check whose
verdict depends on run order is not a check.

So :func:`_verify` counts the records this host's file was responsible for:
it re-reads the source ids through the PRODUCTION reader and reports how
many are visible. Reading back through the production path — rather than
trusting the write count — is the part of the library's discipline that
matters most and is kept: a put that silently no-opped would otherwise
report as success.

Nothing is deleted from SQLite; the old table stays as a fallback.

A DRY RUN IS THE DEFAULT and it must be RUN ON THE HOST — ``_migrate_lib``
owns both rules and the ``$HOME`` trap behind the second one.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Mapping

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
# ...and this script's OWN directory, which a direct ``python scripts/x.py``
# supplies for free and an ``importlib.util.spec_from_file_location`` load
# does not. The develop-tier test that pins "a bare invocation writes
# nothing" loads these scripts the second way, so without this line the
# guard cannot even import the thing it is guarding.
sys.path.insert(0, str(Path(__file__).resolve().parent))

from _migrate_lib import SqliteSource, run_migration  # noqa: E402

from scitex_agent_container._state.state_db_instances import (  # noqa: E402
    scan_instances,
)
from scitex_agent_container._state.state_db_instances_store import (  # noqa: E402
    ACTOR,
    INSTANCES_STORE,
    new_instances_store,
    run_with_reconnect,
)

SOURCE = SqliteSource(
    table="instances",
    columns=(
        "id",
        "name",
        "host",
        "pid",
        "screen",
        "workdir",
        "a2a_port",
        "bound_port",
        "remote",
        "spawned_by",
        "started_at",
        "last_heartbeat_at",
        "iter_count",
        "input_tokens",
        "output_tokens",
        "ended_at",
        "exit_reason",
    ),
    # ``started_at`` rather than rowid: it is the order an operator reads a
    # lifecycle table in, and it makes the dry-run listing stable across a
    # re-run even if rows were rewritten in place. The ``id`` tiebreak is the
    # one the production readers use.
    order_by="started_at ASC, id ASC",
)

#: Source ids seen by the last :func:`_record` pass, so :func:`_verify` can
#: ask about THIS host's rows rather than about the whole shared store. A
#: module global rather than a parameter because ``run_migration`` owns the
#: call sequence and hands ``verify`` nothing.
_SEEN_IDS: list[str] = []


def _record(row: Mapping[str, Any]) -> dict[str, Any]:
    """One SQLite row as the store's record.

    ``None`` values are DROPPED rather than written. That is not tidiness:
    the store freezes an IMMUTABLE field at its first stamped value, and
    writing ``None`` counts as a stamp — so a migrated live row that carried
    ``ended_at=None`` explicitly could never be ended again, and nothing
    would raise (the rejection comes back in ``PutResult.conflicts``, which
    no caller reads).
    """
    identifier = str(row["id"])
    if identifier not in _SEEN_IDS:
        _SEEN_IDS.append(identifier)
    port = row.get("a2a_port")
    if port is None:
        port = row.get("bound_port")
    values: dict[str, Any] = {
        "id": identifier,
        "host": str(row["host"]),
        "name": row.get("name"),
        "pid": row.get("pid"),
        "a2a_port": port,
        "screen": row.get("screen"),
        "workdir": row.get("workdir"),
        # ``remote`` is always written, including False. It is the
        # authoritative locality flag the GC and five readers branch on, and
        # SQLite's DEFAULT 0 means an absent value there meant "local" —
        # carrying that as an unwritten field would turn a known False into
        # an unknown.
        "remote": bool(row.get("remote")),
        "spawned_by": row.get("spawned_by"),
        "started_at": row.get("started_at"),
        "last_heartbeat_at": row.get("last_heartbeat_at"),
        "iter_count": row.get("iter_count"),
        "input_tokens": row.get("input_tokens"),
        "output_tokens": row.get("output_tokens"),
        "ended_at": row.get("ended_at"),
        "exit_reason": row.get("exit_reason"),
    }
    return {
        key: value
        for key, value in values.items()
        if value is not None or key == "remote"
    }


def _key(record: Mapping[str, Any]) -> dict[str, Any]:
    return {"id": record["id"], "host": record["host"]}


def _describe(row: Mapping[str, Any]) -> str:
    state = "ENDED" if row.get("ended_at") else "live "
    port = row.get("a2a_port") or row.get("bound_port") or "-"
    return (
        f"{state} {row.get('started_at')}  {row.get('name')}@{row.get('host')}"
        f":{port}  id={row.get('id')}"
    )


def _verify() -> int:
    """How many of THIS host's source rows the production reader can see.

    Deliberately NOT ``len(all records in the store)`` — see the module
    docstring for why a shared store makes that comparison order-dependent.

    ``scan_instances`` on the shared handle is the SAME read
    ``list_active_instances`` and ``last_known_instance`` perform (they add a
    filter to it and nothing else), so a record the store accepted but cannot
    serve counts as absent here — which is the whole reason verification
    reads back rather than trusting the write count. One scan rather than one
    point-read per row: a point-read is itself a scan (the identity is
    ``{id, host}`` and only the id is known), so N of them would be N² on a
    table whose whole point is that it is the biggest one.
    """
    present = {
        str(row.values.get("id")) for row in run_with_reconnect(scan_instances)
    }
    return sum(1 for identifier in _SEEN_IDS if identifier in present)


def main(argv: list[str] | None = None) -> int:
    return run_migration(
        argv=argv,
        description=__doc__,
        source=SOURCE,
        store_name=INSTANCES_STORE,
        # ``new_...``, not the shared ``open_...``: the library closes the
        # handle it is given, and closing the process-wide one would leave
        # the verification read below holding a dead connection.
        open_store=new_instances_store,
        to_record=_record,
        key_of=_key,
        verify=_verify,
        describe_row=_describe,
        # No ``should_hide``: an ``ended_at`` row is a TOMBSTONE, not a
        # withdrawal. ``last_known_instance`` must keep returning it (see the
        # module docstring), and ``hide()`` would make it read as absent to
        # every production reader — which is the one answer three of them
        # must never be handed by mistake.
        actor=ACTOR,
    )


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
