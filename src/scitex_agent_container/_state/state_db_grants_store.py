#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# File: src/scitex_agent_container/_state/state_db_grants_store.py
"""The ``comms_grants`` store — schema, connection, and the shared handle.

Split out of :mod:`.state_db_grants` (which stays the import surface
:mod:`.state_db_nodes` re-exports) the same way
:mod:`.state_db_comms_nodes_store` is split out of its sibling. Nothing here
is for a caller outside ``_state``: the schema declaration, the ``Store``
factory, the process-wide handle, and the reconnect wrapper every verb runs
through.

WHY THIS FILE EXISTS AT ALL — IT WAS THE ONE ACL STORE WITHOUT A RECONNECT
=========================================================================
``comms_grants``, ``comms_nodes``, ``node_comms_policy``, ``lineage`` and
``instances`` are all read from the send path or the agent-CRUD path. Four
of them already opened through a cached handle wrapped in
:func:`run_with_reconnect`; grants opened a fresh ``Store`` per call and had
no reconnect at all. That asymmetry was not a design decision, it was the
order the ports landed in, and it costs twice:

* ``psycopg.connect`` is 10.707 ms against ``sqlite3.connect``'s 0.067 ms
  (159x, measured on the live primary, card
  ``sqlite-out-per-call-connect-cost-20260828``). :func:`.has_grant` is
  called by ``_listen._acl.check_send_acl`` for every cross-group message,
  so the per-call open was paying that connect on the ACL path.
* a connection dying UNDER a cached handle is the failure the target-keyed
  cache cannot heal by itself — psycopg3 never reconnects and neither does
  ``Store``, so one PostgreSQL restart would break the grants read forever
  in a long-lived ``sac listen`` daemon.

``_open`` SURVIVES IN THE SIBLING MODULE, AND RETURNS A FRESH STORE
===================================================================
``state_db_grants._open`` is imported by name by
``scripts/migrate_comms_grants_to_postgres.py`` and by three tests, all of
which ``close()`` what they are handed. Pointing that name at the SHARED
handle would make every one of those closes break the process's ACL reads.
So it keeps meaning what it has always meant — a fresh, caller-owned
connection — and is :func:`new_grants_store` under a stable name. The
shared handle is reached through :func:`open_grants_store` /
:func:`run_with_reconnect`, which nothing outside ``_state`` calls.
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
    "GRANTS_STORE",
    "grant_as_dict",
    "grant_key",
    "grants_schema",
    "hlc_sort_key",
    "new_grants_store",
    "open_grants_store",
    "reset_grants_store",
    "run_with_reconnect",
]

#: Logical store name. Renders as four physical tables
#: (``comms_grants_rows``, ``_oplog``, ``_identity``, ``_cursor``).
GRANTS_STORE = "comms_grants"

ACTOR = "scitex-agent-container"

#: Operator override for the libpq connect timeout, in seconds.
CONNECT_TIMEOUT_ENV = "SAC_GRANTS_CONNECT_TIMEOUT_S"

#: Seconds libpq may spend establishing a connection before giving up.
#:
#: NOT a tuning knob, for the reason :mod:`.state_db_comms_nodes_store`
#: gives: ``has_grant`` runs inside ``check_send_acl`` while the listen
#: daemon is serving a request, so a connect with libpq's default (wait
#: forever) turns a BLACKHOLED primary into a stalled handler — no answer at
#: all rather than a slow denial. A dead-but-reachable host RSTs at once and
#: was never the danger; a host that swallows SYN is.
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
    gone, and every future call on that handle raises the same thing forever.
    """
    try:
        import psycopg
    except ImportError:  # pragma: no cover - the Postgres path needs psycopg
        return False
    return isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError))


def run_with_reconnect(operation: "Callable[[Store], Any]") -> Any:
    """Run ``operation`` against the shared handle; reopen ONCE if it died.

    RETRY ONCE, NOT IN A LOOP. A second failure is a real outage and must
    reach the caller — a grants read that silently answered "no grant" would
    be a DENIAL invented by an outage, and a grants read that silently
    answered "granted" would be worse.

    THE HONEST CAVEAT: a retry re-runs the operation. The readers are pure.
    ``grant_send`` and ``revoke_send`` are get-then-write and idempotent by
    construction — a re-run reads the row the first attempt may have
    committed and returns without writing again. The rename step in
    :mod:`.state_db_grants_rename` is idempotent in the same way: with
    nothing live under the old name it moves nothing.
    """
    try:
        return operation(open_grants_store())
    except Exception as exc:
        if not _is_connection_lost(exc):
            raise
        reset_grants_store()
        return operation(open_grants_store())


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
    """A granted permission is a historical fact — IMMUTABLE.

    ``created_at`` records WHEN the permission was given; a merge that could
    move it would rewrite the audit trail an operator reads to answer "since
    when could this agent send there?". The same applies to ``note``, which
    names the authorisation.
    """
    from scitex_dev.store import FieldPolicy, FieldRole, MergeRule

    return FieldPolicy(
        kind=kind,
        role=FieldRole.DATA,
        required=required,
        merge=MergeRule.IMMUTABLE,
        indexed=False,
    )


def grants_schema() -> Any:
    from scitex_dev.store import FieldKind, Schema

    return Schema(
        name=GRANTS_STORE,
        fields={
            # The directed pair IS the identity, exactly as the SQLite
            # (sender_name, target_name) lookup treated it.
            "sender_name": _ident(FieldKind.TEXT),
            "target_name": _ident(FieldKind.TEXT),
            "created_at": _fact(FieldKind.REAL, required=True),
            "note": _fact(FieldKind.TEXT),
        },
    )


#: The process-wide handle and the target it was opened for. Guarded by
#: ``_HANDLE_LOCK``; see :func:`open_grants_store` for why it exists.
_HANDLE_LOCK = threading.Lock()
_HANDLE: "Store | None" = None
_HANDLE_TARGET: "StoreTarget | None" = None


def new_grants_store() -> "Store":
    """Construct a FRESH, caller-owned handle. RAISES if PostgreSQL is down.

    For code that legitimately wants its own connection and will close it —
    the one-shot data migration and the tests that read a hidden row back.
    Ordinary readers and writers want :func:`run_with_reconnect`.

    MULTI_WRITER, deliberately. A grant's record has no single stable owner:
    it is created on one host, revoked by an operator from another, and
    bulk-imported from peers by ``state_db_export``. Under SINGLE_WRITER the
    first revoke-from-elsewhere would be an illegal write.

    Raising is the security property: :func:`.has_grant` reads a missing
    record as DENY, so a store that answered empty instead of raising would
    turn an outage into a fleet-wide denial of every cross-group send — and
    the listing an operator audits with would go quietly blank.
    """
    from scitex_dev.store import Store, WriterPolicy, host_store

    schema = grants_schema()
    return Store(
        _with_connect_timeout(
            host_store(pkg="scitex_agent_container", name=schema.name)
        ),
        schema,
        node=socket.gethostname(),
        writer_policy=WriterPolicy.MULTI_WRITER,
        actor=ACTOR,
    )


def open_grants_store() -> "Store":
    """The process's shared grants store. DO NOT ``close()`` the result.

    THE CACHE IS KEYED ON THE RESOLVED TARGET, NOT ON "have we opened one".
    ``host_store`` re-resolves ``SCITEX_STORE_DSN`` on every call and is
    cheap (an env read and a frozen dataclass — it does not connect), so the
    key costs nothing and buys correctness: a changed DSN yields a different
    ``StoreTarget``, which swaps the handle instead of silently serving the
    previous database. That is not hypothetical — the suite's ``pg_schema``
    fixture points the variable at a fresh throwaway schema per test, and a
    cache that ignored it would hand every test the first test's store.
    :func:`reset_grants_store` is the explicit hook for anything that needs
    to drop the handle without changing the target.
    """
    global _HANDLE, _HANDLE_TARGET

    from scitex_dev.store import host_store

    target = _with_connect_timeout(
        host_store(pkg="scitex_agent_container", name=GRANTS_STORE)
    )
    with _HANDLE_LOCK:
        if _HANDLE is not None and _HANDLE_TARGET == target:
            return _HANDLE
        _close_handle_locked()
        handle = new_grants_store()
        _HANDLE, _HANDLE_TARGET = handle, target
        return handle


def reset_grants_store() -> None:
    """Drop the shared handle, closing it. Idempotent.

    The reset hook for anything that changes where the store resolves to
    WITHOUT changing ``SCITEX_STORE_DSN``. Tests use it in a real fixture
    teardown; the ecosystem bans ``monkeypatch``, so the hook is a plain
    function rather than an attribute anyone reaches in and rewrites.
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


def grant_key(values: Any) -> dict[str, Any]:
    """The identity of one grant record, as ``Store.get``/``hide`` want it."""
    return {
        "sender_name": str(values["sender_name"]),
        "target_name": str(values["target_name"]),
    }


def hlc_sort_key(row: "Row") -> tuple:
    """Total order over records, immune to wall-clock skew.

    The successor to the SQLite ``rowid`` ordering. ``node`` is the final
    tiebreak so the order is total rather than merely partial — two origins
    can mint the same (wall_us, logical) pair.
    """
    hlc = row.hlc
    return (hlc.wall_us, hlc.logical, hlc.node)


def grant_as_dict(row: "Row") -> dict[str, Any]:
    """One stored grant in the shape ``list_comms_grants`` returns."""
    values = row.values
    return {
        "sender": str(values["sender_name"]),
        "target": str(values["target_name"]),
        "created_at": float(values["created_at"]),
        "note": values.get("note"),
    }

# EOF
