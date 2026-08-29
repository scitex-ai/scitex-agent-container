"""The ``lineage`` store — schema, connection, and the edge index.

The spawn DAG: one record per CHILD, naming the parent that spawned it.
ON POSTGRESQL SINCE 2026-08-28, opened field for field from the
``sac_lineage`` schema :mod:`.._store_plugin` had declared for it since the
classification was written.

WHY THIS TABLE IS DIFFERENT FROM THE THREE THAT WENT BEFORE IT
==============================================================
``comms_grants`` was authorisation, ``node_comms_policy`` was policy, and
``comms_nodes`` was routing. Each of those is READ by the ACL. ``lineage``
is what the ACL is DERIVED FROM: :func:`..state_db_nodes.derive_group`
turns these edges into the default-ACL group,
:func:`.state_db_lineage_rel.sender_target_relationship` turns them into
the self/parent/child/sibling classification every send is judged against,
and :func:`._lineage.descendants_of` turns them into the manage-scope
``check_lineage_acl`` gates agent CRUD with. An edge that cannot be read is
not a degraded answer; it is a DIFFERENT ACL.

That is also why the readers here are not best-effort. A store that
answered "no edges" on an outage would make every agent a ROOT, and a root
may spawn (:func:`..state_db_nodes.spawn_allowed`) — the outage would hand
out spawn authority. ``host_store`` resolves ``SCITEX_STORE_DSN`` or the
per-host PostgreSQL with NO SQLite fallback, so an unreachable primary
raises ``StoreTargetError`` naming the DSN instead of resolving an empty
local file and quietly promoting the whole fleet.

``db_path`` IS GONE from every signature that took it. It named a SQLite
file; there is no file. Test isolation comes from pointing
``SCITEX_STORE_DSN`` at a throwaway schema (the ``pg_schema`` fixture),
which is stronger than a temp path was because it exercises the real
resolver.

``parent_name`` IS IMMUTABLE, AND THAT IS THE CONFLICT POLICY ITSELF
====================================================================
:mod:`.._store_plugin` declares it so, and the declaration is implemented
rather than revisited. A child has exactly one parent, ever. If two hosts
claim different parents for one child, that is a real contradiction about
the spawn DAG, and LAST_WRITER_WINS would silently rewrite the family tree
— which, because the ACL derives group membership from it, is a silent
PRIVILEGE CHANGE.

MEASURED BEFORE THE CUTOVER, and it is not hypothetical: on 2026-08-28 the
fleet's four state.db files held 23 edges between them and ONE child
disagreed — ``scitex-cards`` recorded ``proj-scitex-hub`` as its parent on
scitex-compute-03 and ``scitex-agent-container`` on scitex-compute-04.
Exactly the contradiction this rule exists to surface.

IMMUTABLE KEEPS THE FIRST VALUE AND DOES NOT RAISE. A later, differing
write is reported in ``PutResult.conflicts`` as a ``MergeConflict``
carrying ``kept`` / ``rejected`` / ``reason``, and both values stay in the
oplog. That maps onto :func:`..state_db_nodes.record_lineage`'s
keeps-first-and-logs contract exactly — but it maps onto it only if the
caller READS the conflicts, which is why the writer there inspects the
result rather than relying on an exception that never comes.

MULTI_WRITER, NOT SINGLE_WRITER
===============================
A cross-host spawn is BROKERED, so the edge can legitimately be written by
either end: the child's host through
:func:`.._lifecycle._spawn_gate.check_spawn`, or the broker host through
:mod:`.._listen._agent_exec`. A second writer must get the loud
``MergeConflict`` that says WHAT the two disagree about, not an ownership
rejection that says only that somebody else got there first.

ONE HANDLE PER PROCESS
======================
Same judgement, and the same measurement, as
:mod:`.state_db_comms_nodes_store`: ``psycopg.connect`` costs 10.707 ms
against ``sqlite3.connect``'s 0.067 ms (159x, live primary, card
``sqlite-out-per-call-connect-cost-20260828``), and these readers sit on
the ACL path of EVERY message send and every agent-CRUD request. A per-call
``Store`` would pay that connect on each one. See
:func:`open_lineage_store` for the target-keyed invalidation and
:func:`run_with_reconnect` for what happens when the connection dies under
the cached handle.
"""

from __future__ import annotations

import os
import socket
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:  # pragma: no cover - typing only
    from scitex_dev.store import Row, Store, StoreTarget

__all__ = [
    "ACTOR",
    "CONNECT_TIMEOUT_ENV",
    "LINEAGE_STORE",
    "LineageEdges",
    "lineage_edge_as_dict",
    "lineage_schema",
    "new_lineage_store",
    "open_lineage_store",
    "parent_name_of",
    "read_edges",
    "reset_lineage_store",
    "run_with_reconnect",
]

#: Logical store name. Renders as four physical tables
#: (``lineage_rows``, ``_oplog``, ``_identity``, ``_cursor``).
LINEAGE_STORE = "lineage"

ACTOR = "scitex-agent-container"

#: Operator override for the libpq connect timeout, in seconds.
CONNECT_TIMEOUT_ENV = "SAC_LINEAGE_CONNECT_TIMEOUT_S"

#: Seconds libpq may spend establishing a connection before giving up.
#:
#: NOT a tuning knob, for the same reason it is not one in
#: :mod:`.state_db_comms_nodes_store`. These edges are read from
#: ``check_send_acl`` and ``check_lineage_acl``, which the listen daemon
#: calls while serving a request, so a connect with libpq's default (wait
#: forever) turns a BLACKHOLED primary into a stalled handler: not a slow
#: 403, no answer at all, for every request the daemon is serving. A
#: dead-but-reachable host RSTs immediately and was never the danger; a host
#: that swallows SYN is, and that is the ordinary shape of a machine losing
#: power or a firewall rule landing.
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

    The cache is keyed on the resolved target, which self-heals a changed
    DSN and a failed CONNECT — neither of those leaves a handle behind. It
    does NOT heal the case that actually happens: the connection dying UNDER
    a cached handle. Verified against the live primary for the sibling
    directory store by killing the backend with ``pg_terminate_backend``:
    the cached handle then raised ``OperationalError: the connection is
    closed`` on every subsequent call, forever, while a fresh connection
    proved the server healthy. In the long-lived listen daemon that is one
    PostgreSQL restart permanently breaking the ACL until every daemon is
    restarted by hand.

    RETRY ONCE, NOT IN A LOOP. A second failure is a real outage and must
    reach the caller.

    THE HONEST CAVEAT: a retry re-runs the operation. Every reader here is a
    pure read, so re-running one is free. The one WRITER,
    :func:`..state_db_nodes.record_lineage`, is a get-then-put that is
    idempotent by construction — a re-run reads the row the first attempt
    may have committed and returns without writing again.
    """
    try:
        return operation(open_lineage_store())
    except Exception as exc:
        if not _is_connection_lost(exc):
            raise
        reset_lineage_store()
        return operation(open_lineage_store())


def lineage_schema() -> Any:
    """The declared ``sac_lineage`` schema, as this module opens it.

    Three fields, matching :data:`.._store_plugin.LINEAGE` field for field.
    The store name is the bare table name — ``sac_`` is the plugin
    namespace, not the store's, exactly as ``comms_nodes`` is opened under
    its bare name while the plugin declares ``sac_comms_nodes``.
    """
    from scitex_dev.store import FieldKind, FieldPolicy, FieldRole, MergeRule, Schema

    return Schema(
        name=LINEAGE_STORE,
        fields={
            # The CHILD is the identity, exactly as the SQLite PRIMARY KEY
            # treated it: a child has one parent, ever, so one record per
            # child makes distinct edges distinct records and a union can
            # drop none of them.
            "child_name": FieldPolicy(
                kind=FieldKind.TEXT,
                role=FieldRole.IDENTITY,
                required=True,
                merge=MergeRule.IMMUTABLE,
                indexed=True,
            ),
            # IMMUTABLE — see the module docstring. This is the conflict
            # policy, not a timestamp preference: a differing second claim
            # is a contradiction about the spawn DAG and the ACL derives
            # group membership from it.
            "parent_name": FieldPolicy(
                kind=FieldKind.TEXT,
                role=FieldRole.DATA,
                required=True,
                merge=MergeRule.IMMUTABLE,
                indexed=True,
            ),
            # When the edge was FIRST recorded. IMMUTABLE for the ordinary
            # reason a historical stamp is: the spawn happened once.
            "created_at": FieldPolicy(
                kind=FieldKind.REAL,
                role=FieldRole.DATA,
                required=True,
                merge=MergeRule.IMMUTABLE,
                indexed=False,
            ),
        },
    )


#: The process-wide handle and the target it was opened for. Guarded by
#: ``_HANDLE_LOCK``; see :func:`open_lineage_store` for why it exists.
_HANDLE_LOCK = threading.Lock()
_HANDLE: "Store | None" = None
_HANDLE_TARGET: "StoreTarget | None" = None


def new_lineage_store() -> "Store":
    """Construct a FRESH, caller-owned handle. RAISES if PostgreSQL is down.

    For code that legitimately wants its own connection and will close it —
    the one-shot data migration, chiefly. Ordinary readers and writers want
    :func:`open_lineage_store` instead, which hands back the shared one.

    Raising is the whole point, and here it is sharper than for the
    directory: every reader of these edges treats "no parent" as ROOT, and a
    root may spawn. A store that answered empty instead of raising would
    turn a database outage into a fleet-wide grant of spawn authority.
    """
    from scitex_dev.store import Store, WriterPolicy, host_store

    schema = lineage_schema()
    return Store(
        _with_connect_timeout(
            host_store(pkg="scitex_agent_container", name=schema.name)
        ),
        schema,
        node=socket.gethostname(),
        # MULTI_WRITER as declared — a brokered spawn is legitimately
        # written by either end; see the module docstring.
        writer_policy=WriterPolicy.MULTI_WRITER,
        actor=ACTOR,
    )


def open_lineage_store() -> "Store":
    """The process's shared lineage store. DO NOT ``close()`` the result.

    ONE HANDLE PER PROCESS, BECAUSE THIS IS THE ACL PATH
    ====================================================
    ``derive_group`` and ``sender_target_relationship`` run per MESSAGE
    (``check_send_acl``), and ``descendants_of`` runs per agent-CRUD request
    (``check_lineage_acl``). Paying a 10.7 ms connect on each is not a cost
    the ACL can carry — see the module docstring for the measurement.

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
    store. :func:`reset_lineage_store` is the explicit hook for anything
    that needs to drop the handle without changing the target.
    """
    global _HANDLE, _HANDLE_TARGET

    from scitex_dev.store import host_store

    target = _with_connect_timeout(
        host_store(pkg="scitex_agent_container", name=LINEAGE_STORE)
    )
    with _HANDLE_LOCK:
        if _HANDLE is not None and _HANDLE_TARGET == target:
            return _HANDLE
        _close_handle_locked()
        handle = new_lineage_store()
        _HANDLE, _HANDLE_TARGET = handle, target
        return handle


def reset_lineage_store() -> None:
    """Drop the shared handle, closing it. Idempotent.

    The reset hook for anything that changes where the store resolves to
    WITHOUT changing ``SCITEX_STORE_DSN`` (a changed DSN invalidates the
    cache on its own — see :func:`open_lineage_store`). Tests use it in a
    real fixture teardown; the ecosystem bans ``monkeypatch``, so the hook
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


def lineage_edge_as_dict(row: "Row") -> dict[str, Any]:
    """One stored edge in the shape the SQLite row had.

    The three columns the table declared, and nothing derived: unlike the
    directory, this table never carried a hand-rolled ``updated_at`` or
    ``source_host`` for the primitive to replace.
    """
    values = row.values
    return {
        "child_name": str(values["child_name"]),
        "parent_name": str(values["parent_name"]),
        "created_at": float(values["created_at"]),
    }


@dataclass(frozen=True)
class LineageEdges:
    """The whole spawn DAG, indexed both ways, from ONE store read.

    WHY AN INDEX RATHER THAN A QUERY PER QUESTION. ``Store`` exposes
    ``get`` (by identity) and ``rows`` (everything) and no query-by-field
    verb, so "who are X's children" cannot be pushed to the server the way
    ``WHERE parent_name = ?`` was. Reading the table once and indexing it in
    Python is not a workaround for that: it is FEWER round-trips than the
    SQLite version made. ``derive_group`` issued up to three statements,
    ``sender_target_relationship`` two, and ``descendants_of`` one PER BFS
    LEVEL; each is now one read.

    The table is small by construction — one row per agent that has ever
    been spawned by another agent, 23 across the whole fleet when it was
    measured on 2026-08-28 — and it is bounded by the number of agents, not
    by traffic. If that ever stops being true the fix is a query verb on the
    primitive, not a cache here.
    """

    #: child → parent. The edge, exactly as stored.
    parent_of: dict[str, str]
    #: parent → its direct children. Derived from :attr:`parent_of`.
    children_of: dict[str, set[str]]

    def children(self, name: str) -> set[str]:
        """``name``'s direct children; empty for a leaf or an unknown name."""
        return set(self.children_of.get(name, ()))

    def parent(self, name: str) -> "str | None":
        """``name``'s parent, or ``None`` for a root or an unknown name."""
        return self.parent_of.get(name)


def read_edges() -> LineageEdges:
    """Every live edge, indexed both ways. One round-trip.

    Hidden records are EXCLUDED. Nothing hides a lineage edge in normal
    operation — the spawn DAG is append-only — but the rename flow retires a
    child's old identity by hiding it, and a hidden edge must not keep
    conferring group membership on a name that no longer exists.
    """
    rows = run_with_reconnect(lambda store: store.rows())
    parent_of: dict[str, str] = {}
    children_of: dict[str, set[str]] = {}
    for row in rows:
        values = row.values
        child = str(values["child_name"])
        parent = str(values["parent_name"])
        parent_of[child] = parent
        children_of.setdefault(parent, set()).add(child)
    return LineageEdges(parent_of=parent_of, children_of=children_of)


def parent_name_of(name: str) -> "str | None":
    """``name``'s parent, by IDENTITY lookup. ``None`` for a root.

    The single-question form of :meth:`LineageEdges.parent`, and the one to
    use when the parent is the ONLY thing wanted: ``child_name`` is the
    identity, so this is an indexed point read rather than a table scan.
    ``spawn_allowed`` and ``sender_target_relationship`` ask exactly this
    and nothing else.

    A HIDDEN edge reads as ``None`` — deliberately, and it is the reason
    this does not pass ``include_hidden``. A retired edge must not keep
    conferring the child status that a live one does.
    """
    if not name:
        return None
    row = run_with_reconnect(lambda store: store.get({"child_name": name}))
    return None if row is None else str(row.values["parent_name"])

# EOF
