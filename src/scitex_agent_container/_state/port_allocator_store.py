"""The ``a2a_ports`` claim ledger — storage adapter, on PostgreSQL only.

Extracted from :mod:`.port_allocator` so that module stays under the per-file
line cap, the same way :mod:`.state_db_grants` was carved out of
:mod:`.state_db_nodes`. The split is by RESPONSIBILITY rather than by size
alone: this file knows how a claim is STORED, and ``port_allocator`` knows
which port an agent should get. Its surface is re-exported from
``port_allocator`` so the existing import sites are unchanged.

WHY THIS LEDGER IS ON THE SHARED STORE
========================================
The operator's 2026-08-19 order was to move every table to
PostgreSQL: "fail fast, fail loud, no fallbacks". ``a2a_ports`` moves the way
``verdict_delivered``, ``incarnations``, ``pending_prompts``,
``inbound_dispatches`` and ``comms_grants`` moved before it — by ADOPTING
:mod:`scitex_dev.store`, the fleet's own store primitive. There is nothing to
fall back TO: a host whose PostgreSQL is unreachable raises
``StoreTargetError`` naming the DSN it could not reach, which is the honest
outcome — a port claim nobody else can read is worse than a launch that
refuses. ``db_path`` IS GONE from every public function; test isolation comes
from the shared ``pg_schema`` fixture pointing ``SCITEX_STORE_DSN`` at a
throwaway schema, which exercises the real resolver.

THE IDENTITY IS THE PORT — AND WHY THAT ALONE PROTECTS NOTHING
==============================================================
``port`` is the sole IDENTITY field: the invariant this ledger exists to hold
is ``UNIQUE(port)``, and keyed on ``name`` two agents claiming one port would
simply be two records (the v0.21.19 collision, back through a different
door). But identity does NOT make the claim exclusive by construction, and
PR #1243's review measured why: ``record_key()`` joins identity values into
``_record``, the rows-table PRIMARY KEY — and the rows write is a per-field
UPSERT (``_apply._apply_upsert``), so that PK never raises. Two processes
racing ``NEW_RECORD`` on one port can both pass ``check_revision`` (a
Python-side compare under a ``threading.RLock`` no other process sees) and
the second write MERGES silently. ``RevisionMismatchError`` catches only the
sequential loser. The store is convergent, not exclusive — so exclusion has
to come from the CLAIM PROTOCOL in :func:`try_claim`, whose read-back makes
the concurrent loser learn it lost. See that function for the contract.

A RELEASED PORT IS A TOMBSTONE, AND THAT IS THE TRAP
====================================================
``Store.hide`` is the only removal this store offers; there is no delete. So
``release_port`` HIDES the claim, and a hidden record still OCCUPIES the
store identity. The two doors then disagree, which
``tests/.../test_port_allocator_pin_reclaim.py`` measured before this
migration was written:

  * ``get(key)`` answers ``None``            -> the record reads as ABSENT
  * ``put(key, NEW_RECORD)`` raises          -> the identity is TAKEN

Handled naively that is fleet-down, not merely a wrong answer: every ordinary
restart of a pinned agent runs ``release_port`` then ``claim_port`` on the
SAME port, and a guard that rejects the tombstone tells the operator the port
is ``already claimed by 'alpha'`` — by alpha itself. A HIDDEN ROW THEREFORE
MEANS THE PORT IS FREE, and :func:`try_claim` takes it over with a
revision-guarded ``unhide`` followed by a fresh ``put`` and the same
read-back every claim path ends with.

WHY ``claimed_by`` IS NOT ``MergeRule.IMMUTABLE`` (measured, 2026-08-28)
========================================================================
The claim protocol settled on PR #1243 (comment 5451759350) specified
``claimed_by`` as IMMUTABLE so a differing concurrent value is REPORTED as a
``MergeConflict`` instead of quietly picked. Measured at scitex-dev 0.56.8
source, IMMUTABLE does more than that: ``_merge.merge_field`` keeps the
first-stamped value on EVERY later differing write — it never consults the
stamps, so "concurrent" and "sequential" are not distinguished ("First value
wins forever", ``_policy.MergeRule``). Under IMMUTABLE a released port could
never be re-claimed by a DIFFERENT agent: the takeover ``put`` would be
rejected by the merge (verified against a real store: read-back still names
the first claimant, with the conflict only reported in
``PutResult.conflicts`` — nothing raises), so every port ever claimed would
be burned to its first claimant forever. That violates the one invariant
this module must not lose — a released port MUST stay re-claimable, by
anyone. LAST_WRITER_WINS is therefore correct for a LEASE: the newest claim
is the live one, and the loudness IMMUTABLE was meant to buy comes from the
mandatory read-back instead, which works under any merge rule.

THE STORE HANDLE IS CACHED PER PROCESS (card
store-connect-cost-per-call-20260828)
============================================
``Store.__init__`` pays a psycopg connect (measured 10.7 ms — 159x the old
local open) plus a schema advisory lock and two catalogue probes on EVERY
construction, and port allocation sits on the agent-start path. So the
module holds ONE Store per (resolved target, pid) behind a lock —
:func:`port_store` — instead of constructing per call. The key includes the
resolved TARGET so a test repointing ``SCITEX_STORE_DSN`` (the ``pg_schema``
fixture does, per test) gets a fresh handle without any hook, and the pid so
a forked child (the concurrency tests use ``multiprocessing``) never reuses
— or worse, closes — the parent's connection through an inherited fd.
:func:`_reset_store_cache` is the explicit reset for tests; no monkeypatch.
"""

from __future__ import annotations

import logging
import threading
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from scitex_dev.store import Store

#: The logical store name. Kept as the old table name so the ledger stays
#: greppable across the migration and in operator muscle memory.
#: ``scitex_dev.store`` renders it as four physical tables (``<name>_rows``,
#: ``_oplog``, ``_identity``, ``_cursor``).
logger = logging.getLogger(__name__)

STORE_NAME = "a2a_ports"

#: Recorded on every write as the acting component.
ACTOR = "scitex-agent-container"

__all__ = [
    "ACTOR",
    "STORE_NAME",
    "claim_values",
    "holder_of",
    "init_port_schema",
    "live_claims",
    "open_port_store",
    "port_store",
    "port_store_target",
    "try_claim",
]


def _schema() -> Any:
    """The claim-ledger schema.

    Built lazily so importing this module does not import scitex-dev; the
    original was equally lazy about ``state_db``, for the same reason
    (import cost off the hot path).

    ``port`` is the sole IDENTITY (the store requires IMMUTABLE on
    identities): a claim on a different port IS a different record.

    ``claimed_by`` and ``claimed_at`` are LAST_WRITER_WINS, NOT the
    IMMUTABLE the settled protocol named — the module docstring carries the
    measured reason. A port is a REUSABLE resource: the whole point of
    ``release_port`` is that the next claimant writes its own name over the
    record the previous holder left, and IMMUTABLE keeps the FIRST value
    forever (sequential writes included), which would burn every released
    port to its first claimant. The loud-lost-race property lives in
    :func:`try_claim`'s mandatory read-back instead.

    ``claimed_at`` is epoch REAL, not the ISO text the original column held.
    Every migrated timestamp column across ``_state`` is REAL, and the only
    consumer is ``sac ports --json``, which passes the value straight
    through.
    """
    from scitex_dev.store import FieldKind, FieldPolicy, FieldRole, MergeRule, Schema

    def ident(kind: Any) -> Any:
        return FieldPolicy(
            kind=kind,
            role=FieldRole.IDENTITY,
            required=True,
            merge=MergeRule.IMMUTABLE,
            indexed=False,
        )

    def lease(kind: Any, *, indexed: bool = False) -> Any:
        return FieldPolicy(
            kind=kind,
            role=FieldRole.DATA,
            required=True,
            merge=MergeRule.LAST_WRITER_WINS,
            indexed=indexed,
        )

    return Schema(
        name=STORE_NAME,
        fields={
            "port": ident(FieldKind.INTEGER),
            # Indexed because every lookup in this ledger goes through it —
            # "which port does this agent hold" inverts into a scan when the
            # identity is the port.
            "claimed_by": lease(FieldKind.TEXT, indexed=True),
            "claimed_at": lease(FieldKind.REAL),
        },
    )


def port_store_target() -> Any:
    """Resolve WHERE the claims live. Pure — does not connect."""
    from scitex_dev.store import host_store

    return host_store(pkg="scitex_agent_container", name=STORE_NAME)


def open_port_store() -> "Store":
    """Open a FRESH claim-ledger handle. RAISES if PostgreSQL is unreachable.

    The caller owns closing it. This is the constructor for callers that
    need a handle of their own (the migration script closes one around its
    batch); the allocator's own functions go through the per-process cache
    in :func:`port_store` instead, so the agent-start path does not pay the
    connect per call.

    MULTI_WRITER, for the reason ``state_db_grants`` gives about its own
    store: a claim has no single stable owner. It is written by the host that
    starts the agent and released by whoever stops it, and a cross-host
    ``sac agents stop`` is routine — under SINGLE_WRITER that ordinary stop
    would be an illegal write.
    """
    import socket

    from scitex_dev.store import Store, WriterPolicy

    return Store(
        port_store_target(),
        _schema(),
        node=socket.gethostname(),
        writer_policy=WriterPolicy.MULTI_WRITER,
        actor=ACTOR,
    )


#: ``(StoreTarget, pid, Store)`` — see the module docstring's cache
#: section. Guarded by ``_STORE_LOCK``; reset with :func:`_reset_store_cache`.
_STORE_CACHE: "tuple[Any, int, Store] | None" = None
_STORE_LOCK = threading.Lock()


def port_store() -> "Store":
    """The per-process cached claim ledger. Do NOT close the result.

    Keyed by the RESOLVED target and the pid (module docstring says why:
    per-call construction costs a 10.7 ms psycopg connect on the agent-start
    path — card store-connect-cost-per-call-20260828 — while the
    ``pg_schema`` fixture and forked test processes both invalidate a naive
    singleton). ``Store`` serialises its own operations internally, so one
    shared handle per process is safe for concurrent threads.

    The key is the ``StoreTarget`` VALUE, not ``str(locator)`` — measured:
    the locator's string form is a redacted description that drops the
    DSN's query, so two ``pg_schema`` DSNs differing only in
    ``?options=-csearch_path`` stringify identically and a string-keyed
    cache would hand the second test the first test's dropped schema.
    ``StoreTarget`` is a frozen dataclass whose equality carries the full
    DSN, so it is the honest key.
    """
    global _STORE_CACHE
    import os

    target = port_store_target()
    pid = os.getpid()
    with _STORE_LOCK:
        if _STORE_CACHE is not None:
            cached_key, cached_pid, cached = _STORE_CACHE
            if cached_key == target and cached_pid == pid:
                if not _handle_is_closed(cached):
                    return cached
                # The peer closed the connection under the cache (a pooler
                # or server restart, an idle cut) and psycopg has already
                # marked it closed. Every later call would raise
                # "the connection is closed" forever — measured 2026-09-05:
                # the tui-bridge-supervisor skipped every tick for two hours
                # on two hosts (card sac-tui-bridge-supervisor-skips-every-
                # tick-after-its-db-connection-closes-20260905). A closed
                # handle is not a handle; reopen and say so once.
                logger.warning(
                    "port_store: the cached claim-ledger connection is "
                    "closed (pid %d); reopening it",
                    pid,
                )
            # A fork inherited the parent's connection through the same fd:
            # closing it HERE would send a termination on the parent's
            # socket. Only the same process that opened a handle may close
            # it; a stale-target handle in the same process is closed so the
            # fd does not leak per test.
            if cached_pid == pid:
                cached.close()
        fresh = open_port_store()
        _STORE_CACHE = (target, pid, fresh)
        return fresh


def _handle_is_closed(store: "Store") -> bool:
    """True when the store's psycopg connection reports itself closed.

    A LOCAL check, no round trip: psycopg sets ``connection.closed`` the
    moment an operation finds the peer gone, and the message every later
    call raises ("the connection is closed") is exactly this flag. A
    handle with no such attribute (a dialect that is not psycopg) is
    treated as open — this guard exists for the one failure that was
    measured, not to second-guess every backend.
    """
    connection = getattr(store, "_connection", None)
    return bool(getattr(connection, "closed", False))


def _reset_store_cache() -> None:
    """Drop (and close) the cached handle. For tests — plain call, no patching."""
    global _STORE_CACHE
    import os

    with _STORE_LOCK:
        if _STORE_CACHE is not None and _STORE_CACHE[1] == os.getpid():
            _STORE_CACHE[2].close()
        _STORE_CACHE = None


def init_port_schema() -> str:
    """Create the claim tables if missing. Idempotent.

    Returns the resolved store LOCATOR as a string — the PostgreSQL
    equivalent of the ``Path`` the schema helper worked against, and
    useful in exactly the same way: it names WHERE the claims actually went,
    so an operator can check rather than assume.
    """
    port_store()  # Store.__init__ creates the tables when absent.
    return str(port_store_target().locator)


def claim_values(port: int, agent_name: str, claimed_at: float) -> dict[str, Any]:
    """The full record a claim writes. One place, so the shape cannot drift."""
    return {"port": int(port), "claimed_by": agent_name, "claimed_at": claimed_at}


def live_claims(store: "Store") -> dict[int, str]:
    """``{port: holder}`` for every LIVE claim, in ONE read.

    ``rows()`` excludes hidden records by default, which IS the
    released-claim filter — spelled out because that exclusion is
    load-bearing here rather than incidental.
    """
    return {
        int(row.values["port"]): str(row.values["claimed_by"]) for row in store.rows()
    }


def holder_of(store: "Store", port: int) -> str | None:
    """Which agent LIVES on ``port``, or ``None`` when it is free.

    A hidden (released) claim reads as free, which is what "released" means.
    """
    row = store.get({"port": int(port)})
    return None if row is None else str(row.values["claimed_by"])


def _claim_confirmed(store: "Store", port: int, agent_name: str) -> bool:
    """MANDATORY read-back: did OUR claim actually land?

    NOT optional, and not belt-and-braces — under merge semantics a ``put``
    that "succeeded" proves nothing. Rows writes are per-field UPSERTs, so a
    concurrent same-port claim never trips the PK and never raises: the
    losing write simply merges (or is overwritten by the later
    materialisation), with at most a ``MergeConflict`` REPORTED in
    ``PutResult.conflicts``. The only way the concurrent loser LEARNS it
    lost is to read the record back from the store both writers converge on
    and check whose name stands. A claim path that skips this hands two
    agents the same port and tells neither.
    """
    row = store.get({"port": int(port)})
    return row is not None and str(row.values["claimed_by"]) == agent_name


def try_claim(store: "Store", *, port: int, agent_name: str, now: float) -> bool:
    """Attempt an exclusive claim on ``port``. ``True`` iff we won it.

    Losing is a NORMAL outcome here, not an error — exactly as
    ``ON CONFLICT DO NOTHING`` treated it — so a lost race is reported as
    ``False`` and the caller moves to the next candidate.

    THE CLAIM PROTOCOL (PR #1243, comment 5451759350), three steps:

      1. ``put(..., expected_revision=NEW_RECORD)`` — the SEQUENTIAL loser
         is caught here as ``RevisionMismatchError`` (the record already
         exists) and answers ``False`` without a second look.
      2. A hidden record is a RELEASED port: take it over with ``unhide``
         guarded by the revision read BEFORE the row it decides on (the
         order is load-bearing — a competitor that moves the record after
         our reads leaves us a stale token and ``unhide`` refuses), then a
         fresh ``put``.
      3. EVERY winning path ends in :func:`_claim_confirmed` — the
         mandatory read-back. Step 1 succeeding proves nothing for the
         CONCURRENT case: two ``NEW_RECORD`` puts racing through the
         revision check (it is a Python-side compare, per process) both
         "succeed" and merge silently. The read-back is where the loser
         finds out, deterministically, from the shared store.

    ``Store.revision`` is the accessor, NOT ``Row.seq``: those are different
    columns (``_revision`` and ``_seq``), and only the former is what
    ``check_revision`` compares against.

    Between the winning ``unhide`` and the ``put`` that stamps our name, the
    record is briefly LIVE under the PREVIOUS holder's name. A competing
    claimant reads that as held and correctly loses; a reader (``get_port``,
    ``list_claims``) sees a stale name for that instant. That is the honest
    cost of a two-call takeover, and it is strictly better than the
    alternative, which is two agents holding one port.
    """
    from scitex_dev.store import (
        ANY_REVISION,
        NEW_RECORD,
        RecordNotFoundError,
        RevisionMismatchError,
    )

    key = {"port": int(port)}
    revision = store.revision(key)
    current = store.get(key, include_hidden=True)

    if revision is None or current is None:
        try:
            store.put(claim_values(port, agent_name, now), expected_revision=NEW_RECORD)
        except RevisionMismatchError:
            return False
        return _claim_confirmed(store, port, agent_name)

    if current.hidden is not True:
        return False

    try:
        store.unhide(key, expected_revision=revision, actor=ACTOR)
    except (RecordNotFoundError, RevisionMismatchError):
        return False
    store.put(claim_values(port, agent_name, now), expected_revision=ANY_REVISION)
    return _claim_confirmed(store, port, agent_name)
