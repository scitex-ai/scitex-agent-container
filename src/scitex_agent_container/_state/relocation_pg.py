"""Relocation state — residency, leases and the attempt journal, on PostgreSQL.

Step 4 of the operator's sqlite→PostgreSQL migration (approved 2026-08-24).
Adopts :mod:`scitex_dev.store` the way :mod:`.state_db_incarnations` and
:mod:`.port_allocator_pg` do, so the fleet keeps one storage primitive and
one failure mode: the store resolves ``SCITEX_STORE_DSN`` or the per-host
PostgreSQL and has NO SQLite fallback, so an unreachable database raises
naming the DSN instead of silently writing somewhere nobody reads.

``db_path`` IS GONE from every function. It named a SQLite file; there is no
file.

THREE STORES, BECAUSE THERE WERE THREE TABLES
=============================================
They are kept separate rather than merged behind a discriminator: they have
different identities, different lifetimes, and the journal is an audit trail
whose retention rules must not be entangled with a lease that is replaced on
every claim.

WHAT ``rowid`` WAS DOING, AND WHAT REPLACES IT
==============================================
The SQLite residency table had no primary key. It leaned on ``rowid`` twice,
and both uses need an answer here because PostgreSQL has no equivalent
(``ctid`` is not stable):

1. ``UPDATE agent_residency SET to_ts=? WHERE rowid=?`` — addressing ONE stay
   to close it. Under the store, records are addressed by IDENTITY, so the
   identity has to be the thing that made that row unique: an agent's stay on
   a host beginning at a given instant. Hence ``(agent, host, from_ts)``.
   Closing a stay becomes a put on that identity, which is what the update
   always meant.

2. ``ORDER BY from_ts ASC, rowid ASC`` — a deterministic TIE-BREAK when two
   stays share a ``from_ts``. Insertion order is gone, so the tie is broken on
   ``hlc`` instead, the store's hybrid-logical stamp. That is a strict
   improvement rather than a substitute: ``rowid`` was monotonic only within
   one host's file, while ``hlc`` is documented as a TOTAL order across
   replicas ((wall_us, logical, node), never equal between two nodes), so the
   history reads identically on every host instead of only on the one that
   wrote it.

``_migrate`` IS DELETED, AND ITS DATA IS NOT
============================================
The SQLite module carried schema evolution — ``PRAGMA table_info`` to detect
an old shape by a MISSING COLUMN, then ``ALTER TABLE ADD COLUMN`` and
``ALTER TABLE RENAME TO``. None of that survives: the store owns its own DDL
from a declared schema, so there is nothing to evolve in place.

But the v1 journal table it created — ``relocation_journal_v1_one_row_per_agent``
— holds the ONLY record of relocations run under the old one-row-per-agent
key, and the module that renamed it was explicit that it is "RENAMED, never
dropped ... nothing is deleted — least of all an audit trail". Deleting the
migration code without carrying those rows would destroy exactly what that
comment protects. They move in the one-time data migration that accompanies
this module, NOT here: a runtime path that quietly imported legacy rows would
re-import them on every start.
"""

from __future__ import annotations

import json
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from scitex_dev.store import Store

#: One store per former table. Names carry the old table names so the state
#: stays greppable across the migration.
RESIDENCY_STORE = "agent_residency"
LEASE_STORE = "relocation_leases"
JOURNAL_STORE = "relocation_journal"

#: The pre-attempt journal table. Named here so the data migration and any
#: future archaeology agree on the string; nothing in this module reads it.
JOURNAL_V1_TABLE = "relocation_journal_v1_one_row_per_agent"

_ACTOR = "scitex-agent-container"

__all__ = [
    "JOURNAL_V1_TABLE",
    "JOURNAL_STORE",
    "LEASE_STORE",
    "RESIDENCY_STORE",
    "current_residency",
    "init_relocation_schema",
    "load_journal",
    "load_journal_attempts",
    "load_lease",
    "read_residency_history",
    "record_residency",
    "save_journal",
    "save_lease",
]


def _policies() -> tuple[Any, Any]:
    """``(ident, fact)`` policy builders. Lazy: importing scitex-dev is not free."""
    from scitex_dev.store import FieldPolicy, FieldRole, MergeRule

    def ident(kind: Any) -> Any:
        return FieldPolicy(
            kind=kind,
            role=FieldRole.IDENTITY,
            required=True,
            merge=MergeRule.IMMUTABLE,
            indexed=False,
        )

    def fact(kind: Any, *, required: bool = False, indexed: bool = False) -> Any:
        return FieldPolicy(
            kind=kind,
            role=FieldRole.DATA,
            required=required,
            merge=MergeRule.LAST_WRITER_WINS,
            indexed=indexed,
        )

    return ident, fact


def _residency_schema() -> Any:
    """A stay: one agent, on one host, starting at one instant.

    ``from_ts`` is part of the IDENTITY and therefore IMMUTABLE — correct, and
    load-bearing: re-recording the same stay must address the same record, and
    a stay that began at a different moment IS a different stay.
    """
    from scitex_dev.store import FieldKind, Schema

    ident, fact = _policies()
    return Schema(
        name=RESIDENCY_STORE,
        fields={
            "agent": ident(FieldKind.TEXT),
            "host": ident(FieldKind.TEXT),
            "from_ts": ident(FieldKind.REAL),
            # to_ts stays NULLABLE: an OPEN stay is the whole point, and a
            # sentinel would have to be excluded from every query by hand.
            "to_ts": fact(FieldKind.REAL),
            "seeded": fact(FieldKind.INTEGER, required=True),
            "note": fact(FieldKind.TEXT),
        },
    )


def _lease_schema() -> Any:
    """The single current lease per agent — replaced, never appended.

    ``agent`` alone is the identity, preserving the SQLite PRIMARY KEY: there
    is exactly one answer to "who holds it", and a history of holders would
    invite reading the wrong one.
    """
    from scitex_dev.store import FieldKind, Schema

    ident, fact = _policies()
    return Schema(
        name=LEASE_STORE,
        fields={
            "agent": ident(FieldKind.TEXT),
            "holder": fact(FieldKind.TEXT, required=True),
            "token": fact(FieldKind.TEXT, required=True),
            "fence": fact(FieldKind.INTEGER, required=True),
            "expires_at": fact(FieldKind.REAL, required=True),
            "updated_at": fact(FieldKind.REAL, required=True),
        },
    )


def _journal_schema() -> Any:
    """One row per ATTEMPT — ``(agent, attempt)``, the old PRIMARY KEY."""
    from scitex_dev.store import FieldKind, Schema

    ident, fact = _policies()
    return Schema(
        name=JOURNAL_STORE,
        fields={
            "agent": ident(FieldKind.TEXT),
            "attempt": ident(FieldKind.INTEGER),
            "from_host": fact(FieldKind.TEXT, required=True),
            "to_host": fact(FieldKind.TEXT, required=True),
            "phase": fact(FieldKind.TEXT, required=True),
            # Steps are stored WHOLE as JSON, as they were: they are only ever
            # read back as a unit, and a partially-written journal would be
            # worse than none.
            "steps": fact(FieldKind.TEXT, required=True),
            "started_at": fact(FieldKind.REAL, required=True, indexed=True),
            "updated_at": fact(FieldKind.REAL, required=True),
        },
    )


def _open(name: str, schema: Any) -> "Store":
    """Open one relocation store. RAISES if PostgreSQL is unreachable."""
    import socket

    from scitex_dev.store import Store, WriterPolicy, host_store

    return Store(
        host_store(pkg="scitex_agent_container", name=name),
        schema,
        node=socket.gethostname(),
        writer_policy=WriterPolicy.SINGLE_WRITER,
        actor=_ACTOR,
    )


def _residency_store() -> "Store":
    return _open(RESIDENCY_STORE, _residency_schema())


def _lease_store() -> "Store":
    return _open(LEASE_STORE, _lease_schema())


def _journal_store() -> "Store":
    return _open(JOURNAL_STORE, _journal_schema())


def init_relocation_schema() -> str:
    """Create all three stores if missing. Idempotent. Returns the locator.

    NO LONGER CALLS ``state_db.init_schema``. The SQLite version did, and
    returned its ``Path`` — a dependency that made relocation state
    inseparable from the rest of ``state.db``. Cutting it is part of the
    point: these three stores stand on their own now.
    """
    from scitex_dev.store import host_store

    for name, schema in (
        (RESIDENCY_STORE, _residency_schema()),
        (LEASE_STORE, _lease_schema()),
        (JOURNAL_STORE, _journal_schema()),
    ):
        _open(name, schema).close()
    return str(host_store(pkg="scitex_agent_container", name=RESIDENCY_STORE).locator)


# --------------------------------------------------------------------------
# residency
# --------------------------------------------------------------------------


def _stays(store: "Store", agent: str) -> list[Any]:
    """This agent's stays, oldest first, ties broken deterministically.

    ``from_ts`` then ``hlc``. The SQLite version broke the tie on ``rowid``
    (insertion order within one file); ``hlc`` is a documented TOTAL order
    across replicas, so two hosts reading the same history now agree.
    """
    rows = [r for r in store.rows() if r.values.get("agent") == agent]
    return sorted(rows, key=lambda r: (float(r.values["from_ts"]), r.hlc))


def record_residency(
    *,
    agent: str,
    host: str,
    now: float | None = None,
    seeded_from_spec: bool = False,
    note: str = "",
) -> bool:
    """Open a stay for ``agent`` on ``host``, closing whatever was open.

    Returns ``True`` when a new stay was opened and ``False`` when the agent
    was already recorded on that host — a successful no-op, not a failure.

    ``seeded_from_spec`` travels into the row so a value that came from a
    legacy spec field is not later mistaken for something measured.
    Provenance dropped at the moment of writing cannot be recovered by
    reading.

    THE CLOSE AND THE OPEN ARE ONE TRANSACTION. In SQLite they shared an
    ``open_db`` block; here they share ``Store.batch()``, whose contract is
    that on any exception "the transaction is rolled back, so a failed batch
    applies NOTHING". Without it a crash between the two writes would leave
    the agent with NO open stay — the one state this table must never be in,
    since ``current_residency`` would then answer ``None`` for a running
    agent.
    """
    if not agent or not agent.strip():
        raise ValueError("record_residency needs the agent name")
    if not host or not host.strip():
        raise ValueError(
            "record_residency needs the host — an empty destination would open a "
            "residency that answers no question"
        )
    agent, host = agent.strip(), host.strip()
    ts = float(now) if now is not None else time.time()

    from scitex_dev.store import ANY_REVISION

    store = _residency_store()
    try:
        open_stays = [r for r in _stays(store, agent) if r.values.get("to_ts") is None]
        current = open_stays[-1] if open_stays else None
        if current is not None and (current.values.get("host") or "") == host:
            return False

        with store.batch():
            if current is not None:
                closed = dict(current.values)
                closed["to_ts"] = ts
                store.put(closed, expected_revision=ANY_REVISION)
            store.put(
                {
                    "agent": agent,
                    "host": host,
                    "from_ts": ts,
                    "to_ts": None,
                    "seeded": 1 if seeded_from_spec else 0,
                    "note": note or None,
                },
                expected_revision=ANY_REVISION,
            )
        return True
    finally:
        store.close()


def read_residency_history(agent: str):
    """The agent's stays, oldest first, as ``.._lifecycle._residency.Residency``.

    Returns ``()`` for an agent this store has never heard of — genuinely
    "the db knows nothing", which is what lets a legacy spec ``host:`` seed it
    once, and is deliberately distinct from a recorded stay that has since
    closed.
    """
    from .._lifecycle._residency import Residency

    store = _residency_store()
    try:
        rows = _stays(store, agent)
    finally:
        store.close()
    return tuple(
        Residency(
            host=r.values["host"],
            from_ts=float(r.values["from_ts"]),
            to_ts=None if r.values.get("to_ts") is None else float(r.values["to_ts"]),
        )
        for r in rows
    )


def current_residency(agent: str) -> str | None:
    """The host of the open stay, or ``None``. ``None`` is not a hostname."""
    from .._lifecycle._residency import current_host

    return current_host(read_residency_history(agent))


# --------------------------------------------------------------------------
# lease
# --------------------------------------------------------------------------


def save_lease(lease) -> None:
    """Persist the single current lease for an agent, replacing any earlier one.

    One record per agent by identity, so a second holder cannot land alongside
    the first. The fence is what actually fences — an old holder that comes
    back reads this record, sees a fence above its own, and knows it is out —
    so it is REPLACED rather than appended.
    """
    from scitex_dev.store import ANY_REVISION

    store = _lease_store()
    try:
        store.put(
            {
                "agent": lease.agent,
                "holder": lease.holder,
                "token": lease.token,
                "fence": int(lease.fence),
                "expires_at": float(lease.expires_at),
                "updated_at": time.time(),
            },
            expected_revision=ANY_REVISION,
        )
    finally:
        store.close()


def load_lease(agent: str):
    """The stored lease for ``agent``, or ``None`` if nobody has ever held it.

    A record carrying an EMPTY token returns ``None`` — not a lease. Every
    verb except ``claim`` requires the caller to PRESENT the token, and
    ``Lease`` refuses an empty one because an empty token would satisfy every
    token check. A holder that cannot present a token cannot prove it holds
    anything, and treating it as held would leave the agent permanently
    unrelocatable behind a credential nobody has.
    """
    from .._lifecycle._relocate_lease import Lease

    store = _lease_store()
    try:
        row = store.get({"agent": agent})
    finally:
        store.close()
    if row is None or not (row.values.get("token") or "").strip():
        return None
    return Lease(
        agent=agent,
        holder=row.values["holder"],
        token=row.values["token"],
        fence=int(row.values["fence"]),
        expires_at=float(row.values["expires_at"]),
    )


# --------------------------------------------------------------------------
# journal
# --------------------------------------------------------------------------


def _attempts(store: "Store", agent: str) -> list[Any]:
    """This agent's journal records, oldest attempt first."""
    rows = [r for r in store.rows() if r.values.get("agent") == agent]
    return sorted(rows, key=lambda r: int(r.values["attempt"]))


def save_journal(relocation) -> int:
    """Write this ATTEMPT's phase and steps. Returns the attempt number written.

    ONE RECORD PER ATTEMPT, not per agent. Which attempt this is comes from
    the relocation's own opening moment (``started_at``): a record RESUMED
    from the store carries the timestamp its first run stamped, so it updates
    the attempt it already owns; a freshly begun one carries a new timestamp
    and opens the next attempt. The caller passes nothing and therefore cannot
    get it wrong, and a retry after an abort no longer erases the attempt
    whose failure prompted it.

    The MAX(attempt)+1 allocation is carried over as-is, INCLUDING its
    read-then-write shape. That is a race in principle — two savers for one
    agent would compute the same next attempt — and it is not one in practice
    for the reason the SQLite version relied on: two relocations of one agent
    cannot be in flight at once, because the resume path loads the latest
    attempt and refuses a different destination. Noting it rather than
    silently inheriting it: if that higher-level guarantee is ever relaxed,
    this is where it bites.
    """
    from scitex_dev.store import ANY_REVISION

    steps = json.dumps(
        [{"phase": s.phase, "at": s.at, "detail": s.detail} for s in relocation.steps]
    )
    started_at = float(relocation.started_at)

    store = _journal_store()
    try:
        existing = _attempts(store, relocation.agent)
        same = [
            r for r in existing if float(r.values["started_at"]) == started_at
        ]
        if same:
            attempt = int(same[0].values["attempt"])
        else:
            attempt = 1 + max(
                (int(r.values["attempt"]) for r in existing), default=0
            )
        store.put(
            {
                "agent": relocation.agent,
                "attempt": attempt,
                "from_host": relocation.from_host,
                "to_host": relocation.to_host,
                "phase": relocation.phase,
                "steps": steps,
                "started_at": started_at,
                "updated_at": time.time(),
            },
            expected_revision=ANY_REVISION,
        )
        return attempt
    finally:
        store.close()


def _relocation_from_values(agent: str, values) -> Any:
    """Rebuild one ``Relocation``, or ``None`` when the record will not parse."""
    from .._lifecycle._relocate_phases import Relocation, Step

    try:
        raw = json.loads(values["steps"])
        steps = tuple(
            Step(phase=s["phase"], at=float(s["at"]), detail=s.get("detail", ""))
            for s in raw
        )
        return Relocation(
            agent=agent,
            from_host=values["from_host"],
            to_host=values["to_host"],
            phase=values["phase"],
            steps=steps,
        )
    except Exception:  # stx-allow: fallback (reason: an unparseable journal record must not make an agent unrelocatable nor hide the other attempts; the record is kept, not deleted, and the caller opens a fresh relocation)
        return None


def load_journal_attempts(agent: str):
    """Every recorded attempt for ``agent``, OLDEST FIRST, as ``(attempt, Relocation)``.

    The audit read. An attempt whose stored JSON will not parse is SKIPPED
    rather than raising — one corrupt record must not hide the rest of the
    history, and the record itself is still there for whoever wants to look.
    """
    store = _journal_store()
    try:
        rows = _attempts(store, agent)
    finally:
        store.close()
    out = []
    for row in rows:
        relocation = _relocation_from_values(agent, row.values)
        if relocation is not None:
            out.append((int(row.values["attempt"]), relocation))
    return tuple(out)


def load_journal(agent: str):
    """The LATEST attempt's relocation for ``agent``, or ``None``.

    The resume read, and the reason it is the latest rather than the only one:
    a re-run continues the attempt that stopped, and the earlier attempts are
    history — present, readable through :func:`load_journal_attempts`, and
    never resumed by accident.

    A record whose JSON will not parse returns ``None`` rather than raising:
    the caller's next move is to open a fresh relocation, and a corrupt
    journal must not make the agent unrelocatable. Nothing is deleted.
    """
    store = _journal_store()
    try:
        rows = _attempts(store, agent)
    finally:
        store.close()
    if not rows:
        return None
    return _relocation_from_values(agent, rows[-1].values)
