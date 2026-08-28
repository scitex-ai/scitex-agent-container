"""The ``sac_channel_events`` store — DSN resolution, DDL, and the handle.

Split out of :mod:`.state_db_channel` (which stays the import surface every
caller uses) to keep both files under the per-file line cap. This half holds
nothing a caller outside ``_state`` should reach for: the two ``CREATE
TABLE`` statements, the cached psycopg connection, and the reconnect wrapper.

PLAIN POSTGRESQL TABLES, NOT ``scitex_dev.store`` — AND WHY
===========================================================
Every other table sac moved off SQLite in August 2026 adopted the fleet's
own ``scitex_dev.store`` primitive. This one deliberately does NOT, and the
decision is recorded in ``docs/adr/0023-channel-events-plain-postgres.md``
with the measurements behind it. The short form, three disqualifiers, each
sufficient on its own:

1. ``PeerState.next_seq()`` is O(oplog) per write — a measured ``EXPLAIN``
   shows Seq Scan / GroupAggregate over the whole oplog, and the store never
   deletes. Minting a cursor through a counter RECORD would pay that scan
   TWICE per event, so the cost of sending a message would grow with the
   number of messages ever sent, on every host, forever.
2. The oplog sequence is per-ORIGIN, not per-target (measured: three
   counters in one store). The SSE ``id:`` line is a PER-TARGET cursor, so
   an origin-scoped sequence interleaves two agents' numbering — exactly the
   silent skip/replay that :data:`.._store_plugin.NEVER_SYNCED` refuses this
   table's replication for.
3. ``store.rows()`` is the only read primitive: full decode plus a Python
   filter. Measured 16.3 ms at 766 rows and ~190 ms at 9 k — paid on every
   SSE connect, on the event loop.

The operator's rule is NO SQLITE. Plain tables in the SAME database
``host_store()`` resolves to satisfy it: one shared PostgreSQL, no per-host
file, no divergent truth. What they give up is the store's replication
machinery, which this table refuses anyway.

EXIT CRITERION, so this is a decision and not a fork: if ``scitex_dev.store``
ever grows (a) a FILTERED read that does not decode every row and (b) a
retention verb that can delete, this table comes back inside the store. Both
are named in the ADR.

WHY THE ID IS A COUNTER ROW AND NOT ``BIGSERIAL``
=================================================
A PostgreSQL sequence is NON-TRANSACTIONAL by design: ``nextval`` does not
take part in the surrounding transaction, so with two concurrent writers on
one target, id ``N+1`` can COMMIT and become visible before ``N`` does. A
reader doing ``WHERE id > cursor ORDER BY id`` then ships ``N+1``, advances
its cursor past it, and never returns ``N`` — a silent drop, with no error
anywhere, that SQLite's single serialised writer could not produce.

The counter row makes commit order and id order the same thing: the
``ON CONFLICT ... DO UPDATE`` takes a row lock on ``(target)`` that is held
until the transaction commits, so a second writer for the SAME target blocks
until the first has inserted its event. Writers for DIFFERENT targets touch
different rows and never contend, so the serialisation is exactly as narrow
as the invariant requires.

``meta_json`` IS TEXT AND NEVER TOUCHES A JSON CODEC
====================================================
The column is declared ``TEXT``, not ``json``/``jsonb``, and the value
stored is the exact ``json.dumps(event, ensure_ascii=False)`` string the
caller minted. Routing it through a codec would break the byte-identity the
replay path depends on in three separate ways: ``sort_keys=True`` reorders
keys so a replayed frame no longer matches the live one, ``ensure_ascii=True``
mangles Japanese content into escapes, and ``default=str`` silently
stringifies the values ``persist_event`` currently raises on. ``jsonb`` would
additionally normalise whitespace, drop duplicate keys and re-order the
object. A frame that is *nearly* the same is the worst outcome available: it
looks delivered.

ONE HANDLE PER PROCESS, LIKE ``comms_nodes`` AND FOR THE SAME MEASUREMENT
=========================================================================
This module's machinery — :func:`_with_connect_timeout`,
:func:`_is_connection_lost`, :func:`run_with_reconnect`, and the
clear-the-reference-before-close in :func:`_close_handle_locked` — is COPIED
from :mod:`.state_db_comms_nodes_store`, which arrived at each piece by
measurement. ``psycopg.connect`` costs 10.707 ms against SQLite's 0.067 ms
(159x, measured on the live primary), and this table is written once per
message and read once per SSE connect, so a per-call connect is not a cost it
can pay either.

The one place this module IMPROVES on the copied caveat is the retry: every
operation here is a single ``with conn.transaction()`` block, so a connection
that dies mid-operation rolls the whole thing back and the retry cannot
double-apply an insert. ``comms_nodes`` had to accept that risk; this one does
not have it.
"""

from __future__ import annotations

import os
import threading
from typing import TYPE_CHECKING, Any, Callable

if TYPE_CHECKING:  # pragma: no cover - typing only
    import psycopg

    from scitex_dev.store import StoreTarget

__all__ = [
    "CHANNEL_STORE",
    "CONNECT_TIMEOUT_ENV",
    "channel_store_locator",
    "init_channel_schema",
    "new_channel_connection",
    "open_channel_connection",
    "reset_channel_connection",
    "run_with_reconnect",
]

#: Logical store name, used ONLY to resolve the target through
#: ``host_store``. Unlike the ``scitex_dev.store`` modules this does NOT
#: render as a set of physical tables — the two tables below are named
#: explicitly and live in the same database.
CHANNEL_STORE = "channel_events"

#: Operator override for the libpq connect timeout, in seconds.
CONNECT_TIMEOUT_ENV = "SAC_CHANNEL_CONNECT_TIMEOUT_S"

#: Seconds libpq may spend establishing a connection before giving up.
#:
#: Same judgement, same number, same reason as
#: ``state_db_comms_nodes_store.DEFAULT_CONNECT_TIMEOUT_S``: this store is
#: read on the SSE connect path and written on the a2a publish path, both of
#: which run inside ``sac listen``. libpq's default is "wait forever", which
#: turns a BLACKHOLED primary into a stalled daemon. A dead-but-reachable
#: host RSTs immediately and was never the danger; a host that swallows SYN
#: is, and that is the ordinary shape of a machine losing power.
DEFAULT_CONNECT_TIMEOUT_S = 5

#: The advisory-lock key guarding the DDL below.
#:
#: Concurrent agent relaunch is NORMAL in this fleet — ``sac agents start``
#: on four hosts at once is a Tuesday — and two sessions issuing
#: ``CREATE TABLE IF NOT EXISTS`` against the same database race inside
#: PostgreSQL itself: ``IF NOT EXISTS`` checks the catalog before it takes
#: the lock, so the loser raises ``DuplicateTable`` rather than being a
#: no-op. A session-independent advisory lock makes the whole DDL block
#: mutually exclusive, which ``IF NOT EXISTS`` on its own does not.
#:
#: The value is arbitrary but must be STABLE and must not collide with
#: another consumer's lock in the same database. It is the low 63 bits of a
#: hash of the table name, computed once and pinned here rather than derived
#: at runtime, so a change to the hash function cannot silently move the lock
#: out from under a running fleet.
DDL_ADVISORY_LOCK_KEY = 7_305_244_310_022_618_113

#: The two tables. Declared here rather than in ``state_db_schema`` because
#: that module holds SQLite DDL for ``state.db``, and this is neither.
#:
#: ``PRIMARY KEY (target, id)`` is the composite the per-target cursor
#: requires: ``id`` alone is NOT unique any more, which is precisely why
#: :func:`.state_db_channel.mark_delivered` had to grow a ``target``
#: argument.
#:
#: The partial index matches the ``list_undelivered`` predicate exactly
#: (``target``, ``id``, ``WHERE delivered_at IS NULL``), so the
#: fresh-subscriber replay is an index scan over only the undelivered rows
#: rather than over the target's whole history. The full ``(target, id)``
#: ordering that ``list_since_id`` needs is served by the primary key.
_DDL = """
CREATE TABLE IF NOT EXISTS sac_channel_events (
    target        TEXT NOT NULL,
    id            BIGINT NOT NULL,
    source        TEXT,
    kind          TEXT NOT NULL DEFAULT 'message',
    content       TEXT,
    meta_json     TEXT NOT NULL,
    ts            DOUBLE PRECISION NOT NULL,
    delivered_at  DOUBLE PRECISION,
    PRIMARY KEY (target, id)
);

CREATE INDEX IF NOT EXISTS sac_channel_events_undelivered_idx
    ON sac_channel_events (target, id) WHERE delivered_at IS NULL;

CREATE TABLE IF NOT EXISTS sac_channel_cursor (
    target   TEXT PRIMARY KEY,
    next_id  BIGINT NOT NULL
);
"""


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

    Copied from :mod:`.state_db_comms_nodes_store`. The bound has to travel
    IN the DSN because that string is all this module hands to
    ``psycopg.connect``; libpq reads ``connect_timeout`` from the URI query
    string, which is the form both resolutions produce (the
    ``SCITEX_STORE_DSN`` override and the per-host socket DSN).

    An explicit ``connect_timeout`` already in the DSN is left ALONE: the
    operator who wrote it outranks this default.

    Non-Postgres targets pass through untouched — :func:`_resolve_target`
    refuses them one level up, with a message that names what it got.
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


def _resolve_target() -> "StoreTarget":
    """Where the two tables live: the database ``host_store`` resolves to.

    RESOLVED THROUGH THE SAME FUNCTION AS EVERY MIGRATED TABLE, on purpose.
    This module does not adopt ``scitex_dev.store``'s record model, but it
    absolutely does adopt its TARGET resolution — ``SCITEX_STORE_DSN`` or the
    per-host PostgreSQL, with NO SQLite fallback — so the channel history
    lands in the same database as everything else sac owns and the test
    suite's ``pg_schema`` fixture isolates it by pointing that one variable
    at a throwaway schema.

    A non-PostgreSQL target RAISES rather than degrading. There is no other
    backend this module can speak, and a resolver that answered "no events"
    from somewhere it cannot read is the exact failure the whole sqlite-out
    migration exists to remove.
    """
    from scitex_dev.store import Backend, StoreTargetError, host_store

    target = host_store(pkg="scitex_agent_container", name=CHANNEL_STORE)
    if target.backend is not Backend.POSTGRES:
        raise StoreTargetError(
            "sac_channel_events requires PostgreSQL; the resolved target is "
            f"{target.locator!r} (backend {target.backend!r}). Set "
            "SCITEX_STORE_DSN to a PostgreSQL DSN."
        )
    return _with_connect_timeout(target)


def channel_store_locator() -> str:
    """The endpoint the two tables live at, as a locator string.

    State that cannot say where it landed is state nobody can audit; every
    migrated module exposes this and the suite asserts on it.
    """
    return str(_resolve_target().locator)


def _is_connection_lost(exc: BaseException) -> bool:
    """Is ``exc`` a DEAD CONNECTION rather than a rejected operation?

    Copied from :mod:`.state_db_comms_nodes_store`. The distinction decides
    whether retrying can possibly help. A ``UniqueViolation`` is a verdict
    about the DATA and means the same thing on a fresh connection, so
    retrying it would only hide it. ``psycopg.OperationalError`` /
    ``InterfaceError`` mean the socket is gone, and every future call on that
    handle raises the same thing forever — psycopg3 never reconnects.
    """
    try:
        import psycopg
    except ImportError:  # pragma: no cover - the Postgres path needs psycopg
        return False
    return isinstance(exc, (psycopg.OperationalError, psycopg.InterfaceError))


#: Serialises OPERATIONS on the shared connection. Not the same lock as
#: ``_HANDLE_LOCK``, which only guards the cache slot.
#:
#: A psycopg connection is thread-safe for individual statements, but a
#: TRANSACTION is a property of the CONNECTION, not of the caller: two threads
#: opening ``conn.transaction()`` on one handle would land in ONE transaction
#: and either could commit the other's half-written work. Every caller here
#: runs inside a transaction, and since the ``asyncio.to_thread`` hops in the
#: SSE handlers mean several threads reach this module at once, that is not a
#: theoretical race.
#:
#: This is the SAME arrangement ``state_db_comms_nodes_store`` relies on — its
#: shared ``Store`` "carries its own internal ``RLock`` and is built to be
#: shared". That lock is simply inside the primitive there and outside it
#: here, because a raw connection has none. An ``RLock`` so a future nested
#: helper cannot self-deadlock.
#:
#: THE TRADE, STATED: one slow operation delays every other channel operation
#: in the process. It does NOT delay the event loop — that is what the
#: ``to_thread`` hops are for — so a stalled primary degrades channel
#: throughput rather than stopping the daemon. The alternative (a connection
#: per thread) multiplies the daemon's PostgreSQL connections by the size of
#: the default executor pool, which is a worse failure at fleet scale.
_OP_LOCK = threading.RLock()


def run_with_reconnect(operation: "Callable[[psycopg.Connection], Any]") -> Any:
    """Run ``operation`` against the shared handle; reopen ONCE if it died.

    WHY THIS EXISTS, MEASURED (``state_db_comms_nodes_store``, 2026-08-28)
    =====================================================================
    The cache is keyed on the resolved target, which self-heals a changed DSN
    and a failed CONNECT — neither of those leaves a handle behind. It does
    NOT heal the case that actually happens: the connection dying UNDER a
    cached handle. Verified against the live primary by killing the backend
    with ``pg_terminate_backend``: the cached handle then raised
    ``OperationalError: the connection is closed`` on every subsequent call,
    forever, while a fresh connection proved the server healthy. In the
    long-lived ``sac listen`` daemon that is one PostgreSQL restart
    permanently breaking every agent's channel until the daemon is restarted
    by hand.

    RETRY ONCE, NOT IN A LOOP. A second failure is a real outage and must
    reach the caller.

    NO DOUBLE-APPLY RISK HERE, unlike the module this was copied from. Every
    caller wraps its statements in ``with conn.transaction()``, so a server
    that dies mid-operation leaves nothing committed and the retry starts
    from a clean slate. The one shape that could double-apply — a commit the
    server completed but never acknowledged — cannot arise for the allocate
    -then-insert path, because the retry re-runs the ALLOCATION too and would
    mint a fresh id rather than colliding. That costs one skipped id, and
    gaps are explicitly allowed (readers use ``id > cursor``, never
    ``id = cursor + 1``).
    """
    with _OP_LOCK:
        try:
            return operation(open_channel_connection())
        except Exception as exc:
            if not _is_connection_lost(exc):
                raise
            reset_channel_connection()
            return operation(open_channel_connection())


#: The process-wide handle and the target it was opened for. Guarded by
#: ``_HANDLE_LOCK``; see :func:`open_channel_connection` for why it exists.
_HANDLE_LOCK = threading.Lock()
_HANDLE: "psycopg.Connection | None" = None
_HANDLE_TARGET: "StoreTarget | None" = None


def new_channel_connection() -> "psycopg.Connection":
    """A FRESH, caller-owned connection with the DDL applied. RAISES if down.

    For code that legitimately wants its own connection and will close it —
    the one-shot data migration, chiefly. Ordinary readers and writers want
    :func:`open_channel_connection`, which hands back the shared one.

    Raising is the whole point. ``list_undelivered`` returning ``[]`` means
    "this agent has nothing waiting", so a connection failure that answered
    empty would turn a database outage into every agent's inbox looking
    delivered — silently, on the replay path.

    ``autocommit=True`` with explicit ``conn.transaction()`` blocks, rather
    than psycopg's implicit-transaction default: every operation in
    :mod:`.state_db_channel` states its own atomicity, and an implicit
    transaction left open by a read would pin a snapshot in a daemon that
    lives for weeks.
    """
    import psycopg

    target = _resolve_target()
    conn = psycopg.connect(str(target.dsn), autocommit=True)
    try:
        init_channel_schema(conn)
    except Exception:
        conn.close()
        raise
    return conn


def init_channel_schema(conn: "psycopg.Connection | None" = None) -> str:
    """Create the two tables if missing. Idempotent. Returns the locator.

    Guarded by a transaction-scoped advisory lock (see
    :data:`DDL_ADVISORY_LOCK_KEY`): ``CREATE TABLE IF NOT EXISTS`` checks the
    catalog BEFORE it takes its own lock, so two agents starting at the same
    moment race and the loser raises ``DuplicateTable``. Concurrent relaunch
    is the normal operating mode of this fleet, not an edge case.

    ``pg_advisory_xact_lock`` rather than the session form: it is released by
    the commit, so a caller that crashes between the lock and the commit
    cannot strand the whole fleet's DDL behind a lock nobody holds a
    reference to.

    Passing ``conn`` runs the DDL on THAT connection (used by
    :func:`new_channel_connection`, which must not recurse into the cache).
    Passing nothing opens, applies, and closes its own — the shape every
    other ``init_*_schema`` in this package has.
    """
    if conn is not None:
        with conn.transaction():
            conn.execute(
                "SELECT pg_advisory_xact_lock(%s)", (DDL_ADVISORY_LOCK_KEY,)
            )
            conn.execute(_DDL)
        return str(_resolve_target().locator)

    owned = new_channel_connection()
    try:
        return str(_resolve_target().locator)
    finally:
        owned.close()


def open_channel_connection() -> "psycopg.Connection":
    """The process's shared connection. DO NOT ``close()`` the result.

    ONE HANDLE PER PROCESS. ``psycopg.connect`` costs 10.707 ms against
    SQLite's 0.067 ms (159x, measured on the live primary), and this table is
    written once per message; paying a connect per event would make every
    a2a send 10 ms slower for nothing. ``psycopg.Connection`` carries its own
    lock and is safe to share between threads, which is what the
    ``asyncio.to_thread`` call sites in the SSE handlers rely on.

    THE CACHE IS KEYED ON THE RESOLVED TARGET, NOT ON "have we opened one".
    ``host_store`` re-resolves ``SCITEX_STORE_DSN`` on every call and is cheap
    (an env read and a frozen dataclass — it does not connect), so the key
    costs nothing and buys correctness: a changed DSN yields a different
    target, which swaps the handle instead of silently serving the previous
    database. That is not hypothetical — the suite's ``pg_schema`` fixture
    points the variable at a fresh throwaway schema per test, and a cache
    that ignored it would hand every test the first test's tables.
    """
    global _HANDLE, _HANDLE_TARGET

    target = _resolve_target()
    with _HANDLE_LOCK:
        if _HANDLE is not None and _HANDLE_TARGET == target and not _HANDLE.closed:
            return _HANDLE
        _close_handle_locked()
        handle = new_channel_connection()
        _HANDLE, _HANDLE_TARGET = handle, target
        return handle


def reset_channel_connection() -> None:
    """Drop the shared connection, closing it. Idempotent.

    The reset hook for anything that changes where the store resolves to
    WITHOUT changing ``SCITEX_STORE_DSN`` (a changed DSN invalidates the
    cache on its own), and the recovery half of :func:`run_with_reconnect`.
    The ecosystem bans ``monkeypatch``, so this is a plain function rather
    than an attribute anyone reaches in and rewrites.
    """
    with _HANDLE_LOCK:
        _close_handle_locked()


def _close_handle_locked() -> None:
    """Drop the cached handle. Caller MUST hold ``_HANDLE_LOCK``.

    The reference is cleared BEFORE the close, so a connection that raises on
    close cannot leave a dead handle cached — which would make every later
    call fail on a connection nothing can replace.
    """
    global _HANDLE, _HANDLE_TARGET

    handle, _HANDLE, _HANDLE_TARGET = _HANDLE, None, None
    if handle is None:
        return
    try:
        handle.close()
    except Exception:  # stx-allow: fallback (reason: the handle is already dropped; a server that vanished makes close() raise, and failing here would turn a successful re-open into an error)
        pass
