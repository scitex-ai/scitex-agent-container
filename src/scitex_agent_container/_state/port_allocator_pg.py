"""``a2a_ports`` — the A2A port claim ledger, on PostgreSQL only.

Step 4 of the operator's sqlite→PostgreSQL migration (approved 2026-08-24).
The move is by ADOPTING :mod:`scitex_dev.store`, the fleet's own store
primitive, exactly as :mod:`.state_db_incarnations` did — not by sac growing
a private psycopg layer. That primitive implements the operator's 2026-08-19
rule ("fail fast, fail loud, no fallbacks"): it resolves ``SCITEX_STORE_DSN``
or the per-host PostgreSQL and has deliberately NO SQLite fallback, so a host
whose database is unreachable raises rather than quietly allocating ports
into a file nobody reads.

``db_path`` IS GONE from every function. It named a SQLite file; there is no
file. Callers that threaded it through simply stop.

THE IDENTITY IS THE PORT, AND THAT IS THE WHOLE DESIGN
======================================================
The obvious port of this module keys the store on ``name``, because
:func:`get_port` and :func:`release_port` both look up by agent. That would
be wrong in a way tests would not catch.

The invariant this module exists to hold is ``UNIQUE(port)`` — a DIFFERENT
column from the lookup key. Keyed on ``name``, two agents claiming port 8080
are two DIFFERENT records, and the store would accept both: mutual exclusion
on the port silently gone, while every unit test still passes. That is the
exact collision this module's SQLite version was rewritten to stop, and the
comments there record what it cost — the v0.21.19 release died on
``sqlite3.IntegrityError: UNIQUE constraint failed: a2a_ports.port``,
reproduced deterministically at 16 threads as 6 raw driver escapes.

So ``port`` is the sole IDENTITY field. The store's own identity uniqueness
CARRIES the invariant structurally: one row per port, by construction, on
every backend, without a secondary constraint to remember. ``name`` becomes
data ABOUT the claim.

The price is honest and worth stating: "which port does this agent hold?"
becomes a scan over claims rather than a keyed lookup. ``name`` is indexed
to keep that cheap, and the ledger is bounded by the port range — tens of
rows, not millions.

ATOMIC CLAIM-OR-LOSE, PRESERVED EXACTLY
=======================================
The SQLite version's hard-won idiom is ``INSERT ... ON CONFLICT DO NOTHING``
followed by a read-back: one statement decides the race, and the read-back
says who won. Its predecessor — ``SELECT`` for a clash, then a bare
``INSERT`` — was a TOCTOU, and WHICH error a caller got was decided by thread
timing, which is why the failure moved between releases and read as a flake.

``put(..., expected_revision=NEW_RECORD)`` is the same contract. The store's
guard states it directly: NEW_RECORD means "the record must NOT exist", and a
lost race raises :class:`RevisionMismatchError` ("the id is taken"). Either
our row lands or it does not; there is no window between the check and the
write. The read-back after a loss is what tells us whose it is.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # pragma: no cover - typing only
    from scitex_dev.store import Store

#: The store name. Kept as the old table name so the ledger stays greppable
#: across the migration and in operator muscle memory.
STORE_NAME = "a2a_ports"

#: Recorded on every write as the acting component.
_ACTOR = "scitex-agent-container"

__all__ = [
    "STORE_NAME",
    "claim_port",
    "get_port",
    "init_port_schema",
    "list_claims",
    "open_port_store",
    "port_store_target",
    "release_port",
]


def _schema() -> Any:
    """The claim-ledger schema.

    Built lazily so importing this module does not import scitex-dev — the
    SQLite version was equally lazy about ``state_db``, for the same reason.

    ``port`` is the sole IDENTITY and is IMMUTABLE, which the store enforces
    on identities: changing one does not update the record, it names a
    different record. That is precisely right here — a claim on a different
    port IS a different claim.

    ``name`` is indexed because every lookup in this module goes through it
    (see the module docstring on why the access pattern inverts).
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

    def fact(kind: Any, *, required: bool = False, indexed: bool = False) -> Any:
        return FieldPolicy(
            kind=kind,
            role=FieldRole.DATA,
            required=required,
            merge=MergeRule.LAST_WRITER_WINS,
            indexed=indexed,
        )

    return Schema(
        name=STORE_NAME,
        fields={
            "port": ident(FieldKind.INTEGER),
            "name": fact(FieldKind.TEXT, required=True, indexed=True),
            "claimed_at": fact(FieldKind.TEXT, required=True),
        },
    )


def port_store_target() -> Any:
    """Resolve WHERE the claims live. Pure — does not connect."""
    from scitex_dev.store import host_store

    return host_store(pkg="scitex_agent_container", name=STORE_NAME)


def open_port_store() -> "Store":
    """Open the claim store. RAISES if PostgreSQL is unreachable.

    The caller owns closing it. Every public function opens and closes one
    per call, mirroring the old ``with open_db(...)`` shape: claims happen on
    launch and release paths, never on a request path.
    """
    import socket

    from scitex_dev.store import Store, WriterPolicy

    return Store(
        port_store_target(),
        _schema(),
        node=socket.gethostname(),
        writer_policy=WriterPolicy.SINGLE_WRITER,
        actor=_ACTOR,
    )


def init_port_schema() -> str:
    """Create the claim tables if missing. Idempotent.

    Returns the resolved store LOCATOR — the PostgreSQL equivalent of the
    ``Path`` the SQLite version returned, and useful the same way: it names
    WHERE the state actually went.
    """
    store = open_port_store()
    try:
        return str(port_store_target().locator)
    finally:
        store.close()


def _claims(store: "Store") -> list[Any]:
    """Every visible claim. One place, so the hidden-row view is uniform."""
    return store.rows()


def get_port(agent_name: str) -> int | None:
    """The port ``agent_name`` currently holds, or ``None``.

    A scan by design — see the module docstring: the identity is the port,
    so the agent is data. The ledger is bounded by the configured range.
    """
    store = open_port_store()
    try:
        for row in _claims(store):
            if row.values.get("name") == agent_name:
                return int(row.values["port"])
        return None
    finally:
        store.close()


def _try_claim(store: "Store", *, port: int, agent_name: str, now: str) -> bool:
    """Attempt an atomic create-only claim on ``port``. True iff we won it.

    ``NEW_RECORD`` is the store's "the record must NOT exist" assertion, so
    this cannot interleave the way a SELECT-then-INSERT could. A lost race
    raises ``RevisionMismatchError`` and is reported as False — losing is a
    normal outcome here, not an error, exactly as ``ON CONFLICT DO NOTHING``
    treated it.

    A RELEASED PORT IS HIDDEN, NOT ABSENT, and that distinction is the bug
    this branch exists to fix. ``release_port`` hides the row; the record
    still EXISTS for the revision check, so a later ``NEW_RECORD`` claim on
    a genuinely free port failed with "already claimed by another agent".
    ``is_hidden`` is three-valued precisely so a caller can tell "released"
    from "held" — a hidden claim is unhidden and overwritten, which is what
    releasing then re-claiming a port means.
    """
    from scitex_dev.store import ANY_REVISION, NEW_RECORD, RevisionMismatchError

    hidden = store.is_hidden({"port": port})
    if hidden is True:
        store.unhide({"port": port}, expected_revision=ANY_REVISION)
        store.put(
            {"port": port, "name": agent_name, "claimed_at": now},
            expected_revision=ANY_REVISION,
        )
        return True

    try:
        store.put(
            {"port": port, "name": agent_name, "claimed_at": now},
            expected_revision=NEW_RECORD,
        )
    except RevisionMismatchError:
        return False
    return True


def _holder_of(store: "Store", port: int) -> str | None:
    """Which agent holds ``port``, or ``None`` if it is free."""
    row = store.get({"port": port})
    return None if row is None else str(row.values["name"])


def claim_port(
    agent_name: str,
    *,
    range_: tuple[int, int] | None = None,
    explicit: int | None = None,
    explicit_is_pin: bool = True,
) -> int:
    """Atomically claim a free port for ``agent_name``.

    Behaviour is preserved from the SQLite version verbatim, including the
    distinction that a routine restart once died on:

    * ``explicit_is_pin=True`` — an OPERATOR PIN from ``spec.a2a.port``. A
      foreign holder is a real misconfiguration, so raise and make it
      visible; handing back a different port would break the contract the
      pin exists to state.
    * ``explicit_is_pin=False`` — a port WE auto-allocated earlier and are
      merely RE-claiming across a restart. That is a preference, not a pin:
      if it was taken while we were down, a NEW free port is the correct
      answer and failing the launch is not, so fall through to the scan.

    Idempotent: a second call for the same agent returns the existing port
    without mutating state.

    Raises:
        RuntimeError: when no free port remains in ``range_``, or when an
            operator-PINNED ``explicit`` port is held by another agent.
    """
    from .port_allocator import _now_iso, _resolve_range

    lo, hi = _resolve_range(range_)
    now = _now_iso()

    store = open_port_store()
    try:
        # Idempotent fast path: same agent -> return the existing claim.
        for row in _claims(store):
            if row.values.get("name") != agent_name:
                continue
            existing = int(row.values["port"])
            if explicit is None or explicit == existing:
                return existing
            # The operator changed the pin between starts. Release the old
            # claim so the new one can be attempted below.
            store.hide({"port": existing}, expected_revision=_ANY())
            break

        if explicit is not None:
            if _try_claim(store, port=explicit, agent_name=agent_name, now=now):
                return int(explicit)

            holder = _holder_of(store, explicit)
            if holder == agent_name:
                # We raced OURSELVES (two starts of one agent). Honour the
                # documented idempotency rather than failing a legitimate
                # re-entry.
                return int(explicit)

            if explicit_is_pin:
                owner = holder if holder is not None else "another agent"
                raise RuntimeError(
                    f"a2a port {explicit} already claimed by "
                    f"{owner!r}; cannot pin for {agent_name!r}"
                )
            # Not a pin — fall through to the auto scan.

        for candidate in range(lo, hi + 1):
            if _try_claim(store, port=candidate, agent_name=agent_name, now=now):
                return candidate
        raise RuntimeError(
            f"no free a2a port in range [{lo}, {hi}] (all claimed); "
            "extend a2a.port_range in ~/.scitex/agent-container/config.yaml"
        )
    finally:
        store.close()


def _ANY() -> Any:
    """``ANY_REVISION``, imported lazily like every other store symbol here."""
    from scitex_dev.store import ANY_REVISION

    return ANY_REVISION


def release_port(agent_name: str) -> bool:
    """Drop the claim. Idempotent — True iff a claim was actually released."""
    store = open_port_store()
    try:
        for row in _claims(store):
            if row.values.get("name") == agent_name:
                store.hide({"port": int(row.values["port"])}, expected_revision=_ANY())
                return True
        return False
    finally:
        store.close()


def list_claims() -> list[dict]:
    """Every live claim, ascending by port — the shape the CLI renders."""
    store = open_port_store()
    try:
        claims = [
            {
                "name": row.values["name"],
                "port": int(row.values["port"]),
                "claimed_at": row.values["claimed_at"],
            }
            for row in _claims(store)
        ]
    finally:
        store.close()
    return sorted(claims, key=lambda c: c["port"])
