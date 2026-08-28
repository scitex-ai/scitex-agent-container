"""The agent-lifecycle event log — on PostgreSQL only.

This was the SQLite ``events`` table. It moved on 2026-08-28 together
with ``instances``, its only writer, under the operator's
SQLite-eradication order.

RENAMED ON THE WAY ACROSS, to ``instance_events``. The store renders a
logical store into physical tables named ``<name>_rows`` / ``_oplog`` /
``_identity`` / ``_cursor`` inside ONE shared PostgreSQL schema, and a
table called ``events_rows`` in a fleet-wide namespace is a name nobody
can attribute to a package, let alone to a table — ``channel_events``
already lives one module over, and ``scitex-cards`` has events of its
own. A SQLite file gave the name a container; a shared schema does not.

WHY THE AUTOINCREMENT ``id`` DID NOT COME ALONG
==============================================
It could not. ``Store.next_seq()`` counts ops per NODE, so two hosts
appending their first event would both mint ``1`` and their records would
collide on identity — two unrelated events merged into one. Measured
against the requirement, not assumed: nothing in this repo ever read
``events.id``. The identity is the natural triple ``(instance_id, kind,
ts)`` instead, which is globally unique because ``instance_id`` is a
uuid7 minted at the origin, and which makes a replayed append idempotent
rather than duplicated.

ORDERING IS THE HLC, and here that is the RIGHT successor — unlike the
``instances`` records next door, which order by ``started_at`` because
they are asked a domain question about start time. An append-only log is
asked *in what order did these happen*, and the hybrid logical clock is
built for exactly that: monotonic per origin, causally ordered across
origins, immune to the wall-clock skew that a ``ts`` column carries
between hosts.
"""

from __future__ import annotations

import socket
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from scitex_dev.store import Store

#: Logical store name. Renders as four physical tables
#: (``instance_events_rows``, ``_oplog``, ``_identity``, ``_cursor``).
EVENTS_STORE = "instance_events"

_ACTOR = "scitex-agent-container"


def _ident(kind: Any) -> Any:
    from scitex_dev.store import FieldPolicy, FieldRole, MergeRule

    return FieldPolicy(
        kind=kind,
        role=FieldRole.IDENTITY,
        required=True,
        merge=MergeRule.IMMUTABLE,
        indexed=False,
    )


def _fact(kind: Any) -> Any:
    """A logged event is a historical fact — IMMUTABLE.

    A merge that could move an actor or a payload would rewrite the
    lifecycle timeline an operator reads to reconstruct what happened.
    """
    from scitex_dev.store import FieldPolicy, FieldRole, MergeRule

    return FieldPolicy(
        kind=kind,
        role=FieldRole.DATA,
        required=False,
        merge=MergeRule.IMMUTABLE,
        indexed=False,
    )


def _events_schema() -> Any:
    from scitex_dev.store import FieldKind, Schema

    return Schema(
        name=EVENTS_STORE,
        fields={
            "instance_id": _ident(FieldKind.TEXT),
            "kind": _ident(FieldKind.TEXT),
            "ts": _ident(FieldKind.TEXT),
            "definition_id": _fact(FieldKind.TEXT),
            "actor": _fact(FieldKind.TEXT),
            "payload_json": _fact(FieldKind.TEXT),
        },
    )


def _open() -> "Store":
    """Open the event store. RAISES if PostgreSQL is unreachable.

    MULTI_WRITER, for the same reason the ``instances`` store is: a stop
    event is appended by whichever host issued the stop, which is not
    necessarily the host that appended the start.
    """
    from scitex_dev.store import Store, WriterPolicy, host_store

    schema = _events_schema()
    return Store(
        host_store(pkg="scitex_agent_container", name=schema.name),
        schema,
        node=socket.gethostname(),
        writer_policy=WriterPolicy.MULTI_WRITER,
        actor=_ACTOR,
    )


def _hlc_sort_key(row: Any) -> tuple:
    """Causal order over appended events, immune to wall-clock skew.

    ``node`` is the final tiebreak so the order is total rather than
    merely partial — two origins can mint the same (wall_us, logical).
    """
    hlc = row.hlc
    return (hlc.wall_us, hlc.logical, hlc.node)


def append_instance_event(
    *,
    instance_id: str,
    kind: str,
    ts: str,
    actor: str = "sac",
    definition_id: str | None = None,
    payload_json: str | None = None,
) -> None:
    """Append one lifecycle event.

    A duplicate (same instance, same kind, same second) is the outcome we
    wanted — the SQLite writers were already replay-safe — so
    ``RevisionMismatchError`` returns rather than raises. The catch is
    deliberately NARROW: an unreachable store still fails loudly, which
    is the whole point of moving off SQLite.
    """
    from scitex_dev.store import NEW_RECORD, RevisionMismatchError

    store = _open()
    try:
        store.put(
            {
                "instance_id": instance_id,
                "kind": kind,
                "ts": ts,
                "actor": actor,
                "definition_id": definition_id,
                "payload_json": payload_json,
            },
            expected_revision=NEW_RECORD,
        )
    except RevisionMismatchError:
        return
    finally:
        store.close()


def instance_events(instance_id: str) -> list[dict]:
    """The lifecycle event log for one instance, oldest first."""
    store = _open()
    try:
        rows = [r for r in store.rows() if r.values.get("instance_id") == instance_id]
    finally:
        store.close()
    rows.sort(key=_hlc_sort_key)
    return [dict(r.values) for r in rows]


__all__ = [
    "EVENTS_STORE",
    "append_instance_event",
    "instance_events",
]
