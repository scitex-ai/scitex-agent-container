"""Schema + opener for the ``instances`` store (PostgreSQL).

Split from :mod:`.state_db_instances` for the per-file line cap, along
the same seam the SQLite version used: :mod:`.state_db_schema` held the
table definition and :mod:`.state_db_instances` held the verbs. This
module is the definition; that one is the verbs.

FOUR SCHEMA DECISIONS THE EASY VERSION WOULD HAVE GOT WRONG
===========================================================

1. MULTI_WRITER, and it is not a formality. An instances record has no
   single stable owner by construction: the cross-host dispatcher writes
   a lead-side ``remote=1`` record on the LEAD for an agent that runs on
   a PEER, ``sac agents forget`` retires a record from whichever host the
   operator happens to be on, and ``import_legacy_registry`` bulk-writes
   records for a host that is not this one. Under SINGLE_WRITER the first
   stop-from-elsewhere would be an illegal write.

2. ``ended_at`` / ``exit_reason`` ARE IMMUTABLE, AND ARE NEVER WRITTEN AT
   START. Immutability begins once a field HAS a value, so stamping
   ``ended_at=None`` on the insert would freeze the field at ``None`` and
   every later stop would be rejected as a contradiction. The start
   payload therefore OMITS them entirely. The pay-off is that a recorded
   death cannot be rewritten: two reapers racing on one record keep the
   first verdict, and the second stays in the oplog as evidence instead
   of overwriting history.

3. THE ROLLING COUNTERS ARE LAST_WRITER_WINS, NOT IMMUTABLE AND NOT MAX.
   ``last_heartbeat_at`` / ``iter_count`` / ``input_tokens`` /
   ``output_tokens`` were a deliberate denormalisation so ``sac agent
   status`` could answer "still working?" without a JOIN, and they were
   written ``COALESCE(?, col)`` — overwrite when a value is given, leave
   alone when it is NULL. ``Store.put`` is a PARTIAL update, so "leave
   alone" is spelled by omitting the field and "overwrite" is
   LAST_WRITER_WINS. MAX was considered and rejected: it looks safer for
   a counter and would silently freeze the column the first time a runner
   reported a lower number, turning a visible regression into an
   invisible one.

4. THE ORDER IS ``started_at``, AND HERE THAT IS NOT THE ``rowid`` TRAP.
   ``comms_grants`` had to swap ``ORDER BY rowid`` for the HLC because
   rowid MEANT insertion order and ``created_at`` only approximated it.
   These queries never ordered by rowid: they say ``ORDER BY started_at
   DESC``, and they ask a domain question — *when did this agent start* —
   whose answer is that column. Ordering by the HLC instead would answer
   *when did THIS host learn of the record*, which for a bulk-imported
   peer record is the moment of the import, and would rank a
   just-imported ancient start above a live local one. ``id`` (uuid7,
   time-ordered, minted at the origin) is the total-order tiebreak the
   second-resolution ``started_at`` needs.

   The append-only :mod:`.state_db_instance_events` log is the opposite
   case and gets the opposite treatment: it IS asking in-what-order-did-
   this-happen, so it orders by the HLC.
"""

from __future__ import annotations

import socket
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from scitex_dev.store import Store

#: Logical store name. Renders as four physical tables
#: (``instances_rows``, ``_oplog``, ``_identity``, ``_cursor``).
INSTANCES_STORE = "instances"

_ACTOR = "scitex-agent-container"

#: The SQLite ``SELECT *`` shape, preserved verbatim so every consumer
#: that reads ``row["exit_reason"]`` (rather than ``.get``) keeps working.
#: The values are the old column DEFAULTs — a field the store has never
#: been given is simply absent from ``row.values``, and no caller should
#: have to tell "never written" from "column not selected".
INSTANCE_DEFAULTS: dict[str, Any] = {
    "id": None,
    "definition_id": None,
    "name": None,
    "host": None,
    "scope": None,
    "pid": None,
    "ppid": None,
    "screen": None,
    "workdir": None,
    "a2a_port": None,
    "started_at": None,
    "last_heartbeat_at": None,
    "ended_at": None,
    "exit_reason": None,
    "iter_count": 0,
    "input_tokens": 0,
    "output_tokens": 0,
    "bound_port": None,
    "remote": 0,
    "spawned_by": None,
}


def _ident(kind: Any) -> Any:
    from scitex_dev.store import FieldPolicy, FieldRole, MergeRule

    return FieldPolicy(
        kind=kind,
        role=FieldRole.IDENTITY,
        required=True,
        merge=MergeRule.IMMUTABLE,
        indexed=False,
    )


def _fact(kind: Any, *, required: bool = False) -> Any:
    """A recorded lifecycle fact — IMMUTABLE.

    Where the agent started, under whose lineage, on which port, and when
    it ended. A merge that could move any of these would rewrite the
    placement history the reconciler and the family-tree DAG read.
    """
    from scitex_dev.store import FieldPolicy, FieldRole, MergeRule

    return FieldPolicy(
        kind=kind,
        role=FieldRole.DATA,
        required=required,
        merge=MergeRule.IMMUTABLE,
        indexed=False,
    )


def _rolling(kind: Any) -> Any:
    """A denormalised rolling counter — LAST_WRITER_WINS.

    The ``COALESCE(?, col)`` columns of the SQLite table. See decision 3
    in the module docstring for why not MAX.
    """
    from scitex_dev.store import FieldPolicy, FieldRole, MergeRule

    return FieldPolicy(
        kind=kind,
        role=FieldRole.DATA,
        required=False,
        merge=MergeRule.LAST_WRITER_WINS,
        indexed=False,
    )


def instances_schema() -> Any:
    from scitex_dev.store import FieldKind, Schema

    return Schema(
        name=INSTANCES_STORE,
        fields={
            # The uuid7 instance id IS the identity, exactly as the SQLite
            # PRIMARY KEY treated it. Globally unique by construction, so
            # two hosts cannot mint the same record.
            "id": _ident(FieldKind.TEXT),
            "definition_id": _fact(FieldKind.TEXT),
            "name": _fact(FieldKind.TEXT, required=True),
            "host": _fact(FieldKind.TEXT, required=True),
            "scope": _fact(FieldKind.TEXT, required=True),
            "pid": _fact(FieldKind.INTEGER),
            "ppid": _fact(FieldKind.INTEGER),
            "screen": _fact(FieldKind.TEXT),
            "workdir": _fact(FieldKind.TEXT),
            "a2a_port": _fact(FieldKind.INTEGER),
            "started_at": _fact(FieldKind.TEXT, required=True),
            "ended_at": _fact(FieldKind.TEXT),
            "exit_reason": _fact(FieldKind.TEXT),
            "bound_port": _fact(FieldKind.INTEGER),
            # 0/1 rather than BOOL: consumers and their tests read this as
            # the integer SQLite handed them (``row["remote"] == 1``), and
            # quietly changing a field's wire type is how a breaking change
            # rides along inside a migration.
            "remote": _fact(FieldKind.INTEGER),
            "spawned_by": _fact(FieldKind.TEXT),
            "last_heartbeat_at": _rolling(FieldKind.TEXT),
            "iter_count": _rolling(FieldKind.INTEGER),
            "input_tokens": _rolling(FieldKind.INTEGER),
            "output_tokens": _rolling(FieldKind.INTEGER),
        },
    )


def open_instances_store() -> "Store":
    """Open the instances store. RAISES if PostgreSQL is unreachable.

    MULTI_WRITER — see decision 1 in the module docstring.
    """
    from scitex_dev.store import Store, WriterPolicy, host_store

    schema = instances_schema()
    return Store(
        host_store(pkg="scitex_agent_container", name=schema.name),
        schema,
        node=socket.gethostname(),
        writer_policy=WriterPolicy.MULTI_WRITER,
        actor=_ACTOR,
    )


def instance_dict(row: Any) -> dict:
    """One store row as the ``SELECT *`` dict every caller already reads."""
    out = dict(INSTANCE_DEFAULTS)
    for key, value in row.values.items():
        if value is not None:
            out[key] = value
    return out


def start_order(data: dict) -> tuple:
    """``ORDER BY started_at DESC, id DESC``, as an ASCENDING sort key.

    See decision 4 in the module docstring for why this is ``started_at``
    and not the HLC.
    """
    return (str(data.get("started_at") or ""), str(data.get("id") or ""))


__all__ = [
    "INSTANCES_STORE",
    "INSTANCE_DEFAULTS",
    "instance_dict",
    "instances_schema",
    "open_instances_store",
    "start_order",
]
