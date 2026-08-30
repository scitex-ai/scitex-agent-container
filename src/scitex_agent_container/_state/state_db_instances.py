#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# File: src/scitex_agent_container/_state/state_db_instances.py
"""``instances`` lifecycle CRUD — ON POSTGRESQL SINCE 2026-08-28.

The four public names (:func:`record_instance_start`,
:func:`record_instance_stop`, :func:`list_active_instances`,
:func:`last_known_instance`) are unchanged and still re-exported from
:mod:`.state_db`, so every ``from ...state_db import record_instance_start``
keeps resolving. What changed is underneath: the rows live in the shared
PostgreSQL store (:mod:`.state_db_instances_store`), not in a per-host
file, and ``db_path`` is GONE from every signature because there is
no file to point at.

The agent-rename half lives in :mod:`.state_db_instances_rename` — the
per-file line cap, and the same split ``comms_nodes`` uses.

WHY THE STORE MAKES THESE FUNCTIONS LOOK DIFFERENT
===================================================
``Store`` has no UPDATE-only verb and no WHERE clause. Three consequences
run through everything below, and each one is written out rather than
relied upon, because the guard moved OUT of the database and INTO this file:

1. **A stop reads first and refuses explicitly.** The tombstone was
   ``UPDATE ... WHERE id=? AND ended_at IS NULL``, and the ``WHERE`` did the
   refusing: a missing row and an already-ended row both came back as
   ``rowcount == 0``. ``put`` refuses nothing, so :func:`end_instance`
   performs that check itself and NEVER inserts. Same invariant, same shape,
   as :func:`.state_db_incarnations.record_incarnation_exit`.

2. **Every write is a PARTIAL put, and every payload is stripped of
   ``None``.** ``Store.put`` leaves absent fields alone, and a field written
   as ``None`` is a WRITE — it stamps the field. For the IMMUTABLE fields
   that is irreversible: an insert carrying ``ended_at=None`` would freeze
   the field at None and make the GC's later tombstone a silently rejected
   ``MergeConflict``. See :func:`.state_db_instances_store.strip_unset`,
   where that hazard is measured and documented.

3. **A read by ``id`` alone is a scan.** The record identity is ``{id,
   host}`` (PER_HOST truth: two hosts describing one agent are describing
   two processes), and :func:`record_instance_stop` is handed only an id —
   as its predecessor was. It therefore locates the record by scanning,
   which costs one query on a path that runs once per stop. The alternative
   would be to make every caller thread a host it does not have.

ORDERING IS NOW THIS MODULE'S JOB, AND THE ``id`` TIEBREAK IS LOAD-BEARING
==========================================================================
``resolve_node_host`` and ``resolve_forward_target`` both read
``ORDER BY started_at DESC, id DESC LIMIT 1``. ``started_at`` is
second-resolution, so two starts inside one second are a genuine tie and the
tiebreak decides which agent a message reaches. uuid7 ids are time-ordered,
which is what makes ``id DESC`` the CORRECT tiebreak rather than an
arbitrary one. :func:`.state_db_instances_store.sortable_recency` is the one
place that key is spelled.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .state_db_hostname import resolve_host as _resolve_host
from .state_db_instances_store import (
    ACTOR,
    instance_as_dict,
    instance_key,
    run_with_reconnect,
    sortable_recency,
    strip_unset,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from scitex_dev.store import Row, Store

__all__ = [
    "end_instance",
    "last_known_instance",
    "list_active_instances",
    "live_instance_for_name",
    "read_instance",
    "record_instance_start",
    "record_instance_stop",
    "scan_instances",
]


def scan_instances(store: "Store") -> list["Row"]:
    """Every record. The store offers no WHERE, so filtering is ours."""
    return store.rows()


def find_by_id(store: "Store", instance_id: str) -> "Row | None":
    """The record with this uuid7 ``id``, whatever host observed it.

    A scan, because ``id`` is only HALF the identity. uuid7 ids are globally
    unique, so at most one record can match and the scan cannot be ambiguous
    — which is exactly what ``WHERE id=?`` relied on.
    """
    if not instance_id:
        return None
    for row in scan_instances(store):
        if str(row.values.get("id")) == instance_id:
            return row
    return None


def record_instance_start(
    name: str,
    *,
    pid: int | None = None,
    screen: str | None = None,
    workdir: str | None = None,
    a2a_port: int | None = None,
    host: str | None = None,
    bound_port: int | None = None,
    remote: bool = False,
    spawned_by: str | None = None,
) -> str:
    """Insert an ``instances`` record for a freshly-started agent.

    Returns the new ``instance_id`` (uuid7). ``expected_revision=NEW_RECORD``
    makes a re-used id a loud ``RevisionMismatchError`` rather than a silent
    overwrite of somebody else's lifetime.

    ``bound_port`` is FOLDED into ``a2a_port`` rather than stored beside it:
    both columns always carried one value, and keeping two copies is how the
    routing readers came to disagree about which was authoritative (see
    :mod:`.state_db_forward`). Callers may keep passing either or both;
    ``a2a_port`` wins when they differ, matching
    ``COALESCE(a2a_port, bound_port)``.

    THREE PARAMETERS ARE GONE — ``scope``, ``definition_id`` and ``ppid``.
    No call site in ``src/`` ever passed any of them, so every row ever
    written carried ``scope='global'`` and two NULLs, and the store does not
    declare them. Passing one now is a ``TypeError``, which is the point: a
    silently ignored keyword is how a caller comes to believe it recorded
    something.
    """
    from scitex_dev.store import NEW_RECORD

    from .state_db import new_uuid7, now_iso

    instance_id = new_uuid7()
    canonical_host = _resolve_host(host)
    started_at = now_iso()
    port = a2a_port if a2a_port is not None else bound_port

    values = strip_unset(
        {
            "name": name,
            "pid": pid,
            "a2a_port": port,
            "screen": screen,
            "workdir": workdir,
            "spawned_by": spawned_by,
            "started_at": started_at,
        }
    )
    # ``remote`` is always written, including False: it is the authoritative
    # locality flag half the fleet branches on, and an absent field would
    # read as "unknown" the first time anything learns to tell those apart.
    values["remote"] = bool(remote)
    values["id"] = instance_id
    values["host"] = canonical_host

    run_with_reconnect(
        lambda store: store.put(values, expected_revision=NEW_RECORD, actor=ACTOR)
    )
    return instance_id


def end_instance(instance_id: str, *, exit_reason: str, ended_at: str) -> bool:
    """Fill ``ended_at``/``exit_reason`` ONCE. ``True`` iff this call did it.

    The single tombstone writer — :func:`record_instance_stop` and the GC
    sweep both go through here, so "a process ends once" is enforced in one
    place rather than in each of the four call sites that used to spell the
    same ``UPDATE ... WHERE ended_at IS NULL``.

    ``False`` means one of three things, and none of them is an error: the id
    is unknown, the record is already ended, or a concurrent writer won the
    race. The last case is a ``RevisionMismatchError`` from the optimistic
    lock, and it is deliberately NOT retried into a second tombstone — the
    record is ended, which is the goal, and a second, different ``ended_at``
    would be a rejected ``MergeConflict`` anyway (the field is IMMUTABLE).

    A missing record is a ``False``, NEVER an insert. A death with no
    recorded birth is a real signal — a row swept on another host, or a
    start write that failed — and fabricating a start here would hide it.
    """
    from scitex_dev.store import RevisionMismatchError

    def _end(store: "Store") -> bool:
        row = find_by_id(store, instance_id)
        if row is None:
            return False
        if row.values.get("ended_at") is not None:
            return False
        key = instance_key(row.values)
        revision = store.revision(key)
        try:
            store.put(
                {**key, "ended_at": ended_at, "exit_reason": exit_reason},
                expected_revision=revision,
                actor=ACTOR,
            )
        except RevisionMismatchError:
            return False
        return True

    return bool(run_with_reconnect(_end))


def record_instance_stop(instance_id: str, *, exit_reason: str = "stopped") -> bool:
    """Mark an instance as ended. Returns True iff THIS call ended it.

    Idempotent: stopping an already-stopped record is a no-op returning
    ``False``, exactly as a zero rowcount meant. Stopping an
    id the store has never seen is the same ``False`` and writes nothing.

    It appended a ``kind='stop'`` row to ``events`` until 2026-08-28. That
    table went the same day for having no reader at all, and every
    fact the row carried — the stop timestamp and ``exit_reason`` — is
    written to the record here.
    """
    from .state_db import now_iso

    return end_instance(instance_id, exit_reason=exit_reason, ended_at=now_iso())


# ``record_instance_activity`` lived here between the store port and the
# merge of the three-dead-tables change, as the successor to
# ``update_heartbeat``'s ``UPDATE instances SET last_heartbeat_at = ?,
# iter_count = COALESCE(...)`` half. That function is GONE — its table
# ``instance_heartbeats`` had no caller in ``src/`` and 0 rows on every
# host — so the successor would have been a public writer nobody calls,
# which is precisely what this package deletes rather than keeps.
#
# THE FIELDS STAY DECLARED, and that is not a contradiction.
# ``last_heartbeat_at`` / ``iter_count`` / ``input_tokens`` /
# ``output_tokens`` hold REAL VALUES on rows migrated out of the old store, and
# ``state_db_gc``'s staleness branch reads the first of them. With no
# writer that branch can only ever fire on migrated history — a
# pre-existing fact the ``instance_heartbeats`` departure note records, and
# one this move makes visible rather than causes. Repairing it means
# deciding who beats, which is not a storage migration.


def read_instance(instance_id: str) -> dict | None:
    """One record by uuid7 ``id``, active OR ended, or ``None`` if unknown."""

    def _read(store: "Store") -> dict | None:
        row = find_by_id(store, instance_id)
        return None if row is None else instance_as_dict(row)

    return run_with_reconnect(_read)


def list_active_instances(host: str | None = None) -> list[dict]:
    """Every record with no ``ended_at``, optionally host-filtered.

    Ordered ``(started_at, id)`` DESC — newest first, as the original
    ``ORDER BY started_at DESC`` was, plus the ``id`` tiebreak the resolvers
    depend on and that ``started_at``'s one-second resolution cannot supply
    on its own.

    An UNREACHABLE store RAISES here; it does not answer ``[]``. Every caller
    reads empty as "nothing is running", so a fallback would turn a database
    outage into a fleet-wide false negative on the very path that decides
    whether to start a SECOND copy of a running agent.
    """

    def _list(store: "Store") -> list[dict]:
        out = []
        for row in scan_instances(store):
            values = row.values
            if values.get("ended_at") is not None:
                continue
            if host is not None and str(values.get("host")) != host:
                continue
            out.append(instance_as_dict(row))
        return out

    return sorted(run_with_reconnect(_list), key=sortable_recency, reverse=True)


def live_instance_for_name(name: str) -> dict | None:
    """The newest LIVE record for ``name``, or ``None``.

    The shared reader behind :func:`.state_db_nodes.resolve_node_host` and
    :func:`.state_db_forward.resolve_forward_target`, which spelled the same
    ``WHERE name = ? AND ended_at IS NULL ORDER BY started_at DESC, id DESC
    LIMIT 1`` twice. They ask DIFFERENT questions of the answer — one wants
    locality, the other an address — and that difference is theirs to keep;
    the record they ask it of must be the same one, and now provably is.
    """
    if not name:
        return None
    for row in list_active_instances():
        if row.get("name") == name:
            return row
    return None


def last_known_instance(name: str) -> dict | None:
    """The most-recent record for ``name``, active OR ENDED.

    Unlike :func:`list_active_instances` this does NOT filter ``ended_at``,
    and that is a contract other code depends on rather than a convenience.
    ``cli_pkg/lifecycle/_restart_verify`` reads an ended record for the tmux
    session name, ``_reconcile/_rule`` reads its ``exit_reason`` to decide
    whether a restart is warranted, and ``_network/_peer_faillloud`` reads it
    to NAME the last known placement in the #192 fail-loud message. ``None``
    therefore means the agent name has never been observed at all — the one
    answer those three readers must not be handed by mistake.
    """
    if not name:
        return None

    def _list(store: "Store") -> list[dict]:
        return [
            instance_as_dict(row)
            for row in scan_instances(store)
            if row.values.get("name") == name
        ]

    rows = run_with_reconnect(_list)
    return max(rows, key=sortable_recency) if rows else None

# EOF
