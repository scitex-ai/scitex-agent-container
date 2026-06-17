"""Inbound dispatch ledger — one row per INBOUND dispatch awaiting a
completion report (2026-06-18).

The receiver-side mirror of :mod:`dispatch_ledger` (which is the SENDER's
outbound record). It exists for the ``runtime: tui`` push-feedback loop:
a TUI agent has no in-process turn envelope, so the requester identity
(``from_agent`` + ``dispatch_id``) of a bus-pushed wake cannot ride the
tmux-injected text from the host-side bridge through to the in-container
``Stop`` hook that reports completion. This SQLite table is that bridge —
the SAME ``state.db`` is bound into the container (``/state/<name>``), so
the host-side writer and the in-container reader share it (WAL +
``busy_timeout`` from :func:`state_db.open_db` make the cross-process
access safe).

Lifecycle of one row:

  * ``pending``   — the bridge recorded a requester-bearing inbound wake
    (:func:`record_inbound`); the turn is queued/running in the TUI.
  * ``reporting`` — the Stop hook atomically CLAIMED the oldest pending
    row (:func:`claim_oldest_pending`) to push its completion. The claim
    is a single ``UPDATE`` so two concurrent Stop hooks (or a retry)
    cannot double-report the same dispatch.
  * ``reported`` / ``failed`` — terminal, set by :func:`mark_reported`
    after the completion push to the requester succeeds / fails loud.

FIFO by ``ts`` + sequential TUI turn processing keeps the dispatch↔turn
correlation correct: the Nth ``Stop`` claims the Nth recorded dispatch.

Sibling-module rationale (carried from :mod:`dispatch_ledger`): the table
+ its ``CREATE TABLE IF NOT EXISTS`` live here behind
:func:`init_inbound_schema`, layered on top of ``state_db`` via
``open_db``, so this feature never edits the same lines as a concurrent
``state_db.py`` restructure.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Optional

# Row lifecycle. ``record_inbound`` always writes ``pending``; the Stop
# hook claims to ``reporting`` then settles to ``reported`` / ``failed``.
STATUS_PENDING = "pending"
STATUS_REPORTING = "reporting"
STATUS_REPORTED = "reported"
STATUS_FAILED = "failed"
VALID_STATUSES = (STATUS_PENDING, STATUS_REPORTING, STATUS_REPORTED, STATUS_FAILED)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS inbound_dispatches (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    agent        TEXT NOT NULL,
    from_agent   TEXT NOT NULL,
    dispatch_id  TEXT,
    status       TEXT NOT NULL DEFAULT 'pending',
    ts           REAL NOT NULL,
    reported_ts  REAL
);
CREATE INDEX IF NOT EXISTS idx_inbound_agent_status_ts
    ON inbound_dispatches(agent, status, ts);
"""

__all__ = [
    "STATUS_PENDING",
    "STATUS_REPORTING",
    "STATUS_REPORTED",
    "STATUS_FAILED",
    "VALID_STATUSES",
    "init_inbound_schema",
    "record_inbound",
    "claim_oldest_pending",
    "mark_reported",
    "list_inbound",
]


def init_inbound_schema(db_path: Path | None = None) -> Path:
    """Create the ``inbound_dispatches`` table if missing. Idempotent.

    Returns the resolved database path. Mirrors
    :func:`dispatch_ledger.init_ledger_schema`: delegates base schema +
    connection config to :func:`state_db.open_db`, then layers our own
    ``CREATE TABLE IF NOT EXISTS``.
    """
    from .state_db import init_schema, open_db

    with open_db(db_path) as conn:
        conn.executescript(_SCHEMA)
    return init_schema(db_path)


def _ensure_table(conn: sqlite3.Connection) -> None:
    """Ensure the inbound table exists on an already-open connection."""
    conn.executescript(_SCHEMA)


def record_inbound(
    *,
    agent: str,
    from_agent: str,
    dispatch_id: Optional[str] = None,
    ts: float | None = None,
    db_path: Path | None = None,
) -> int:
    """Insert one ``pending`` inbound-dispatch row; return its ``id``.

    Called by the bridge when an inbound wake carries a ``from_agent``
    (the peer to report back to). A wake with no requester is NOT recorded
    by the caller — there is nobody to report to. ``ts`` is a real-time
    injection seam for tests.
    """
    if not agent or not from_agent:
        raise ValueError("record_inbound requires non-empty agent + from_agent")
    row_ts = float(ts) if ts is not None else time.time()
    from .state_db import open_db

    with open_db(db_path) as conn:
        _ensure_table(conn)
        cur = conn.execute(
            """
            INSERT INTO inbound_dispatches (agent, from_agent, dispatch_id, status, ts)
            VALUES (?, ?, ?, ?, ?)
            """,
            (agent, from_agent, dispatch_id, STATUS_PENDING, row_ts),
        )
        # lastrowid is always set after a successful single-row INSERT;
        # the ``or 0`` only satisfies the int|None type and never fires.
        return int(cur.lastrowid or 0)


def claim_oldest_pending(
    *,
    agent: str,
    db_path: Path | None = None,
) -> dict[str, Any] | None:
    """Atomically claim the OLDEST ``pending`` row for ``agent``.

    Flips exactly one row ``pending → reporting`` inside a single
    ``BEGIN IMMEDIATE`` transaction and returns it (as a dict), or
    ``None`` when the agent has no pending dispatch (the common no-op —
    most turns have no requester). The atomic claim means two concurrent
    ``Stop`` hooks (or a hook retry) can never push the same completion
    twice. The caller settles the claimed row via :func:`mark_reported`.
    """
    from .state_db import open_db

    with open_db(db_path) as conn:
        _ensure_table(conn)
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                """
                SELECT * FROM inbound_dispatches
                WHERE agent = ? AND status = ?
                ORDER BY ts ASC, id ASC
                LIMIT 1
                """,
                (agent, STATUS_PENDING),
            ).fetchone()
            if row is None:
                conn.execute("COMMIT")
                return None
            conn.execute(
                "UPDATE inbound_dispatches SET status = ? WHERE id = ?",
                (STATUS_REPORTING, row["id"]),
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        claimed = dict(row)
        claimed["status"] = STATUS_REPORTING
        return claimed


def mark_reported(
    row_id: int,
    *,
    status: str = STATUS_REPORTED,
    reported_ts: float | None = None,
    db_path: Path | None = None,
) -> bool:
    """Settle a claimed row to a terminal status. Returns True iff matched.

    ``status`` must be ``reported`` (push succeeded) or ``failed`` (push
    raised) — an unknown value raises rather than writing an unqueryable
    status (fail loudly, never silently).
    """
    if status not in (STATUS_REPORTED, STATUS_FAILED):
        raise ValueError(
            f"mark_reported status must be {STATUS_REPORTED!r} or "
            f"{STATUS_FAILED!r}, got {status!r}"
        )
    row_ts = float(reported_ts) if reported_ts is not None else time.time()
    from .state_db import open_db

    with open_db(db_path) as conn:
        _ensure_table(conn)
        cur = conn.execute(
            "UPDATE inbound_dispatches SET status = ?, reported_ts = ? WHERE id = ?",
            (status, row_ts, row_id),
        )
        return cur.rowcount > 0


def list_inbound(
    *,
    agent: str | None = None,
    status: str | None = None,
    limit: int = 100,
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Return inbound rows (newest first) — observability / tests."""
    from .state_db import open_db

    clauses: list[str] = []
    params: list[Any] = []
    if agent:
        clauses.append("agent = ?")
        params.append(agent)
    if status:
        clauses.append("status = ?")
        params.append(status)
    where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
    params.append(int(limit))
    with open_db(db_path) as conn:
        _ensure_table(conn)
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            f"SELECT * FROM inbound_dispatches{where} ORDER BY ts DESC, id DESC LIMIT ?",
            params,
        ).fetchall()
        return [dict(r) for r in rows]
