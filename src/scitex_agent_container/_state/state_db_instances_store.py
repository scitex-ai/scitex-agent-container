#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# File: src/scitex_agent_container/_state/state_db_instances_store.py
"""The ``instances`` store — schema, connection, and the row codec.

Split out of :mod:`.state_db_instances` (which stays the import surface
:mod:`.state_db` re-exports) to keep both files under the per-file line cap,
exactly as :mod:`.state_db_comms_nodes_store` is split out of its sibling.
Nothing here is for a caller outside ``_state``: the schema declaration, the
``Store`` factory, the process-wide handle, and the two helpers that turn a
stored record into the dict shape callers read.

ON POSTGRESQL SINCE 2026-08-28 — THE LAST BIG TABLE
====================================================
``instances`` was the largest table left in ``state.db`` (603 rows on
compute-04, nine caller modules). It is PER_HOST truth: compute-04 and
spartan describing "the same agent" are describing TWO DIFFERENT PROCESSES,
and each is right about itself. That is expressed by putting ``host`` in the
record IDENTITY alongside the uuid7 ``id``, so the two observations are
different RECORDS that never meet in a merge, and by ``SINGLE_WRITER`` so
only the observing host writes its own telemetry.

The store resolves through ``scitex_dev.store.host_store``:
``SCITEX_STORE_DSN`` or the per-host PostgreSQL, with NO local-file fallback, so
a host whose PostgreSQL is unreachable raises ``StoreTargetError`` naming the
DSN it could not reach — rather than resolving an empty local file and
answering "that agent is not running", which is the failure this move exists
to remove.

``db_path`` IS GONE from every signature in the sibling module. It named a
file; there is no file.

THREE COLUMNS WERE DROPPED, AND THREE WERE ADDED BACK. BOTH MEASURED.
======================================================================
:mod:`.._store_plugin` declared ``sac_instances`` while it was still a plan.
Checking that declaration against the original DDL and against every reader in
``src/`` found gaps in both directions, and the honest fix is to close them
in the declaration rather than to paper over them here.

DROPPED — no reader anywhere in ``src/``, and no writer ever set them:

``definition_id``
    A foreign key to ``definitions``, a table nothing has ever INSERTed
    into. Every row carried NULL. ``_store_plugin.NEVER_SYNCED`` already
    said so about the referent: "a spec is a promise and its truth is the
    YAML on disk".
``scope``
    Written as the literal ``"global"`` by both writers and read by nobody.
    It survived only inside ``idx_instances_active(name, host, scope)``,
    an index whose selectivity it contributed nothing to.
``ppid``
    A parameter with no call site. NULL on every row ever written.

ADDED — production code reads them, so the store has to carry them:

``screen``
    ``cli_pkg/lifecycle/_restart_verify`` reads it off ``last_known_instance``
    to get the tmux handle it pairs with ``#{session_created}``. Without it
    the restart verifier abstains on every agent.
``workdir``
    Written at start and REWRITTEN BY RENAME (it embeds the agent name as a
    path component). Never read back off a row today — but the rename verb
    edits it, so dropping it would silently retire that coverage.
``remote``
    The authoritative locality flag, deliberately NOT a hostname compare:
    ``_agent_list_remote_rows``, ``_reconcile/_rule``, ``_peer_faillloud``,
    ``_lifecycle/_status`` and the GC all branch on it.

``bound_port`` IS FOLDED INTO ``a2a_port``, NOT CARRIED
=======================================================
The DDL had both, and every writer set them from ONE value
(``record_instance_start(a2a_port=bound, bound_port=bound)``). Two columns
holding one fact is how the two drift, and they HAD: readers disagreed about
which to prefer, and ``state_db_forward``'s docstring records a live row on
ywata-note-win where the split produced two different answers to "where do I
send this" from the same row in the same moment.

So the store keeps ONE port field, and the migration folds
``COALESCE(a2a_port, bound_port)`` into it. :func:`instance_as_dict` then
MIRRORS the value back out under both keys, because seven readers prefer
``bound_port`` and changing their shape is a second, silent change riding
inside this one. The keys stay; the second source of truth does not.

ONE HANDLE PER PROCESS — MANDATORY HERE, NOT AN OPTIMISATION
=============================================================
``instances`` sits under ``_send_resolve``, ``resolve_node_host``,
``resolve_forward_target`` and ``_agents_list`` — the a2a routing path, run
PER MESSAGE. Measured on the live primary (card
``store-connect-cost-per-call-20260828``): the previous local-file
connect cost 0.067 ms and ``psycopg.connect`` 10.707 ms — 159x — and
``Store.__init__``
pays that connect plus the dialect ``schema_lock`` and two probes even when
no DDL runs. A per-call ``Store`` measured a ~44x routing regression on the
sibling table. This module therefore caches, keyed on the resolved target,
with the same error-path eviction :func:`run_with_reconnect` implements for
``comms_nodes``.
"""

from __future__ import annotations

import os
import socket
import threading
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from scitex_dev.store import Row, Store, StoreTarget

__all__ = [
    "ACTOR",
    "CONNECT_TIMEOUT_ENV",
    "INSTANCES_STORE",
    "instance_as_dict",
    "instance_key",
    "instances_schema",
    "new_instances_store",
    "open_instances_store",
    "reset_instances_store",
    "run_with_reconnect",
    "sortable_recency",
    "strip_unset",
]

#: Logical store name. Renders as four physical tables
#: (``instances_rows``, ``_oplog``, ``_identity``, ``_cursor``).
INSTANCES_STORE = "instances"

ACTOR = "scitex-agent-container"

#: Operator override for the libpq connect timeout, in seconds.
CONNECT_TIMEOUT_ENV = "SAC_INSTANCES_CONNECT_TIMEOUT_S"

#: Seconds libpq may spend establishing a connection before giving up.
#:
#: Same judgement as ``comms_nodes``, for the same reason: this store is read
#: on the a2a ROUTING path, so a connect with libpq's default (wait forever)
#: turns a BLACKHOLED primary into a stalled event loop — no reply at all,
#: for every request the daemon is serving. A dead-but-reachable host RSTs
#: immediately and was never the danger; a host that swallows SYN is.
DEFAULT_CONNECT_TIMEOUT_S = 5


def _connect_timeout_s() -> int:
    """The configured connect timeout. Bad values fall back, loudly-ish."""
    raw = os.environ.get(CONNECT_TIMEOUT_ENV, "")
    if not raw.strip():
        return DEFAULT_CONNECT_TIMEOUT_S
    try:
        parsed = int(raw)
    except ValueError:
        return DEFAULT_CONNECT_TIMEOUT_S
    # 0 means "wait forever" to libpq, which is the behaviour this constant
    # exists to prevent; treat it as unset rather than honouring it.
    return parsed if parsed > 0 else DEFAULT_CONNECT_TIMEOUT_S


def _with_connect_timeout(target: Any) -> Any:
    """Return ``target`` with a bounded ``connect_timeout`` in its DSN.

    ``scitex_dev``'s Postgres dialect calls ``psycopg.connect(target.dsn)``
    with no timeout argument, so the bound has to travel IN the DSN. An
    explicit ``connect_timeout`` already there is left ALONE: the operator
    who wrote it outranks this default. Non-Postgres targets pass through.
    """
    from scitex_dev.store import Backend, StoreTarget

    if target.backend is not Backend.POSTGRES:
        return target
    dsn = str(target.dsn)
    if "connect_timeout" in dsn:
        return target
    separator = "&" if "?" in dsn else "?"
    return StoreTarget.postgres(
        f"{dsn}{separator}connect_timeout={_connect_timeout_s()}",
        pkg=target.pkg,
        name=target.name,
    )


def _is_connection_lost(exc: BaseException) -> bool:
    """Is ``exc`` a DEAD CONNECTION rather than a rejected operation?

    The distinction decides whether retrying can possibly help. A
    ``RevisionMismatchError`` is a verdict about the DATA and means the same
    thing on a fresh connection, so retrying it would only hide it.
    ``psycopg.OperationalError`` / ``InterfaceError`` mean the socket is
    gone, and every future call on that handle raises the same thing forever
    — psycopg3 never reconnects, and neither does ``Store``.
    """
    try:
        import psycopg
    except ImportError:  # pragma: no cover - the Postgres path needs psycopg
        return False
    return isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError))


def run_with_reconnect(operation: "Callable[[Store], Any]") -> Any:
    """Run ``operation`` against the shared handle; reopen ONCE if it died.

    The cache is keyed on the resolved target, which self-heals a changed DSN
    and a failed CONNECT — neither of those leaves a handle behind. It does
    NOT heal the case that actually happens: the connection dying UNDER a
    cached handle. Verified against the live primary by killing the backend
    with ``pg_terminate_backend``: the cached handle then raised
    ``OperationalError: the connection is closed`` on every subsequent call,
    forever, while a fresh connection proved the server healthy. In the
    long-lived listen daemon that is one PostgreSQL restart permanently
    breaking a2a routing until every daemon is restarted by hand.

    RETRY ONCE, NOT IN A LOOP. A second failure is a real outage and must
    reach the caller.

    THE HONEST CAVEAT: a retry re-runs the operation. For the dominant case
    — the connection died while IDLE and the first statement after it fails
    having done nothing — that is exactly right. Every write in the sibling
    module reads first and re-checks its precondition, so a re-run either
    finds the work already done or does it once; a ``NEW_RECORD`` insert
    comes back as ``RevisionMismatchError`` rather than as a wrong answer.
    """
    try:
        return operation(open_instances_store())
    except Exception as exc:
        if not _is_connection_lost(exc):
            raise
        reset_instances_store()
        return operation(open_instances_store())


def instances_schema() -> Any:
    """The declared ``sac_instances`` schema, as this module opens it.

    Fifteen fields, matching :data:`.._store_plugin.INSTANCES` field for
    field. The store name is the bare table name — ``sac_`` is the plugin
    namespace, not the store's, exactly as ``comms_nodes`` is opened under
    its bare name while the plugin declares ``sac_comms_nodes``.

    The merge rules are chosen so a STALE replica can never move a live
    host's truth backwards; the reasoning lives in ``_store_plugin`` beside
    the declaration and is not duplicated here. What IS worth repeating is
    the trap the IMMUTABLE fields carry, because it is invisible and this
    module is where the writes are:

    MEASURED — an IMMUTABLE field is frozen by its FIRST WRITE, and writing
    ``None`` counts as a write. ``merge_field`` takes the incoming value
    whenever ``current_stamp is None`` ("immutability starts once there IS a
    value") and otherwise reports a ``MergeConflict`` and KEEPS the first
    value, without raising. So a ``put`` that carries ``ended_at=None`` on a
    live row STAMPS that field at None, and the GC's later tombstone is then
    silently rejected — the row can never be ended again. Every write path in
    the sibling module goes through :func:`strip_unset` for that reason, and
    every one of them is a PARTIAL put (``Store.put`` documents that absent
    fields are left alone) rather than a whole-record rewrite.
    """
    from scitex_dev.store import FieldKind, FieldPolicy, FieldRole, MergeRule, Schema

    def ident(kind: Any) -> Any:
        return FieldPolicy(
            kind=kind,
            role=FieldRole.IDENTITY,
            required=True,
            merge=MergeRule.IMMUTABLE,
            indexed=True,
        )

    def data(
        kind: Any,
        merge: Any,
        *,
        required: bool = False,
        indexed: bool = False,
    ) -> Any:
        return FieldPolicy(
            kind=kind,
            role=FieldRole.DATA,
            required=required,
            merge=merge,
            indexed=indexed,
        )

    lww = MergeRule.LAST_WRITER_WINS
    return Schema(
        name=INSTANCES_STORE,
        fields={
            # PER_HOST truth: ``host`` in the identity makes two hosts'
            # observations different RECORDS rather than a merge to resolve.
            "id": ident(FieldKind.TEXT),
            "host": ident(FieldKind.TEXT),
            "name": data(FieldKind.TEXT, lww, required=True, indexed=True),
            "pid": data(FieldKind.INTEGER, lww),
            "a2a_port": data(FieldKind.INTEGER, lww),
            "screen": data(FieldKind.TEXT, lww),
            "workdir": data(FieldKind.TEXT, lww),
            "remote": data(FieldKind.BOOL, lww),
            # LAST_WRITER_WINS, and the plugin says the same since this
            # module measured why — see ``_store_plugin.INSTANCES``.
            "spawned_by": data(FieldKind.TEXT, lww),
            "started_at": data(FieldKind.TEXT, MergeRule.IMMUTABLE, required=True),
            "last_heartbeat_at": data(FieldKind.TEXT, MergeRule.MAX),
            "iter_count": data(FieldKind.INTEGER, MergeRule.MAX),
            "input_tokens": data(FieldKind.INTEGER, MergeRule.MAX),
            "output_tokens": data(FieldKind.INTEGER, MergeRule.MAX),
            "ended_at": data(FieldKind.TEXT, MergeRule.IMMUTABLE),
            "exit_reason": data(FieldKind.TEXT, MergeRule.IMMUTABLE),
        },
    )


#: The process-wide handle and the target it was opened for. Guarded by
#: ``_HANDLE_LOCK``; see :func:`open_instances_store` for why it exists.
_HANDLE_LOCK = threading.Lock()
_HANDLE: "Store | None" = None
_HANDLE_TARGET: "StoreTarget | None" = None


def new_instances_store() -> "Store":
    """Construct a FRESH, caller-owned handle. RAISES if PostgreSQL is down.

    For code that legitimately wants its own connection and will close it —
    the one-shot data migration, chiefly. Ordinary readers and writers want
    :func:`open_instances_store`, which hands back the shared one.

    Raising is the whole point. ``list_active_instances`` answering empty is
    read everywhere as "no agent is running", so a store that resolved to an
    empty local file instead of raising would turn a database outage into a
    fleet-wide "nothing is running" — silently, on the routing path.
    """
    from scitex_dev.store import Store, WriterPolicy, host_store

    schema = instances_schema()
    return Store(
        _with_connect_timeout(
            host_store(pkg="scitex_agent_container", name=schema.name)
        ),
        schema,
        node=socket.gethostname(),
        # SINGLE_WRITER as declared: only the observing host writes its own
        # telemetry. ``check_owner`` compares the record's owner against the
        # store's ``actor``, and every sac store passes the same package
        # constant, so what this refuses in practice is a write from a
        # different PACKAGE — the cross-host guard is the ``host`` identity
        # field, which makes a peer's row a different record entirely.
        writer_policy=WriterPolicy.SINGLE_WRITER,
        actor=ACTOR,
    )


def open_instances_store() -> "Store":
    """The process's shared instances store. DO NOT ``close()`` the result.

    ONE HANDLE PER PROCESS — see the module docstring for the measured cost
    that makes this mandatory rather than a tidy-up. ``Store`` carries its
    own internal ``RLock`` and is built to be shared, so one handle per
    process is the intended shape rather than a shortcut.

    THE CACHE IS KEYED ON THE RESOLVED TARGET, NOT ON "have we opened one".
    ``host_store`` re-resolves ``SCITEX_STORE_DSN`` on every call and is
    cheap (an env read and a frozen dataclass — it does not connect), so the
    key costs nothing and buys correctness: a changed DSN yields a different
    ``StoreTarget``, which swaps the handle instead of silently serving the
    previous database. That is not hypothetical — the suite's ``pg_schema``
    fixture points the variable at a fresh throwaway schema per test, and a
    cache that ignored it would hand every test the first test's store.
    :func:`reset_instances_store` is the explicit hook for anything that
    needs to drop the handle without changing the target.
    """
    global _HANDLE, _HANDLE_TARGET

    from scitex_dev.store import host_store

    target = _with_connect_timeout(
        host_store(pkg="scitex_agent_container", name=INSTANCES_STORE)
    )
    with _HANDLE_LOCK:
        if _HANDLE is not None and _HANDLE_TARGET == target:
            return _HANDLE
        _close_handle_locked()
        handle = new_instances_store()
        _HANDLE, _HANDLE_TARGET = handle, target
        return handle


def reset_instances_store() -> None:
    """Drop the shared handle, closing it. Idempotent.

    The reset hook for anything that changes where the store resolves to
    WITHOUT changing ``SCITEX_STORE_DSN`` (a changed DSN invalidates the
    cache on its own). Tests use it in a real fixture teardown; the
    ecosystem bans ``monkeypatch``, so the hook is a plain function rather
    than an attribute anyone reaches in and rewrites.
    """
    with _HANDLE_LOCK:
        _close_handle_locked()


def _close_handle_locked() -> None:
    """Drop the cached handle. Caller MUST hold ``_HANDLE_LOCK``.

    The reference is cleared BEFORE the close, so a connection that raises on
    close cannot leave a dead handle cached — which would make every later
    call fail on a store nothing can replace.
    """
    global _HANDLE, _HANDLE_TARGET

    handle, _HANDLE, _HANDLE_TARGET = _HANDLE, None, None
    if handle is None:
        return
    try:
        handle.close()
    except Exception:  # stx-allow: fallback (reason: the handle is already dropped; a server that vanished makes close() raise, and failing here would turn a successful re-open into an error)
        pass


def instance_key(values: Any) -> dict[str, Any]:
    """The record identity ``{id, host}`` for a row dict or a ``Row.values``."""
    return {"id": str(values["id"]), "host": str(values["host"])}


def strip_unset(values: dict[str, Any]) -> dict[str, Any]:
    """Drop every ``None`` from a put payload. NOT cosmetic — see below.

    ``Store.put`` is a PARTIAL update: absent fields are left alone. A field
    carrying ``None`` is therefore not "the same as absent" — it is a WRITE
    of the value None, which stamps the field's HLC.

    For the four IMMUTABLE fields that is permanent damage: ``merge_field``
    freezes an IMMUTABLE field at its first stamped value, so a start that
    wrote ``ended_at=None`` would make the GC's later tombstone a silently
    rejected ``MergeConflict`` — the row could never be ended, and nothing
    would raise. For the MAX counters it is harmless but pointless. So every
    write path strips, and the identity fields are added back by the caller.
    """
    return {k: v for k, v in values.items() if v is not None}


def sortable_recency(row: dict[str, Any]) -> tuple[str, str]:
    """The ``(started_at, id)`` key every reader orders DESC by.

    The ``id`` tiebreak is load-bearing, not decoration: ``resolve_node_host``
    and ``resolve_forward_target`` both ordered ``started_at DESC, id DESC``
    and took ``LIMIT 1``. ``started_at`` is second-resolution, so two starts
    inside one second are a real tie, and something has to break it or the
    answer to "where do I send this message" changes between calls.

    WHAT THE TIEBREAK DOES AND DOES NOT GUARANTEE — MEASURED, because the
    obvious reading is wrong on the runtime this fleet actually uses.
    ``new_uuid7`` returns a real time-ordered uuid7 only where
    ``uuid.uuid7`` exists (Python 3.14+); everywhere else — including the
    3.12 in the sac image — it FALLS BACK TO uuid4, which is random. So on
    today's runtime ``id DESC`` guarantees DETERMINISM (the same tie resolves
    the same way every call, which is what stops a resolver flapping between
    two live records) and does NOT guarantee RECENCY.

    This is inherited, not introduced: the original ``ORDER BY started_at
    DESC, id DESC`` had exactly the same property, against the same ids. It is
    written down here rather than left implied because "uuid7 is
    time-ordered" is the kind of premise a reader accepts without checking,
    and a caller that needs true recency inside one second needs a finer
    ``started_at``, not a better tiebreak.
    """
    return (str(row.get("started_at") or ""), str(row.get("id") or ""))


def instance_as_dict(row: "Row") -> dict[str, Any]:
    """One stored record in the caller-facing dict shape.

    ``bound_port`` is MIRRORED from ``a2a_port`` — the two columns always
    held one value and the store now keeps one field, but seven readers
    prefer the ``bound_port`` key and changing their shape is not part of a
    storage move. See the module docstring.

    ``remote`` is emitted as ``0``/``1`` rather than as a Python bool because
    the rows callers were handed always did: they compare it truthily, but a
    test asserting
    ``row["remote"] == 1`` is asserting the shape it was handed, and this
    move must not quietly change that answer.

    The three columns this store dropped (``definition_id``, ``scope``,
    ``ppid``) are NOT re-emitted as ``None``. No reader in ``src/`` ever
    touched them, and a key present with a plausible NULL is exactly how a
    dropped concept survives as a thing people write code against.
    """
    values = row.values
    port = values.get("a2a_port")
    port = None if port is None else int(port)
    return {
        "id": str(values["id"]),
        "host": str(values["host"]),
        "name": str(values["name"]) if values.get("name") is not None else None,
        "pid": None if values.get("pid") is None else int(values["pid"]),
        "screen": values.get("screen"),
        "workdir": values.get("workdir"),
        "a2a_port": port,
        "bound_port": port,
        "remote": 1 if values.get("remote") else 0,
        "spawned_by": values.get("spawned_by"),
        "started_at": values.get("started_at"),
        "last_heartbeat_at": values.get("last_heartbeat_at"),
        "iter_count": values.get("iter_count"),
        "input_tokens": values.get("input_tokens"),
        "output_tokens": values.get("output_tokens"),
        "ended_at": values.get("ended_at"),
        "exit_reason": values.get("exit_reason"),
    }

# EOF
