"""Dispatch ledger — one row per OUTBOUND dispatch (2026-05-22), on
PostgreSQL only.

Every dispatched turn/message gets a stable ``dispatch_id`` (uuid4 hex) minted
at the sender side and persisted here, so dispatches can be filtered and
recalled later ("which conversation did this belong to?"). It is the
sender-side half of the push-feedback architecture; :mod:`.inbound_ledger` is
the receiver-side mirror. A row is the identity of one outbound *send action*,
which is why the id is orthogonal to the a2a ``conversation_id`` (many
messages), the a2a ``message_id`` (one message) and the receiver-side
``turn_id`` (the diary's ``turns`` store, on per-host PostgreSQL since
2026-08-28, which tracks the ``/v1/turn`` state machine).

WHY THIS MODULE IS ON THE SHARED STORE
========================================
The operator's 2026-08-19 order was to move every table to
PostgreSQL: "fail fast, fail loud, no fallbacks". This table moves the way
every predecessor did — by ADOPTING :mod:`scitex_dev.store` rather than by sac
growing a private psycopg layer. ``db_path`` IS GONE from every function; it
named a file and there is no file. A host whose PostgreSQL is
unreachable raises ``StoreTargetError`` naming the DSN, which is intended: a
ledger written to a private local file nobody reads is worse than no ledger.

THE DEFECT THAT HAD TO BE FIXED FIRST, in one line: ``state.db`` was PER-AGENT
and ``SCITEX_STORE_DSN`` is FLEET-WIDE, so the shard that used to do the
scoping disappears and an unfiltered read starts answering for 130+ agents
without raising anything. The fix is an OWNING-AGENT field in the store
identity — ``agent``, distinct from the nullable ``from_agent`` — and the
whole argument lives in :mod:`.dispatch_ledger_store`, next to the schema that
implements it. Read that before changing this file's read surface.

``update_dispatch_status`` IS O(1) WHEN TOLD THE OWNER AND O(n) WHEN NOT.
``agent=`` there is a FAST PATH, NOT A FILTER: the identity is a pair, so a
caller that names the owner gets a keyed write, and one that does not — or one
whose idea of the owner disagrees with the writer's — falls back to a scan for
the unique ``dispatch_id`` and still finds the row. Making it a filter instead
was tried first and lost a status update silently; :func:`_find_row` carries
the measurement. The OWNING AGENT SCOPES READS, which is where the fleet-wide
leak lives, and is not a permission check on a write.

NOTHING IS MIGRATED IN FROM THE 130+ PER-AGENT SHARDS, deliberately. A dispatch
row records a send that already happened; the recall and comm-miss surfaces
that read it have no production callers, and a comm-miss report is only
actionable inside its SLO window (seconds to minutes), so importing history
would import noise. The old files stay on disk, readable with any
client, for anyone who wants them.

All times are unix-seconds (float), matching the diary tables.
"""

from __future__ import annotations

import time
import uuid
from typing import TYPE_CHECKING, Optional

from .dispatch_ledger_store import (
    IDENTITY_FIELDS,
    STORE_NAME,
    dispatch_store_target,
    open_dispatch_store,
    sorted_values,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from scitex_dev.store import Row, Store

# Bound on the inline message summary so a runaway dispatch can't bloat the
# ledger. Matches the diary tables' "first ~500 chars" convention.
_TEXT_SUMMARY_LIMIT = 500

# Valid lifecycle statuses. ``sent`` is the mint-time value; the others are
# terminal observations the sender records once the round-trip resolves. Kept
# as a tuple (not an enum) so the field stays free-form TEXT and a richer
# state machine can push new values without a migration. ``record_dispatch``
# validates against this set so a typo fails loudly instead of silently
# writing an unqueryable status.
STATUS_SENT = "sent"
STATUS_DELIVERED = "delivered"
# STATUS_REACTED — the receiver's channel adapter posted a structural reaction
# ack back to the sender (👀 marker). Distinct from ``delivered`` (the listen
# server's publish HTTP 200, which does NOT prove the recipient's adapter
# picked the event up): REACTED is the operator's "comm-miss detectable"
# signal. See lead a2a 1781e82a (2026-06-14).
STATUS_REACTED = "reacted"
STATUS_TIMEOUT = "timeout"
STATUS_FAILED = "failed"
VALID_STATUSES = (
    STATUS_SENT,
    STATUS_DELIVERED,
    STATUS_REACTED,
    STATUS_TIMEOUT,
    STATUS_FAILED,
)

__all__ = [
    "IDENTITY_FIELDS",
    "STATUS_DELIVERED",
    "STATUS_FAILED",
    "STATUS_REACTED",
    "STATUS_SENT",
    "STATUS_TIMEOUT",
    "STORE_NAME",
    "VALID_STATUSES",
    "dispatch_store_target",
    "init_ledger_schema",
    "list_dispatches",
    "list_unreacted_dispatches",
    "mark_dispatch_reacted",
    "new_dispatch_id",
    "open_dispatch_store",
    "record_dispatch",
    "update_dispatch_status",
]


def new_dispatch_id() -> str:
    """Mint a fresh dispatch id (uuid4 hex)."""
    return uuid.uuid4().hex


def _clip(text: str | None, limit: int = _TEXT_SUMMARY_LIMIT) -> str | None:
    if text is None:
        return None
    if len(text) <= limit:
        return text
    return text[:limit]


def init_ledger_schema() -> str:
    """Create the ledger tables if missing. Idempotent.

    Returns the resolved store LOCATOR as a string — the PostgreSQL equivalent
    of the ``Path`` the previous implementation returned, and useful the same way: it
    NAMES where the state actually went, so an operator can check rather than
    assume.
    """
    store = open_dispatch_store()
    try:
        return str(dispatch_store_target().locator)
    finally:
        store.close()


def record_dispatch(
    *,
    from_agent: str | None,
    to_agent: str | None,
    text: str | None = None,
    conversation_id: str | None = None,
    status: str = STATUS_SENT,
    dispatch_id: str | None = None,
    ts: float | None = None,
    agent: str | None = None,
) -> str:
    """Insert one dispatch row. Returns the ``dispatch_id``.

    Mints a uuid4-hex ``dispatch_id`` when none is supplied (the caller
    usually mints it earlier so it can thread the same id onto the wire).
    ``text`` is the dispatched message body — stored truncated to the first
    ~500 chars as ``text_summary``.

    ``agent`` is the OWNING agent: the process whose ledger this row is, which
    is NOT the same question as ``from_agent`` (the sender named on the row,
    and legitimately ``None``). It is what the scoped reads filter on. Omitting
    it records the row as unowned (``""``), which an unfiltered read still
    returns and no scoped read does — see :mod:`.dispatch_ledger_store`.

    ``status`` must be one of :data:`VALID_STATUSES`; an unknown value raises
    ``ValueError`` rather than silently writing an unqueryable status (fail
    loudly, never silently).
    """
    if status not in VALID_STATUSES:
        raise ValueError(
            f"unknown dispatch status {status!r}; expected one of {VALID_STATUSES}"
        )

    from scitex_dev.store import NEW_RECORD

    did = dispatch_id or new_dispatch_id()
    row_ts = float(ts) if ts is not None else time.time()
    store = open_dispatch_store()
    try:
        store.put(
            {
                "agent": agent or "",
                "dispatch_id": did,
                "from_agent": from_agent,
                "to_agent": to_agent,
                "conversation_id": conversation_id,
                "text_summary": _clip(text),
                "status": status,
                "ts": row_ts,
            },
            expected_revision=NEW_RECORD,
        )
    finally:
        store.close()
    return did


def _find_row(store: Store, dispatch_id: str, agent: str | None) -> Row | None:
    """The row for ``dispatch_id``. ``agent`` is a fast path, not a filter.

    A non-empty ``agent`` completes the identity, so it is tried first as a
    keyed ``get``. ON A MISS THE LEDGER IS STILL SCANNED, and that fallback is
    not belt-and-braces — MEASURED 2026-08-28, its absence broke
    ``test_inbound_reaction_updates_dispatch_ledger``:

      * ``_network/_peer_dispatch`` stamps the owner from ``SAC_NAME``;
      * ``_mcp/channel`` stamps it from ``--name`` or the discovered self
        spec.

    Two resolvers for the same question. When they disagree — or when a row
    was written by an ops script with no owner at all and a named agent later
    absorbs its reaction — a strictly keyed update matches NOTHING, returns
    ``False``, and the status silently never moves. That failure did not exist
    before this table was shared, and it must not be introduced by sharing it.

    Resolving by ``dispatch_id`` is exactly the pre-migration semantics and is
    safe for the same reason it was then: the id is a uuid4 minted by the
    sender, so it is unique across the shared table and unguessable by anyone
    who was not told it. THE OWNING AGENT SCOPES READS, which is where the
    fleet-wide leak lives; it is not a permission check on a write.
    """
    if agent:
        row = store.get({"agent": agent, "dispatch_id": dispatch_id})
        if row is not None:
            return row
    for row in store.rows():
        if row.values.get("dispatch_id") == dispatch_id:
            return row
    return None


def update_dispatch_status(
    dispatch_id: str,
    status: str,
    *,
    agent: str | None = None,
) -> bool:
    """Update the ``status`` of an existing dispatch. True iff a row matched.

    ``status`` must be one of :data:`VALID_STATUSES`. The row is minted
    ``sent`` at dispatch time; the sender calls this once the round-trip
    resolves to ``delivered`` / ``timeout`` / ``failed``.

    ``agent`` is the OWNER the row was recorded under, and it is a FAST PATH
    rather than a filter — pass it and the write is keyed and O(1); omit it,
    or name an owner the row does not carry, and the ledger is scanned for the
    uuid4 ``dispatch_id`` instead, which is correct but linear. See
    :func:`_find_row` for why the miss must fall back rather than report a
    miss.

    Idempotent: re-writing the same status is accepted, because ``status`` is
    the one LAST_WRITER_WINS field in the schema. The surrounding IMMUTABLE
    facts are re-put unchanged, which the store also accepts — measured
    2026-08-28 against PostgreSQL 16 rather than assumed, because an IMMUTABLE
    rule that rejected an identical re-write would make every status
    transition raise.
    """
    if status not in VALID_STATUSES:
        raise ValueError(
            f"unknown dispatch status {status!r}; expected one of {VALID_STATUSES}"
        )

    from scitex_dev.store import ANY_REVISION

    store = open_dispatch_store()
    try:
        row = _find_row(store, dispatch_id, agent)
        if row is None:
            return False
        values = dict(row.values)
        values["status"] = status
        store.put(values, expected_revision=ANY_REVISION)
        return True
    finally:
        store.close()


def mark_dispatch_reacted(dispatch_id: str, *, agent: str | None = None) -> bool:
    """Mark a dispatch REACTED (receiver injected, structural ack received).

    Thin wrapper over :func:`update_dispatch_status` pinning the status to
    :data:`STATUS_REACTED`. Used by the sender-side channel adapter when it
    receives a ``kind="reaction"`` envelope whose ``extra.reacted_dispatch_id``
    matches an outbound row.

    Returns ``True`` iff a row matched — a ``False`` result is the audit signal
    that the reaction landed for a dispatch this sender never minted
    (out-of-order replay, wrong sender, or stale ledger).
    """
    return update_dispatch_status(dispatch_id, STATUS_REACTED, agent=agent)


def list_unreacted_dispatches(
    *,
    older_than_s: float,
    agent: Optional[str] = None,
    from_agent: str | None = None,
    to_agent: str | None = None,
) -> list[dict]:
    """Dispatches the receiver has not REACTED to within the SLO, newest first.

    "Comm-miss detection" surface (lead a2a 1781e82a, 2026-06-14): structural
    reaction-ack means the SENDER can poll this and see exactly which outbound
    messages never produced a 👀 from the recipient's channel adapter. Absence
    of a reaction past ``older_than_s`` seconds = the recipient never injected
    the message (their adapter is down, disconnected, or wedged).

    ``older_than_s`` (REQUIRED) keeps rows with ``ts <= now - older_than_s``: a
    freshly-minted dispatch that has not had time to be REACTED yet is NOT a
    miss, and the SLO is the operator's choice (30s interactive, 5min batch).
    ``agent`` scopes to the OWNING agent, and THIS SURFACE LEAKS WORST WITHOUT
    IT — a fleet-wide answer does not merely add noise, it manufactures alerts
    about peers this agent never dispatched to, and the natural response to one
    of those is to re-send. ``from_agent`` / ``to_agent`` narrow to one pair.

    Terminal-failure rows (``failed``, ``timeout``) are excluded — those are
    ALREADY known not to have landed and would be noise in a comm-miss
    dashboard. ``reacted`` rows are excluded too (the success case). Result:
    ``sent`` and ``delivered`` rows older than the SLO with no reaction.
    """
    cutoff = time.time() - float(older_than_s)
    store = open_dispatch_store()
    try:
        rows = sorted_values(store.rows())
    finally:
        store.close()
    rows = [
        r
        for r in rows
        if r.get("status") in (STATUS_SENT, STATUS_DELIVERED)
        and float(r.get("ts") or 0.0) <= cutoff
    ]
    if agent is not None:
        rows = [r for r in rows if r.get("agent") == agent]
    if from_agent is not None:
        rows = [r for r in rows if r.get("from_agent") == from_agent]
    if to_agent is not None:
        rows = [r for r in rows if r.get("to_agent") == to_agent]
    return rows


def list_dispatches(
    *,
    agent: Optional[str] = None,
    from_agent: str | None = None,
    to_agent: str | None = None,
    status: str | None = None,
    conversation_id: str | None = None,
    since: float | None = None,
    limit: int | None = None,
) -> list[dict]:
    """Return ledger rows matching the filters, newest first.

    Every filter is optional and AND-combined. ``agent`` is the OWNING agent —
    the one filter that makes this "my dispatches" rather than the fleet's; see
    :mod:`.dispatch_ledger_store` for why an unfiltered read is still
    fleet-wide on purpose. ``since`` is a unix-second lower bound
    (``ts >= since``).
    """
    store = open_dispatch_store()
    try:
        rows = sorted_values(store.rows())
    finally:
        store.close()
    if agent is not None:
        rows = [r for r in rows if r.get("agent") == agent]
    if from_agent is not None:
        rows = [r for r in rows if r.get("from_agent") == from_agent]
    if to_agent is not None:
        rows = [r for r in rows if r.get("to_agent") == to_agent]
    if status is not None:
        rows = [r for r in rows if r.get("status") == status]
    if conversation_id is not None:
        rows = [r for r in rows if r.get("conversation_id") == conversation_id]
    if since is not None:
        rows = [r for r in rows if float(r.get("ts") or 0.0) >= float(since)]
    if limit is not None:
        rows = rows[: int(limit)]
    return rows
