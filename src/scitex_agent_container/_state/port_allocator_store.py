"""The ``a2a_ports`` claim ledger — storage adapter, on PostgreSQL only.

Extracted from :mod:`.port_allocator` so that module stays under the per-file
line cap, the same way :mod:`.state_db_grants` was carved out of
:mod:`.state_db_nodes`. The split is by RESPONSIBILITY rather than by size
alone: this file knows how a claim is STORED, and ``port_allocator`` knows
which port an agent should get. Its surface is re-exported from
``port_allocator`` so the existing import sites are unchanged.

WHY THIS LEDGER NO LONGER TOUCHES SQLite
========================================
The operator's 2026-08-19 order was to eradicate SQLite and move to
PostgreSQL: "fail fast, fail loud, no fallbacks". ``a2a_ports`` moves the way
``verdict_delivered``, ``incarnations``, ``pending_prompts``,
``inbound_dispatches`` and ``comms_grants`` moved before it — by ADOPTING
:mod:`scitex_dev.store`, the fleet's own store primitive, rather than by sac
growing a private psycopg layer.

That primitive already implements the operator's rule, in its own words at
``resolve_target``: "exactly two steps (``SCITEX_STORE_DSN`` or the per-host
Postgres) and deliberately NO SQLite fallback: a host whose Postgres is down
must fail loudly rather than start writing to a private local file that
shares nothing." So there is nothing here to fall back TO — a host whose
PostgreSQL is unreachable raises ``StoreTargetError`` naming the DSN it could
not reach, which is the honest outcome: a port claim nobody else can read is
worse than a launch that refuses.

``db_path`` IS GONE from every public function. It named a SQLite file; there
is no file. Test isolation comes from pointing ``SCITEX_STORE_DSN`` at a
throwaway schema — the shared ``pg_schema`` fixture — which is a better
isolation than a temp path was, because it exercises the real resolver.

THE IDENTITY IS THE PORT, AND THAT IS THE WHOLE DESIGN
======================================================
The obvious port of this ledger keys the store on ``name``, because
``get_port`` and ``release_port`` both look up by agent. That is wrong in a
way tests would not catch.

The invariant this ledger exists to hold is ``UNIQUE(port)`` — a DIFFERENT
column from the lookup key. Keyed on ``name``, two agents claiming port 19000
are two DIFFERENT records and the store accepts both: mutual exclusion on the
port silently gone, while every unit test still passes. That is the exact
collision the SQLite version was rewritten to stop — the v0.21.19 release died
on ``sqlite3.IntegrityError: UNIQUE constraint failed: a2a_ports.port``,
reproduced deterministically at 16 threads as 6 raw driver escapes.

So ``port`` is the sole IDENTITY field and the store's own identity
uniqueness carries the invariant STRUCTURALLY: one record per port, by
construction, with no secondary constraint to remember.

WHAT THE INVERSION GIVES AWAY, MEASURED RATHER THAN ASSUMED
===========================================================
``name`` was the SQLite PRIMARY KEY, so one agent could not hold two ports.
That constraint was doing real work and it was not free: the loser of a
same-agent race got ``no free a2a port in range [...]`` rather than a port —
the v0.21.18 symptom. Keying on ``port`` removes it and nothing replaces it,
so IN PRINCIPLE two concurrent auto-claims for ONE agent could each win a
DIFFERENT port and both return happily. That would be worse than the old
error, because it raises nothing: ``get_port`` would answer with whichever
record it met first while the other port stayed claimed forever.

IT DID NOT REPRODUCE. Measured 2026-08-28 through the public surface against a
real PostgreSQL 18 — 15 runs at 2, 4, 8, 16 and 32 threads, all released
together on a barrier — and every run ended with EXACTLY ONE claim and zero
errors. The store serialises ``Store.__init__`` for one schema behind its own
advisory lock (scitex-dev 0.56.6), so the racers queue at construction and
every thread after the first finds the claim in ``claim_port``'s idempotent
fast path.

NO SETTLEMENT LOGIC IS SHIPPED FOR IT, deliberately. A fix for a defect that
cannot be demonstrated arrives with a test that cannot fail, and a green test
proving nothing is worse than no test. This paragraph exists so a future
reader who DOES see one agent holding two ports knows the question was asked,
what was measured, and where to look: the fast path in ``claim_port`` and the
store's schema lock are what hold it today, and neither is a guarantee.

THE PRICE IS STATED RATHER THAN HIDDEN: "which port does this agent hold?"
inverts from a keyed lookup into a scan. The store exposes
``get``/``put``/``rows``, not SQL, so :func:`live_claims` reads the whole
ledger and the caller filters in Python. The ledger is bounded by the
configured range — 1,000 records at the default ``(19000, 19999)``, and in
practice the fleet's largest host holds tens — so this is comfortable, but it
is O(n) per call and a range widened to six figures would want an indexed
query instead. Recorded so a future reader finds a decision, not a surprise.

A RELEASED PORT IS A TOMBSTONE, AND THAT IS THE TRAP
====================================================
``Store.hide`` is the only removal this store offers; there is no delete. So
``release_port`` HIDES the claim, and a hidden record still OCCUPIES the store
identity. The two doors then disagree, which
``tests/.../test_port_allocator_pin_reclaim.py`` measured before this
migration was written:

  * ``get(key)`` answers ``None``            -> the record reads as ABSENT
  * ``put(key, NEW_RECORD)`` raises          -> the identity is TAKEN

Handled naively that is fleet-down, not merely a wrong answer. An operator who
writes ``spec.a2a.port: 19100`` has stated a contract, and every ordinary
restart runs ``agent_stop`` (``release_port``) then ``agent_start``
(``claim_port(explicit=19100, explicit_is_pin=True)``). If the second claim
consults a hidden-INCLUSIVE holder scan and rejects the tombstone with a guard
shaped "holder is not None and not hidden", the operator is told the port is
``already claimed by 'alpha'`` — BY ALPHA ITSELF — and a pinned agent never
comes back.

:func:`try_claim` therefore uses the three-valued ``Store.is_hidden``
distinction the store provides for exactly this: a tombstone is UNHIDDEN and
overwritten, because that is what releasing a port and claiming it again
means. ``state_db_grants.grant_send`` does the same thing for the same reason.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from scitex_dev.store import Store

#: The logical store name. Kept as the old table name so the ledger stays
#: greppable across the migration and in operator muscle memory.
#: ``scitex_dev.store`` renders it as four physical tables (``<name>_rows``,
#: ``_oplog``, ``_identity``, ``_cursor``).
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
    "port_store_target",
    "try_claim",
]


def _schema() -> Any:
    """The claim-ledger schema.

    Built lazily so importing this module does not import scitex-dev; the
    SQLite version was equally lazy about ``state_db``, for the same reason
    (import cost off the hot path).

    ``port`` is the sole IDENTITY and IMMUTABLE, which the store enforces on
    identities: changing one does not update the record, it names a different
    record. That is exactly right here — a claim on a different port IS a
    different claim.

    ``name`` and ``claimed_at`` are LAST_WRITER_WINS rather than IMMUTABLE. A
    port is a REUSABLE resource: the whole point of ``release_port`` is that
    the next claimant writes its own name over the record the previous holder
    left. IMMUTABLE is right for an append-only fact (a delivered verdict, a
    granted permission) and wrong for a lease.

    ``claimed_at`` is epoch REAL, not the ISO text the SQLite column held.
    Every migrated timestamp column across ``_state`` is REAL, and the only
    consumer is ``sac ports --json``, which passes the value straight through.
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
            # see the module docstring on why the access pattern inverts.
            "name": lease(FieldKind.TEXT, indexed=True),
            "claimed_at": lease(FieldKind.REAL),
        },
    )


def port_store_target() -> Any:
    """Resolve WHERE the claims live. Pure — does not connect."""
    from scitex_dev.store import host_store

    return host_store(pkg="scitex_agent_container", name=STORE_NAME)


def open_port_store() -> "Store":
    """Open the claim ledger. RAISES if PostgreSQL is unreachable.

    The caller owns closing it. Every public function opens and closes one per
    call, mirroring the old ``with open_db(...)`` shape — this runs on the
    launch and stop paths and on ``sac agents list``, never in a request loop.

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


def init_port_schema() -> str:
    """Create the claim tables if missing. Idempotent.

    Returns the resolved store LOCATOR as a string — the PostgreSQL equivalent
    of the ``Path`` the SQLite schema helper worked against, and useful in
    exactly the same way: it names WHERE the claims actually went, so an
    operator can check rather than assume.
    """
    store = open_port_store()
    try:
        return str(port_store_target().locator)
    finally:
        store.close()


def claim_values(port: int, agent_name: str, claimed_at: float) -> dict[str, Any]:
    """The full record a claim writes. One place, so the shape cannot drift."""
    return {"port": int(port), "name": agent_name, "claimed_at": claimed_at}


def live_claims(store: "Store") -> dict[int, str]:
    """``{port: holder}`` for every LIVE claim, in ONE read.

    ``rows()`` excludes hidden records by default, which IS the
    released-claim filter — spelled out because that exclusion is
    load-bearing here rather than incidental.
    """
    return {int(row.values["port"]): str(row.values["name"]) for row in store.rows()}


def holder_of(store: "Store", port: int) -> str | None:
    """Which agent LIVES on ``port``, or ``None`` when it is free.

    A hidden (released) claim reads as free, which is what "released" means.
    """
    row = store.get({"port": int(port)})
    return None if row is None else str(row.values["name"])


def try_claim(store: "Store", *, port: int, agent_name: str, now: float) -> bool:
    """Attempt an exclusive claim on ``port``. ``True`` iff we won it.

    Losing is a NORMAL outcome here, not an error — exactly as
    ``ON CONFLICT DO NOTHING`` treated it — so a lost race is reported as
    ``False`` and the caller moves to the next candidate.

    ATOMIC CLAIM-OR-LOSE, PRESERVED. The SQLite version's hard-won idiom is
    ``INSERT ... ON CONFLICT DO NOTHING`` followed by a read-back: ONE
    statement decides the race. Its predecessor — ``SELECT`` for a clash then
    a bare ``INSERT`` — was a TOCTOU, and WHICH error a caller got was decided
    by thread timing, which is why the failure moved between releases and read
    as a flake. ``put(..., expected_revision=NEW_RECORD)`` is the same
    contract for a port nobody has ever claimed: the store's guard states it
    directly ("the record must NOT exist" / "if a create was intended, the id
    is taken"), so either our record lands or it does not, with no window.

    THE ORDER OF THE TWO READS IS LOAD-BEARING for the TOMBSTONE path. The
    revision is read FIRST, before the row we decide on. That token is the
    compare-and-swap, and capturing it before any observation we act on is
    what makes every interleaving safe:

      * a competitor that takes the port BEFORE our ``revision`` call — our
        ``get`` sees a live record and we return ``False``;
      * a competitor that takes it BETWEEN the two reads — same, ``get`` sees
        it live;
      * a competitor that takes it AFTER our ``get`` — they moved the record,
        so our revision token is stale and ``unhide`` refuses.

    Read the row first and that last case inverts: our stale-but-newer token
    would match, our write would land on top of theirs, and BOTH callers would
    be told they hold the port. That is precisely the outcome the SQLite
    version's single-statement insert existed to prevent.

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
        return True

    if current.hidden is not True:
        return False

    try:
        store.unhide(key, expected_revision=revision, actor=ACTOR)
    except (RecordNotFoundError, RevisionMismatchError):
        return False
    store.put(claim_values(port, agent_name, now), expected_revision=ANY_REVISION)
    return True
