"""comms_grants CRUD — explicit cross-group send permissions (WI-2).

Extracted from :mod:`.state_db_nodes` so that module stays under the
per-file line cap. The four primitives below are re-exported from
``state_db_nodes`` so the existing import surface is unchanged:

    from scitex_agent_container._state.state_db_nodes import (
        grant_send, revoke_send, has_grant, list_comms_grants,
    )

A grant is a directed ``sender → target`` row in the ``comms_grants``
table (schema in ``_SCHEMA_REGISTRY`` of :mod:`.state_db`). The sender
identity is authenticated by
:class:`scitex_agent_container._listen._acl.NodeAuthMiddleware`
resolving the bearer; the optional ``note`` is a free-form audit
annotation. Pure DB CRUD — no behavior change from the pre-extraction
definitions.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

__all__ = [
    "grant_send",
    "has_grant",
    "list_comms_grants",
    "revoke_send",
]


def grant_send(
    *,
    sender: str,
    target: str,
    db_path: Path | None = None,
    note: str | None = None,
) -> None:
    """Insert (or refresh) a cross-group grant ``sender → target``.

    Idempotent — re-granting the same pair leaves the row untouched
    (timestamp not bumped). The ``sender`` identity is authenticated
    by :class:`_listen._acl.NodeAuthMiddleware` resolving the bearer;
    the optional ``note`` is a free-form audit annotation (e.g. the
    ticket / handoff that authorised the grant).
    """
    if not sender or not target:
        raise ValueError("grant_send: sender and target must be non-empty")
    from .state_db import open_db

    with open_db(db_path) as conn:
        existing = conn.execute(
            "SELECT 1 FROM comms_grants WHERE sender_name = ? AND target_name = ?",
            (sender, target),
        ).fetchone()
        if existing is not None:
            return
        conn.execute(
            "INSERT INTO comms_grants "
            "(sender_name, target_name, created_at, note) "
            "VALUES (?, ?, ?, ?)",
            (sender, target, time.time(), note),
        )


def revoke_send(
    *,
    sender: str,
    target: str,
    db_path: Path | None = None,
) -> bool:
    """Remove a ``sender → target`` grant. Returns ``True`` iff a row
    was removed."""
    if not sender or not target:
        return False
    from .state_db import open_db

    with open_db(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM comms_grants WHERE sender_name = ? AND target_name = ?",
            (sender, target),
        )
    return cur.rowcount > 0


def has_grant(
    *,
    sender: str,
    target: str,
    db_path: Path | None = None,
) -> bool:
    """Return ``True`` iff a ``sender → target`` cross-group grant
    exists."""
    if not sender or not target:
        return False
    from .state_db import open_db

    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM comms_grants WHERE sender_name = ? AND target_name = ?",
            (sender, target),
        ).fetchone()
    return row is not None


def list_comms_grants(
    db_path: Path | None = None,
) -> list[dict[str, Any]]:
    """Return every grant row in insertion order.

    Observability surface for the host operator. Each row carries the
    audit ``note`` (default: the deferred-identity caveat).
    """
    from .state_db import open_db

    with open_db(db_path) as conn:
        cur = conn.execute(
            "SELECT sender_name, target_name, created_at, note "
            "FROM comms_grants "
            "ORDER BY created_at ASC, sender_name ASC, target_name ASC"
        )
        return [
            {
                "sender": str(r["sender_name"]),
                "target": str(r["target_name"]),
                "created_at": float(r["created_at"]),
                "note": (r["note"] if r["note"] is not None else None),
            }
            for r in cur.fetchall()
        ]
