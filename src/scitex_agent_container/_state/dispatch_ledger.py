"""Dispatch ledger — one row per OUTBOUND dispatch (2026-05-22).

Every dispatched turn/message gets a stable ``dispatch_id`` (uuid4 hex)
minted at the sender side and persisted here, so dispatches can be
filtered and recalled later ("which conversation did this belong to?").

This is the dispatch-ledger piece of the push-feedback architecture.

A ``dispatch_id`` is orthogonal to the other ids already in the system:

  * the a2a ``conversation_id`` correlates many messages in one
    conversation,
  * the a2a ``message_id`` identifies one message,
  * the receiver-side ``turn_id`` (``state_db.turns``) tracks the
    ``/v1/turn`` state machine.

The ledger row is the identity of one outbound *send action*. One
conversation produces many dispatches. The optional ``conversation_id``
column lets a later query group dispatches back into a conversation.

Why a NEW module + NEW table (not an addition to ``state_db.py``):
a sibling branch is concurrently restructuring the heartbeat code in
``state_db.py`` (adding a heartbeats module + a seq migration). Keeping
the ledger table + its ``CREATE TABLE IF NOT EXISTS`` here, behind its
own :func:`init_ledger_schema`, means the two branches never touch the
same lines. If a future sequence-numbered migration registry collides
at merge it is a trivial renumber.

The connection itself comes from :func:`state_db.open_db` (shared WAL +
busy-timeout + foreign-keys config); we only ensure our own table exists
on top of the state_db tables.

All times are stored as ``REAL`` unix-seconds (float), matching the
diary tables in :mod:`state_db_diary`.
"""

from __future__ import annotations

import sqlite3
import time
import uuid
from pathlib import Path

# Bound on the inline message summary so a runaway dispatch can't bloat
# state.db. Matches the diary tables' "first ~500 chars" convention.
_TEXT_SUMMARY_LIMIT = 500

# Valid lifecycle statuses. ``sent`` is the mint-time value; the others
# are terminal observations the sender can record once the round-trip
# resolves. Kept as a tuple (not an enum) so the column stays free-form
# TEXT and a richer state machine can push new values without a
# migration. ``record_dispatch`` validates against this set so a typo
# fails loudly instead of silently writing an unqueryable status.
STATUS_SENT = "sent"
STATUS_DELIVERED = "delivered"
STATUS_TIMEOUT = "timeout"
STATUS_FAILED = "failed"
VALID_STATUSES = (STATUS_SENT, STATUS_DELIVERED, STATUS_TIMEOUT, STATUS_FAILED)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS dispatches (
    dispatch_id      TEXT PRIMARY KEY,
    from_agent       TEXT,
    to_agent         TEXT,
    conversation_id  TEXT,
    text_summary     TEXT,
    status           TEXT NOT NULL,
    ts               REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_dispatches_from ON dispatches(from_agent, ts);
CREATE INDEX IF NOT EXISTS idx_dispatches_to ON dispatches(to_agent, ts);
CREATE INDEX IF NOT EXISTS idx_dispatches_status ON dispatches(status, ts);
CREATE INDEX IF NOT EXISTS idx_dispatches_conversation
    ON dispatches(conversation_id, ts);
"""


def new_dispatch_id() -> str:
    """Mint a fresh dispatch id (uuid4 hex)."""
    return uuid.uuid4().hex


def _clip(text: str | None, limit: int = _TEXT_SUMMARY_LIMIT) -> str | None:
    if text is None:
        return None
    if len(text) <= limit:
        return text
    return text[:limit]


def init_ledger_schema(db_path: Path | None = None) -> Path:
    """Create the ``dispatches`` table if missing. Idempotent.

    Returns the resolved database path. Delegates the base schema +
    connection config to :func:`state_db.open_db`, then layers our own
    ``CREATE TABLE IF NOT EXISTS`` so the ledger lives in the same
    ``state.db`` file as the registry and diary tables.
    """
    from .state_db import open_db

    with open_db(db_path) as conn:
        conn.executescript(_SCHEMA)
    from .state_db import init_schema

    return init_schema(db_path)


def _ensure_table(conn: sqlite3.Connection) -> None:
    """Ensure the ledger table exists on an already-open connection."""
    conn.executescript(_SCHEMA)


def record_dispatch(
    *,
    from_agent: str | None,
    to_agent: str | None,
    text: str | None = None,
    conversation_id: str | None = None,
    status: str = STATUS_SENT,
    dispatch_id: str | None = None,
    ts: float | None = None,
    db_path: Path | None = None,
) -> str:
    """Insert one ``dispatches`` row. Returns the ``dispatch_id``.

    Mints a uuid4-hex ``dispatch_id`` when none is supplied (the caller
    usually mints it earlier so it can thread the same id onto the wire).
    ``text`` is the dispatched message body — stored truncated to the
    first ~500 chars as ``text_summary``.

    ``status`` must be one of :data:`VALID_STATUSES`; an unknown value
    raises ``ValueError`` rather than silently writing an unqueryable
    status (fail loudly, never silently).
    """
    if status not in VALID_STATUSES:
        raise ValueError(
            f"unknown dispatch status {status!r}; expected one of {VALID_STATUSES}"
        )
    did = dispatch_id or new_dispatch_id()
    row_ts = float(ts) if ts is not None else time.time()

    from .state_db import open_db

    with open_db(db_path) as conn:
        _ensure_table(conn)
        conn.execute(
            """
            INSERT INTO dispatches (
                dispatch_id, from_agent, to_agent, conversation_id,
                text_summary, status, ts
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                did,
                from_agent,
                to_agent,
                conversation_id,
                _clip(text),
                status,
                row_ts,
            ),
        )
    return did


def update_dispatch_status(
    dispatch_id: str,
    status: str,
    *,
    db_path: Path | None = None,
) -> bool:
    """Update the ``status`` of an existing dispatch. Returns True iff a row matched.

    ``status`` must be one of :data:`VALID_STATUSES`. The ledger row is
    minted ``sent`` at dispatch time; the sender calls this once the
    round-trip resolves to ``delivered`` / ``timeout`` / ``failed``.
    """
    if status not in VALID_STATUSES:
        raise ValueError(
            f"unknown dispatch status {status!r}; expected one of {VALID_STATUSES}"
        )
    from .state_db import open_db

    with open_db(db_path) as conn:
        _ensure_table(conn)
        cur = conn.execute(
            "UPDATE dispatches SET status=? WHERE dispatch_id=?",
            (status, dispatch_id),
        )
        return cur.rowcount > 0


def list_dispatches(
    *,
    from_agent: str | None = None,
    to_agent: str | None = None,
    status: str | None = None,
    conversation_id: str | None = None,
    since: float | None = None,
    limit: int | None = None,
    db_path: Path | None = None,
) -> list[dict]:
    """Return ledger rows matching the filters, newest first.

    Every filter is optional and AND-combined. ``since`` is a unix-second
    lower bound (``ts >= since``). With no filters, returns the whole
    ledger (subject to ``limit``). The query is parameterised — no
    user value is str-formatted into the SQL.
    """
    clauses: list[str] = []
    params: list[object] = []
    if from_agent is not None:
        clauses.append("from_agent = ?")
        params.append(from_agent)
    if to_agent is not None:
        clauses.append("to_agent = ?")
        params.append(to_agent)
    if status is not None:
        clauses.append("status = ?")
        params.append(status)
    if conversation_id is not None:
        clauses.append("conversation_id = ?")
        params.append(conversation_id)
    if since is not None:
        clauses.append("ts >= ?")
        params.append(float(since))

    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    limit_sql = ""
    if limit is not None:
        limit_sql = " LIMIT ?"
        params.append(int(limit))

    from .state_db import open_db

    with open_db(db_path) as conn:
        _ensure_table(conn)
        rows = conn.execute(
            f"SELECT * FROM dispatches{where} ORDER BY ts DESC{limit_sql}",
            tuple(params),
        ).fetchall()
        return [dict(r) for r in rows]


__all__ = [
    "STATUS_DELIVERED",
    "STATUS_FAILED",
    "STATUS_SENT",
    "STATUS_TIMEOUT",
    "VALID_STATUSES",
    "init_ledger_schema",
    "list_dispatches",
    "new_dispatch_id",
    "record_dispatch",
    "update_dispatch_status",
]
