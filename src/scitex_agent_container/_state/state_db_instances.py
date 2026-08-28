"""``instances`` lifecycle CRUD — on PostgreSQL only.

Moved off SQLite 2026-08-28, under the operator's SQLite-eradication
order and for the reason he gave: the fleet is MULTI-HOST, and a SQLite
file per host means a different truth per host. For THIS table that is
not an abstract risk. ``instances`` is where the fleet records *which
agent is running where*, and ``ended_at IS NULL`` is the liveness
predicate half a dozen other modules ask — the routers, the forwarder,
``sac agents list``, the reconciler, the GC sweep. A per-host copy of
that answer is a fleet that disagrees with itself about who is alive.

The schema, the merge policy and the opener live in
:mod:`.state_db_instances_store`; this module is the verbs. The
companion ``events`` table moved in the same pass and lives in
:mod:`.state_db_instance_events` (renamed ``instance_events`` — see that
module for why the name could not survive the crossing).

The store resolves through ``scitex_dev.store.host_store``:
``SCITEX_STORE_DSN`` or the per-host PostgreSQL, with NO SQLite
fallback, so a host whose PostgreSQL is unreachable raises
``StoreTargetError`` naming the DSN it could not reach.

``db_path`` IS GONE from every signature. It named a SQLite file; there
is no file. Callers that threaded it through simply stop — including
``_lifecycle/_instances.record_local_instance``, which keeps its own
``db_path`` parameter only because ``port_allocator`` has not moved yet.

NOTHING HERE DELETES, AND NOTHING HERE HIDES. The SQLite version never
deleted either — retiring an instance was always ``UPDATE ... SET
ended_at``, a tombstone — and that is preserved exactly. ``Store.hide``
is deliberately NOT used for the end of a lifecycle: a stopped agent is
not a hidden record, it is a record carrying a death stamp, and the
family tree, ``sac agents recall`` and the #192 fail-loud resolver all
keep reading it by default. Hiding it would make "never started here"
and "started and stopped" indistinguishable to every default read —
precisely the audit question this table exists to answer.
"""

from __future__ import annotations

import json
from typing import Any

from .state_db_hostname import resolve_host as _resolve_host
from .state_db_instance_events import append_instance_event
from .state_db_instances_store import (
    INSTANCES_STORE,
    instance_dict,
    open_instances_store,
    start_order,
)


def record_instance_start(
    name: str,
    *,
    pid: int | None = None,
    ppid: int | None = None,
    screen: str | None = None,
    workdir: str | None = None,
    a2a_port: int | None = None,
    scope: str = "global",
    host: str | None = None,
    definition_id: str | None = None,
    bound_port: int | None = None,
    remote: bool = False,
    spawned_by: str | None = None,
) -> str:
    """Write an ``instances`` record for a freshly-started agent.

    Returns the new ``instance_id`` (uuid7). Also appends a
    ``kind='start'`` record to ``instance_events``.

    The family-tree fields make every start — local OR cross-host
    dispatch — record its bound port, host, lineage and locality as an
    intrinsic side-effect (sac-agent-spawn design, Rule B). When
    ``bound_port`` is not given it defaults to ``a2a_port`` so a caller
    that only knows the resolved port still populates both.

    ``ended_at`` / ``exit_reason`` are NOT in the payload, deliberately —
    see decision 2 in :mod:`.state_db_instances_store`.
    """
    from scitex_dev.store import NEW_RECORD

    from .state_db import new_uuid7, now_iso

    instance_id = new_uuid7()
    started_at = now_iso()
    canonical_host = _resolve_host(host)
    if bound_port is None:
        bound_port = a2a_port

    store = open_instances_store()
    try:
        store.put(
            {
                "id": instance_id,
                "definition_id": definition_id,
                "name": name,
                "host": canonical_host,
                "scope": scope,
                "pid": pid,
                "ppid": ppid,
                "screen": screen,
                "workdir": workdir,
                "a2a_port": a2a_port,
                "started_at": started_at,
                "bound_port": bound_port,
                "remote": 1 if remote else 0,
                "spawned_by": spawned_by,
                "iter_count": 0,
                "input_tokens": 0,
                "output_tokens": 0,
            },
            expected_revision=NEW_RECORD,
        )
    finally:
        store.close()

    append_instance_event(instance_id=instance_id, kind="start", ts=started_at)
    return instance_id


def end_instance(instance_id: str, *, ended_at: str, exit_reason: str) -> bool:
    """Stamp a death on a LIVE record. ``True`` iff one was live.

    The primitive behind both :func:`record_instance_stop` and the GC
    sweep, which supplies its own ``ended_at`` (the boot epoch for a
    reboot sweep, the sweep's own clock otherwise) and appends no event.

    Reads the record first rather than leaning on the IMMUTABLE merge to
    reject a second stamp: the merge RECORDS a conflict, it does not
    raise, so a caller watching only for an exception would report a
    re-stop as a fresh one.
    """
    from scitex_dev.store import ANY_REVISION

    store = open_instances_store()
    try:
        row = store.get({"id": instance_id})
        if row is None or row.values.get("ended_at") is not None:
            return False
        store.put(
            {"id": instance_id, "ended_at": ended_at, "exit_reason": exit_reason},
            expected_revision=ANY_REVISION,
        )
        return True
    finally:
        store.close()


def record_instance_stop(instance_id: str, *, exit_reason: str = "stopped") -> bool:
    """Mark an instance as ended. Returns True iff a record was updated.

    Idempotent: stopping an already-stopped record is a no-op, and no
    ``stop`` event is appended for it.
    """
    from .state_db import now_iso

    ended_at = now_iso()
    if not end_instance(instance_id, ended_at=ended_at, exit_reason=exit_reason):
        return False
    append_instance_event(
        instance_id=instance_id,
        kind="stop",
        ts=ended_at,
        payload_json=json.dumps({"exit_reason": exit_reason}),
    )
    return True


def touch_instance_counters(
    instance_id: str,
    *,
    last_heartbeat_at: str,
    iter: int | None = None,
    input_tokens: int | None = None,
    output_tokens: int | None = None,
) -> None:
    """Refresh the denormalised rolling counters on one record.

    The second half of the old ``update_heartbeat`` — its ``UPDATE
    instances SET last_heartbeat_at=?, iter_count=COALESCE(...)``
    statement. A ``None`` argument means "leave alone", which
    ``Store.put`` expresses by omitting the field rather than by writing
    a NULL over the value.

    Silently does nothing when the record is unknown, matching the SQLite
    ``UPDATE ... WHERE id=?`` that affected zero rows.
    """
    from scitex_dev.store import ANY_REVISION

    values: dict[str, Any] = {
        "id": instance_id,
        "last_heartbeat_at": last_heartbeat_at,
    }
    if iter is not None:
        values["iter_count"] = iter
    if input_tokens is not None:
        values["input_tokens"] = input_tokens
    if output_tokens is not None:
        values["output_tokens"] = output_tokens

    store = open_instances_store()
    try:
        if store.get({"id": instance_id}) is None:
            return
        store.put(values, expected_revision=ANY_REVISION)
    finally:
        store.close()


def all_instances(*, active_only: bool = False) -> list[dict]:
    """Every ``instances`` record, newest start first.

    ``active_only`` applies the fleet's liveness predicate — the
    successor to ``ended_at IS NULL``, spelled the same way.
    """
    store = open_instances_store()
    try:
        rows = [instance_dict(r) for r in store.rows()]
    finally:
        store.close()
    if active_only:
        rows = [r for r in rows if r.get("ended_at") is None]
    rows.sort(key=start_order, reverse=True)
    return rows


def list_active_instances(host: str | None = None) -> list[dict]:
    """Return every live record, optionally host-filtered.

    "Live" is ``ended_at is None`` — the same predicate the SQLite
    ``WHERE ended_at IS NULL`` expressed, and the one the routers, the
    forwarder and the reconciler all depend on.
    """
    rows = all_instances(active_only=True)
    if host is None:
        return rows
    return [r for r in rows if r.get("host") == host]


def latest_active_instance(name: str) -> dict | None:
    """The most recently started LIVE record for ``name``, or ``None``.

    The address lookup behind ``resolve_node_host`` and
    ``resolve_forward_target``, which each apply their own port policy to
    the result. Extracted so the two cannot drift: they were one SQL
    statement copied into two modules, and they HAD already drifted once
    — one preferred ``bound_port`` and the other did not, so the same
    record resolved two ways at the same moment.
    """
    for row in list_active_instances():
        if row.get("name") == name:
            return row
    return None


def last_known_instance(name: str) -> dict | None:
    """Return the most-recent record for ``name``, active OR ended.

    Unlike :func:`list_active_instances` (which filters on ``ended_at``),
    this returns the latest record regardless of lifecycle state so a
    fail-loud resolver can report the LAST KNOWN host + ``started_at`` +
    whether the record has ``ended_at`` set. ``None`` only when the agent
    name has never appeared in this fleet's registry.

    This is the evidence behind the #192 fail-loud message: when an agent
    cannot be resolved to a live instance, the resolver must name the last
    known placement rather than silently assume the agent is local.
    """
    for row in all_instances():
        if row.get("name") == name:
            return row
    return None


def put_instance_record(values: dict) -> bool:
    """Write one pre-built record verbatim. ``True`` iff it was created.

    The bulk-import door, used by ``import_legacy_registry`` and by
    ``scripts/migrate_instances_to_postgres.py``. It writes what it is
    handed — no ``now_iso()`` restamping — because a record's
    ``started_at`` is the evidence of when that agent ran, and rewriting
    it to the import moment destroys the only thing the record was kept
    for.

    Keys whose value is ``None`` are DROPPED rather than written, which
    for the IMMUTABLE ``ended_at`` / ``exit_reason`` pair is the
    difference between a record that can still be retired later and one
    frozen at ``None`` forever.
    """
    from scitex_dev.store import NEW_RECORD, RevisionMismatchError

    instance_id = values.get("id")
    if not instance_id:
        raise ValueError("put_instance_record: 'id' is required")

    store = open_instances_store()
    try:
        if store.get({"id": instance_id}, include_hidden=True) is not None:
            return False
        try:
            store.put(
                {k: v for k, v in values.items() if v is not None},
                expected_revision=NEW_RECORD,
            )
        except RevisionMismatchError:
            # Another writer landed it between the read and the put. Not
            # an error: the record exists, which is the goal.
            return False
        return True
    finally:
        store.close()


__all__ = [
    "INSTANCES_STORE",
    "all_instances",
    "end_instance",
    "last_known_instance",
    "latest_active_instance",
    "list_active_instances",
    "put_instance_record",
    "record_instance_start",
    "record_instance_stop",
    "touch_instance_counters",
]
