"""The ``comms_nodes`` store — schema, connection, and the row codec.

Split out of :mod:`.state_db_comms_nodes` (which stays the import surface
everything else uses) to keep both files under the per-file line cap. This
half holds nothing a caller outside ``_state`` should reach for: the schema
declaration, the ``Store`` factory, and the two helpers that turn a stored
record into the dict shape callers read.

ON POSTGRESQL SINCE 2026-08-28, AND THAT DELETES THE SYNC LAYER
===============================================================
``comms_nodes`` was the one table sac already synced, and the sync was
provably lossy in a way its own source admitted: ``sac registry sync``
ssh-pulled a peer's ``sac db export --tables comms_nodes`` and fed it to
``import_state``, which was ``INSERT OR IGNORE`` on the ``name`` primary
key (both verbs deleted 2026-08-29).
That statement can carry neither an UPDATE nor a deletion, so a node that
MOVED (new port) and a node that LEFT (tombstoned) both arrived at the peer
as ``rowcount == 0`` — the importer's success value and its did-not-happen
value being the same number. The pre-move docstring of
``unregister_comms_node`` said as much: the deletion "will need an
UPDATE-shaped sync (future work)".

There is no future work. On the shared primary store every host reads and
writes ONE directory, so cross-host resolution is a read rather than a
replicated guess, and the anti-entropy layer has nothing left to converge.

The store resolves through ``scitex_dev.store.host_store``:
``SCITEX_STORE_DSN`` or the per-host PostgreSQL, with NO local-file fallback, so
a host whose PostgreSQL is unreachable raises ``StoreTargetError`` naming the
DSN it could not reach — rather than resolving an empty local file and
answering "that agent is not registered", which is the failure this whole
move exists to remove.

``db_path`` IS GONE from every signature in the sibling module. It named a
file; there is no file. Test isolation comes from pointing
``SCITEX_STORE_DSN`` at a throwaway schema (the ``pg_schema`` fixture),
which is stronger than a temp path was because it exercises the real
resolver.

THREE COLUMNS DID NOT MOVE, AND EACH IS A CONCEPT THE PRIMITIVE OWNS
====================================================================
The schema below is the one :mod:`.._store_plugin` declared for
``sac_comms_nodes`` back when it was still a plan — ``name`` identity,
``host`` and ``a2a_port`` last-writer-wins, ``registered_at`` immutable —
and it declares exactly four fields. The three legacy columns that are NOT
here were each a hand-rolled copy of something the store already maintains,
and keeping a second copy is how the two drift:

``ended_at`` → ``hide()`` / ``unhide()``
    The soft tombstone. It existed so the next export could carry a
    deletion, which INSERT OR IGNORE then dropped on the floor. The store's
    hide is its ONLY removal, it replicates as an op like any other, and the
    record plus its whole history stays readable through
    ``include_hidden=True``. ``unregister_comms_node`` hides;
    ``register_comms_node`` unhides. Nothing is ever hard-deleted, so "was
    never registered" and "was registered and stopped" remain different
    answers — which they were NOT going to be for much longer, because the
    previous implementation's own docstring planned a GC that physically
    deletes tombstones.

``source_host`` → the reserved ``_origin`` column (``Row.origin``)
    Hand-rolled provenance, and :mod:`.._store_plugin` measured what it was
    actually worth: NULL for every locally registered row, set only on the
    pull path, so it recorded who a row was pulled FROM and left a
    locally-created row anonymous. ``_origin`` is stamped by the primitive on
    every op from ``Store.node``, so the conflict detector asks the RECORD
    who wrote it instead of trusting a column the writer had to remember to
    fill.

``updated_at`` → the hybrid logical clock (``Row.hlc``)
    "When was this record last touched". Every op restamps it, so the value
    is maintained by construction rather than by each writer remembering to
    bump a column, and it is comparable ACROSS hosts: ``(wall_us, logical,
    node)`` is a total order immune to the clock skew that made a wall-clock
    ``updated_at`` meaningless the moment two hosts wrote the same directory.
    :func:`comms_node_as_dict` still returns ``updated_at`` (and
    ``ended_at``, and ``source_host``) so the caller shape is unchanged; they
    are now derived rather than stored.

WHY ``registered_at`` IS IMMUTABLE, WHICH IS THE CONFLICT POLICY ITSELF
======================================================================
ADR-0014 makes names globally unique, and two hosts claiming one name with
different registration times is exactly the collision this table has raised
``CommsNodeConflictError`` for since Stage 1. Declaring the field IMMUTABLE
makes the primitive report that as a ``MergeConflict`` (kept / rejected /
reason) instead of quietly picking a winner — so the fail-loud policy
survives replication rather than only surviving inside one process. A LOCAL
re-registration never trips it: the writer carries the stored
``registered_at`` forward untouched, so only a genuinely different claim can
produce a second value.

SINGLE_WRITER, AS DECLARED — AND WHAT IT DOES AND DOES NOT ENFORCE
==================================================================
:mod:`.._store_plugin` classifies this as FLEET truth under
``WriterPolicy.SINGLE_WRITER``, and that declaration is kept rather than
downgraded the way ``node_comms_policy``'s was. What it enforces is
measured, not assumed: ``Store.put`` calls ``check_owner``, which compares
the record's ``owner`` against the store's ``actor`` — and every sac store
passes the package constant ``"scitex-agent-container"``, so the owner is
that same string on every host. The mode therefore refuses a write from a
different PACKAGE, and does not refuse the operator repairing a peer's entry
from another machine (``sac registry register``), which a directory of FLEET
truth must keep allowing. The cross-host claim that IS a real contradiction
is caught one level up, by the origin check in ``register_comms_node`` and by
IMMUTABLE ``registered_at``.

ONE HANDLE PER PROCESS — THIS MODULE DIFFERS FROM ITS SIBLINGS ON PURPOSE
=========================================================================
Every other migrated module opens and closes a ``Store`` per call. This one
caches: ``comms_nodes`` is the a2a ROUTING path, read once per message, and
``psycopg.connect`` costs 10.707 ms against the 0.067 ms the previous
local-file backend paid (159x, measured on the live primary — card
``sqlite-out-per-call-connect-cost-20260828``). See
:func:`open_comms_nodes_store` for the end-to-end number, the target-keyed
invalidation, and what happens when the connection dies.
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
    "COMMS_NODES_STORE",
    "CONNECT_TIMEOUT_ENV",
    "comms_node_as_dict",
    "comms_nodes_schema",
    "hlc_seconds",
    "new_comms_nodes_store",
    "open_comms_nodes_store",
    "reset_comms_nodes_store",
    "run_with_reconnect",
]

#: Logical store name. Renders as four physical tables
#: (``comms_nodes_rows``, ``_oplog``, ``_identity``, ``_cursor``).
COMMS_NODES_STORE = "comms_nodes"

ACTOR = "scitex-agent-container"

#: Operator override for the libpq connect timeout, in seconds.
CONNECT_TIMEOUT_ENV = "SAC_COMMS_NODES_CONNECT_TIMEOUT_S"

#: Seconds libpq may spend establishing a connection before giving up.
#:
#: NOT optional, and not a tuning knob. This store is read on the a2a
#: ROUTING path -- ``_listen/_node_channel.node_message_send`` is an ``async
#: def`` that calls ``is_local_node`` synchronously -- so a connect with
#: libpq's default (wait forever) turns a BLACKHOLED primary into a stalled
#: event loop: not a slow reply, no reply at all, for every request the
#: daemon is serving. A dead-but-reachable host RSTs immediately and was
#: never the danger; a host that swallows SYN is, and that is the ordinary
#: shape of a machine losing power or a firewall rule landing.
#:
#: 5s matches the ceiling the deleted ``registry sync`` used for the same
#: judgement (``_PEER_SSH_CONNECT_TIMEOUT_S``): long enough that a loaded
#: healthy primary is not declared down, short enough that a request fails
#: rather than hangs.
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
    with no timeout argument, so the bound has to travel IN the DSN. libpq
    reads ``connect_timeout`` from the URI query string, which is the form
    both resolutions produce (the ``SCITEX_STORE_DSN`` override and the
    per-host socket DSN).

    An explicit ``connect_timeout`` already in the DSN is left ALONE: the
    operator who wrote it outranks this default.

    Non-Postgres targets pass through untouched.
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
    ``RevisionMismatchError`` or a ``CommsNodeConflictError`` is a verdict
    about the DATA and means the same thing on a fresh connection, so
    retrying it would only hide it. ``psycopg.OperationalError`` /
    ``InterfaceError`` mean the socket is gone, and every future call on
    that handle raises the same thing forever -- psycopg3 never reconnects,
    and neither does ``Store``.
    """
    try:
        import psycopg
    except ImportError:  # pragma: no cover - the Postgres path needs psycopg
        return False
    return isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError))


def run_with_reconnect(operation: "Callable[[Store], Any]") -> Any:
    """Run ``operation`` against the shared handle; reopen ONCE if it died.

    WHY THIS EXISTS, MEASURED
    =========================
    The cache is keyed on the resolved target, which self-heals a changed
    DSN and a failed CONNECT -- neither of those leaves a handle behind. It
    does NOT heal the case that actually happens: the connection dying
    UNDER a cached handle. Verified against the live primary by killing the
    backend with ``pg_terminate_backend``: the cached handle then raised
    ``OperationalError: the connection is closed`` on every subsequent call,
    forever, while a fresh connection proved the server healthy. In the
    long-lived listen daemon that is one PostgreSQL restart permanently
    breaking a2a routing until every daemon is restarted by hand.

    RETRY ONCE, NOT IN A LOOP. A second failure is a real outage and must
    reach the caller, who is already best-effort everywhere this matters.

    THE HONEST CAVEAT: a retry re-runs the operation. For the dominant case
    -- the connection died while IDLE and the first statement after it fails
    having done nothing -- that is exactly right. If instead the server
    committed a write and died before acknowledging it, the retry re-applies
    it: harmless for the ``ANY_REVISION`` upserts and for hide/unhide, which
    are idempotent, and loud rather than silent for a ``NEW_RECORD`` insert,
    which comes back as ``RevisionMismatchError`` instead of a wrong answer.
    Choosing that over a permanently dead handle is the trade this makes.
    """
    try:
        return operation(open_comms_nodes_store())
    except Exception as exc:
        if not _is_connection_lost(exc):
            raise
        reset_comms_nodes_store()
        return operation(open_comms_nodes_store())


def comms_nodes_schema() -> Any:
    """The declared ``sac_comms_nodes`` schema, as this module opens it.

    Four fields, matching :data:`.._store_plugin.COMMS_NODES` field for
    field. The store name is the bare table name — ``sac_`` is the plugin
    namespace, not the store's, exactly as ``comms_grants`` is opened under
    its bare name while the plugin declares ``sac_comms_grants``.
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

    def addr(kind: Any, *, indexed: bool = False) -> Any:
        """The routing tuple. The newest write IS the best answer.

        An agent that restarts on a different port has genuinely moved, and
        ``spec.a2a.port: auto`` makes that the NORMAL outcome of a restart,
        so LAST_WRITER_WINS is honest here in a way it is not for a
        historical fact like ``registered_at``.
        """
        return FieldPolicy(
            kind=kind,
            role=FieldRole.DATA,
            required=True,
            merge=MergeRule.LAST_WRITER_WINS,
            indexed=indexed,
        )

    return Schema(
        name=COMMS_NODES_STORE,
        fields={
            # The agent name IS the identity, exactly as the previous
            # PRIMARY KEY treated it. ADR-0014: names are globally unique.
            "name": ident(FieldKind.TEXT),
            "host": addr(FieldKind.TEXT, indexed=True),
            "a2a_port": addr(FieldKind.INTEGER),
            # IMMUTABLE — see the module docstring. This is the conflict
            # policy, not a timestamp preference.
            "registered_at": FieldPolicy(
                kind=FieldKind.REAL,
                role=FieldRole.DATA,
                required=True,
                merge=MergeRule.IMMUTABLE,
                indexed=False,
            ),
        },
    )


#: The process-wide handle and the target it was opened for. Guarded by
#: ``_HANDLE_LOCK``; see :func:`open_comms_nodes_store` for why it exists.
_HANDLE_LOCK = threading.Lock()
_HANDLE: "Store | None" = None
_HANDLE_TARGET: "StoreTarget | None" = None


def new_comms_nodes_store() -> "Store":
    """Construct a FRESH, caller-owned handle. RAISES if PostgreSQL is down.

    For code that legitimately wants its own connection and will close it —
    the one-shot data migration, chiefly. Ordinary readers and writers want
    :func:`open_comms_nodes_store` instead, which hands back the shared one.

    Raising is the whole point. Every caller of ``resolve_comms_node_host``
    reads ``None`` as "this name is not in the federated graph, do not
    cross-host forward", so a store that answered empty instead of raising
    would turn a database outage into "the fleet has no agents" — silently,
    on the routing path.
    """
    from scitex_dev.store import Store, WriterPolicy, host_store

    schema = comms_nodes_schema()
    return Store(
        _with_connect_timeout(
            host_store(pkg="scitex_agent_container", name=schema.name)
        ),
        schema,
        node=socket.gethostname(),
        # SINGLE_WRITER as declared — see the module docstring for what that
        # does and does not enforce.
        writer_policy=WriterPolicy.SINGLE_WRITER,
        actor=ACTOR,
    )


def open_comms_nodes_store() -> "Store":
    """The process's shared directory store. DO NOT ``close()`` the result.

    ONE HANDLE PER PROCESS, BECAUSE THIS IS THE A2A ROUTING HOT PATH
    ================================================================
    Every sibling migrated module opens and closes a ``Store`` per call,
    mirroring the old ``with open_db(...)`` shape, and for a store touched
    once per operator command that is the right trade. This one is not that:
    ``lookup_comms_node`` / ``resolve_comms_node_host`` /
    ``list_comms_nodes`` sit under ``resolve_node_host``,
    ``resolve_forward_target`` and ``_agents_list``, which run PER MESSAGE.

    Measured on the live primary (card
    ``sqlite-out-per-call-connect-cost-20260828``): the previous local-file
    connect cost 0.067 ms and ``psycopg.connect`` 10.707 ms — 159x — and
    ``Store.__init__``
    pays that connect plus the dialect ``schema_lock`` and two probes even
    when no DDL runs. End to end, ``resolve_comms_node_host`` measured
    1.03 ms/call before the move against 45.3 ms/call with a per-call
    ``Store``:
    a ~44x routing regression, which is not a cost this table can pay.

    ``Store`` carries its own internal ``RLock`` and is built to be shared,
    so one handle per process is the intended shape rather than a shortcut.

    THE CACHE IS KEYED ON THE RESOLVED TARGET, NOT ON "have we opened one"
    ---------------------------------------------------------------------
    ``host_store`` re-resolves ``SCITEX_STORE_DSN`` on every call and is
    cheap (an env read and a frozen dataclass — it does not connect), so the
    key costs nothing and buys correctness: a changed DSN yields a different
    ``StoreTarget``, which swaps the handle instead of silently serving the
    previous database. That is not a hypothetical — the test suite's
    ``pg_schema`` fixture points the variable at a fresh throwaway schema per
    test, and a cache that ignored it would hand every test the first test's
    store. :func:`reset_comms_nodes_store` is the explicit hook for anything
    that needs to drop the handle without changing the target.

    A DEAD CONNECTION FAILS LOUDLY; THERE IS NO RETRY HERE
    -----------------------------------------------------
    If the server restarts under a cached handle, the next call raises
    whatever ``psycopg`` raises for a closed connection — the store adds no
    reconnect and neither does this. That is deliberate: a routing lookup
    that silently retried could mask a database that is down, and ``None``
    from this path means "not registered", which is the one answer a
    transient failure must never be allowed to fabricate. The caller's own
    best-effort handling (every production writer catches and logs) is where
    a failure is absorbed, in the open.
    """
    global _HANDLE, _HANDLE_TARGET

    from scitex_dev.store import host_store

    target = _with_connect_timeout(
        host_store(pkg="scitex_agent_container", name=COMMS_NODES_STORE)
    )
    with _HANDLE_LOCK:
        if _HANDLE is not None and _HANDLE_TARGET == target:
            return _HANDLE
        _close_handle_locked()
        handle = new_comms_nodes_store()
        _HANDLE, _HANDLE_TARGET = handle, target
        return handle


def reset_comms_nodes_store() -> None:
    """Drop the shared handle, closing it. Idempotent.

    The reset hook for anything that changes where the store resolves to
    WITHOUT changing ``SCITEX_STORE_DSN`` (a changed DSN invalidates the
    cache on its own — see :func:`open_comms_nodes_store`). Tests use it in
    a real fixture teardown; the ecosystem bans ``monkeypatch``, so the hook
    is a plain function rather than an attribute anyone reaches in and
    rewrites.
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


def hlc_seconds(row: "Row") -> float:
    """The record's last-op stamp as a POSIX-style float.

    The successor to the ``updated_at`` column. ``wall_us`` is microseconds
    since the epoch, so this is the same quantity the column held, sourced
    from the clock the primitive already maintains.
    """
    return float(row.hlc.wall_us) / 1_000_000.0


def comms_node_as_dict(row: "Row") -> dict[str, Any]:
    """One stored record in the caller-facing dict shape.

    ``updated_at`` / ``ended_at`` / ``source_host`` are DERIVED (see the
    module docstring): the HLC, the hide flag, and ``_origin`` respectively.
    Returning them keeps every reader — ``sac listen``'s agent listing, the
    lifecycle tests, the operator's ``sac registry`` output — working against
    the same keys.
    """
    values = row.values
    return {
        "name": str(values["name"]),
        "host": str(values["host"]),
        "a2a_port": int(values["a2a_port"]),
        "registered_at": float(values["registered_at"]),
        "updated_at": hlc_seconds(row),
        "source_host": str(row.origin),
        "ended_at": hlc_seconds(row) if row.hidden else None,
    }
