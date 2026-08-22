"""Inbound dispatch ledger — one row per INBOUND dispatch awaiting a
completion report (2026-06-18), on PostgreSQL only.

The receiver-side mirror of :mod:`dispatch_ledger` (the SENDER's outbound
record). It exists for the ``runtime: tui`` push-feedback loop: a TUI agent
has no in-process turn envelope, so the requester identity (``from_agent`` +
``dispatch_id``) of a bus-pushed wake cannot ride the tmux-injected text from
the host-side bridge through to the in-container ``Stop`` hook that reports
completion. This ledger is that bridge.

Lifecycle of one row:

  * ``pending``   — the bridge recorded a requester-bearing inbound wake
    (:func:`record_inbound`); the turn is queued/running in the TUI.
  * ``reporting`` — the Stop hook atomically CLAIMED the oldest pending row
    (:func:`claim_oldest_pending`) to push its completion.
  * ``reported`` / ``failed`` — terminal, set by :func:`mark_reported` after
    the completion push to the requester succeeds / fails loud.

FIFO by ``ts`` + sequential TUI turn processing keeps the dispatch↔turn
correlation correct: the Nth ``Stop`` claims the Nth recorded dispatch.

WHY THIS MODULE NO LONGER TOUCHES SQLite
========================================
The operator's 2026-08-19 order was to eradicate SQLite and move to
PostgreSQL: "fail fast, fail loud, no fallbacks". This is the fourth table to
move, after ``verdict_delivered``, ``incarnations`` and ``pending_prompts``,
and it moves the same way — by ADOPTING :mod:`scitex_dev.store` rather than
by sac growing a private psycopg layer. ``db_path`` is gone from every
function; it named a SQLite file and there is no file.

The old module's own rationale for the cross-process bridge — "the SAME
state.db is bound into the container so the host-side writer and the
in-container reader share it" — is what PostgreSQL now provides directly,
and better: the two processes no longer need a shared mount, only a reachable
endpoint.

THE AUTOINCREMENT ID WAS NEVER PUBLIC IN EFFECT
===============================================
``id INTEGER PRIMARY KEY AUTOINCREMENT`` looked like the hard part: it is
returned by ``record_inbound`` and taken by ``mark_reported``, so porting to a
store with no counter looked like it forced a surrogate — and surrogate ids do
not survive a store boundary, which this fleet has already paid for once.

Measured 2026-08-20 before assuming it:

    record_inbound's return value, consumed in PRODUCTION:  NONE
      runtimes/_tui_outbound.py returns it from a wrapper; no caller binds it.
    mark_reported's argument in PRODUCTION:  always claim-derived
      row_id = int(claimed["id"])  ->  mark_reported(row_id, ...)

So the id only ever ROUND-TRIPS claim -> settle inside one Stop-hook call. It
never crosses a process boundary, is never persisted elsewhere, and is never
compared. Its integer-ness was an artifact of how SQLite minted it, not a
property anything depended on. The natural key replaces it outright.

IDENTITY IS TOTAL, WHICH TOOK ONE DECISION
==========================================
``(agent, from_agent, dispatch_id, ts)``. ``dispatch_id`` is optional at the
call site, and store IDENTITY fields must be present, so a missing one is
stored as ``""`` — read it as "this wake carried no dispatch id", the same
thing the SQLite ``NULL`` meant.

That leaves one collision: two wakes for the same agent, from the same peer,
with no dispatch id, in the SAME float instant. ``time.time()`` is
microsecond-resolution and a TUI wake is a human-or-bus event, so it is not
credible — but "not credible" is exactly the assumption that bites, and the
SQLite counter made duplicates free. So the insert RETRIES with the timestamp
advanced by one microsecond instead of failing or overwriting. Deterministic,
preserves FIFO order, and needs no counter.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any, Optional

if TYPE_CHECKING:  # pragma: no cover - typing only
    from scitex_dev.store import Store

#: The logical store name. ``scitex_dev.store`` renders it as four physical
#: tables (``<name>_rows``, ``_oplog``, ``_identity``, ``_cursor``).
STORE_NAME = "inbound_dispatches"

#: Every write from this host is attributed to one node. The bridge that
#: records and the Stop hook that claims both run on this host, so
#: SINGLE_WRITER is honest rather than convenient.
_ACTOR = "scitex-agent-container"

#: Row lifecycle. ``record_inbound`` always writes ``pending``; the Stop hook
#: claims to ``reporting`` then settles to ``reported`` / ``failed``.
STATUS_PENDING = "pending"
STATUS_REPORTING = "reporting"
STATUS_REPORTED = "reported"
STATUS_FAILED = "failed"
VALID_STATUSES = (STATUS_PENDING, STATUS_REPORTING, STATUS_REPORTED, STATUS_FAILED)

#: The identity fields, in order. Exported because callers hand a claimed
#: row straight back to :func:`mark_reported` and this names what that needs.
IDENTITY_FIELDS = ("agent", "from_agent", "dispatch_id", "ts")

__all__ = [
    "IDENTITY_FIELDS",
    "STATUS_FAILED",
    "STATUS_PENDING",
    "STATUS_REPORTED",
    "STATUS_REPORTING",
    "STORE_NAME",
    "VALID_STATUSES",
    "claim_oldest_pending",
    "inbound_store_target",
    "init_inbound_schema",
    "list_inbound",
    "mark_reported",
    "open_inbound_store",
    "record_inbound",
]


def _schema() -> Any:
    """The inbound-dispatch schema.

    Built lazily so importing this module does not import scitex-dev; the old
    module was equally lazy about ``state_db``, for the same reason.

    ``status`` is LAST_WRITER_WINS because the whole point is that it moves:
    pending -> reporting -> reported/failed. ``reported_ts`` likewise, and it
    is not required — a row that has not settled has no settle time, and
    inventing one would make "never reported" indistinguishable from
    "reported at epoch".
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

    def data(kind: Any, *, required: bool) -> Any:
        return FieldPolicy(
            kind=kind,
            role=FieldRole.DATA,
            required=required,
            merge=MergeRule.LAST_WRITER_WINS,
            indexed=False,
        )

    return Schema(
        name=STORE_NAME,
        fields={
            "agent": ident(FieldKind.TEXT),
            "from_agent": ident(FieldKind.TEXT),
            "dispatch_id": ident(FieldKind.TEXT),
            "ts": ident(FieldKind.REAL),
            "status": data(FieldKind.TEXT, required=True),
            "reported_ts": data(FieldKind.REAL, required=False),
        },
    )


def inbound_store_target() -> Any:
    """Resolve WHERE the inbound ledger lives. Pure — does not connect."""
    from scitex_dev.store import host_store

    return host_store(pkg="scitex_agent_container", name=STORE_NAME)


def open_inbound_store() -> Store:
    """Open the ledger. RAISES if PostgreSQL is unreachable.

    The caller owns closing it. Every public function opens and closes one per
    call, mirroring the old ``with open_db(...)`` shape: this runs once per
    inbound wake and once per Stop hook, not on a request path.
    """
    import socket

    from scitex_dev.store import Store, WriterPolicy

    return Store(
        inbound_store_target(),
        _schema(),
        node=socket.gethostname(),
        writer_policy=WriterPolicy.SINGLE_WRITER,
        actor=_ACTOR,
    )


def init_inbound_schema() -> str:
    """Create the ledger tables if missing. Idempotent.

    Returns the resolved store LOCATOR as a string — the PostgreSQL
    equivalent of the path the SQLite version returned, and it names WHERE
    the state actually went so an operator can check rather than assume.
    """
    store = open_inbound_store()
    try:
        return str(inbound_store_target().locator)
    finally:
        store.close()


def _key(row_or_mapping: Any) -> dict[str, Any]:
    """The identity mapping for a row or a claimed dict.

    Accepts either shape so a caller can hand back exactly what
    :func:`claim_oldest_pending` gave them without unpacking it.
    """
    src = getattr(row_or_mapping, "values", None) or row_or_mapping
    if callable(src):  # a Mapping's .values method, not a Row's field dict
        src = row_or_mapping
    return {field: src[field] for field in IDENTITY_FIELDS}


def record_inbound(
    *,
    agent: str,
    from_agent: str,
    dispatch_id: Optional[str] = None,
    ts: Optional[float] = None,
) -> dict[str, Any]:
    """Insert one ``pending`` inbound-dispatch row; return its identity.

    Called by the bridge when an inbound wake carries a ``from_agent`` (the
    peer to report back to). A wake with no requester is NOT recorded by the
    caller — there is nobody to report to. ``ts`` is a real-time injection
    seam for tests.

    Returns the identity mapping rather than the old integer id. Nothing in
    production consumed that integer (see the module docstring), and the
    mapping is what :func:`mark_reported` now takes.

    THE MICROSECOND RETRY is the collision handling described in the module
    docstring: identical (agent, from_agent, dispatch_id, ts) means the SAME
    record to the store, so a genuine second wake in the same instant would
    otherwise silently overwrite the first. Advancing ``ts`` keeps both and
    keeps them ordered.
    """
    if not agent or not from_agent:
        raise ValueError("record_inbound requires non-empty agent + from_agent")

    from scitex_dev.store import NEW_RECORD, RevisionMismatchError

    row_ts = float(ts) if ts is not None else time.time()
    store = open_inbound_store()
    try:
        for _ in range(1000):
            key = {
                "agent": agent,
                "from_agent": from_agent,
                "dispatch_id": dispatch_id or "",
                "ts": row_ts,
            }
            try:
                store.put(
                    {**key, "status": STATUS_PENDING}, expected_revision=NEW_RECORD
                )
            except RevisionMismatchError:
                row_ts += 1e-6
                continue
            return key
        raise RuntimeError(
            "record_inbound: 1000 identical timestamps for the same "
            f"(agent={agent!r}, from_agent={from_agent!r}) — refusing to spin"
        )
    finally:
        store.close()


def claim_oldest_pending(*, agent: str) -> dict[str, Any] | None:
    """Atomically claim the OLDEST ``pending`` row for ``agent``.

    Flips exactly one row ``pending → reporting`` and returns it (identity
    fields plus ``status``), or ``None`` when the agent has no pending
    dispatch (the common no-op — most turns have no requester).

    THE ATOMIC CLAIM IS PRESERVED, and it was explicit in the SQLite version:
    "two concurrent Stop hooks (or a hook retry) can never push the same
    completion twice". SQLite got that from ``BEGIN IMMEDIATE``; the store has
    no transaction, so it comes from optimistic concurrency instead. The
    update carries ``expected_revision=<the row's seq>``, so a racing claimer
    that already flipped this row raises ``RevisionMismatchError`` — and we
    move to the NEXT pending row rather than failing, because a competitor
    taking one row is not a reason to report none.

    ``Row.seq`` IS the revision; there is no ``Row.revision``.
    """
    from scitex_dev.store import RevisionMismatchError

    store = open_inbound_store()
    try:
        pending = [
            row
            for row in store.rows()
            if row.values.get("agent") == agent
            and row.values.get("status") == STATUS_PENDING
        ]
        pending.sort(key=lambda r: float(r.values["ts"]))
        for row in pending:
            key = _key(row.values)
            try:
                store.put({**key, "status": STATUS_REPORTING}, expected_revision=row.seq)
            except RevisionMismatchError:
                continue
            claimed = dict(row.values)
            claimed["status"] = STATUS_REPORTING
            return claimed
        return None
    finally:
        store.close()


def mark_reported(
    handle: Any,
    *,
    status: str = STATUS_REPORTED,
    reported_ts: Optional[float] = None,
) -> bool:
    """Settle a claimed row to a terminal status. Returns True iff matched.

    ``handle`` is whatever :func:`record_inbound` or
    :func:`claim_oldest_pending` returned — the identity mapping. It replaces
    the old integer row id, which nothing in production ever used.

    ``status`` must be ``reported`` (push succeeded) or ``failed`` (push
    raised) — an unknown value raises rather than writing an unqueryable
    status (fail loudly, never silently).
    """
    if status not in (STATUS_REPORTED, STATUS_FAILED):
        raise ValueError(
            f"mark_reported status must be {STATUS_REPORTED!r} or "
            f"{STATUS_FAILED!r}, got {status!r}"
        )

    from scitex_dev.store import ANY_REVISION

    key = _key(handle)
    settle_ts = float(reported_ts) if reported_ts is not None else time.time()
    store = open_inbound_store()
    try:
        if store.get(key) is None:
            return False
        store.put(
            {**key, "status": status, "reported_ts": settle_ts},
            expected_revision=ANY_REVISION,
        )
        return True
    finally:
        store.close()


def list_inbound(
    *,
    agent: Optional[str] = None,
    status: Optional[str] = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """Return inbound rows (newest first) — observability / tests."""
    store = open_inbound_store()
    try:
        rows = [dict(row.values) for row in store.rows()]
    finally:
        store.close()
    if agent:
        rows = [r for r in rows if r.get("agent") == agent]
    if status:
        rows = [r for r in rows if r.get("status") == status]
    rows.sort(key=lambda r: float(r.get("ts") or 0.0), reverse=True)
    return rows[: int(limit)]
