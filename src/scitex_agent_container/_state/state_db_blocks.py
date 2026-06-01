"""Block-list — receiver-driven persistent silencing of a sender (task #27).

Operator-requested via lead (2026-06-01). Lead's design amendment: the
ACL approve-prompt flow boils down to BLOCK / UNBLOCK as the
primitive operations, dropping the held-message + TTL +
debounce-heuristic machinery.

* UNBLOCK is the existing :func:`state_db_nodes.grant_send` — writes
  the ``comms_grants`` row that lets the sender's future messages
  pass.
* BLOCK is this module's :func:`block_send` — writes a
  ``comms_blocks`` row that makes the sender's future ``message:send``
  attempts SILENTLY drop (no 403 trail, no receiver push, no
  approve-prompt re-fire). The receiver chose to silence the sender;
  the system honours that without further surface area.

Symmetric helpers (``unblock_send``, ``has_block``) mirror the
``grant_send`` / ``revoke_send`` / ``has_grant`` shape in
``state_db_nodes``.

Block precedence: a (sender, target) pair with BOTH a grant and a
block is denied (block wins). The receiver explicitly silenced the
sender after some earlier grant — honouring the more recent veto.
:func:`_listen._acl.check_send_acl` enforces this precedence.
"""

from __future__ import annotations

import time
from pathlib import Path

__all__ = [
    "ensure_comms_blocks_table",
    "block_send",
    "has_block",
    "unblock_send",
]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS comms_blocks (
    sender_name TEXT NOT NULL,
    target_name TEXT NOT NULL,
    created_at  REAL NOT NULL,
    note        TEXT,
    PRIMARY KEY (sender_name, target_name)
);
"""


def ensure_comms_blocks_table(db_path: Path | None = None) -> None:
    """Idempotent CREATE TABLE for ``comms_blocks``.

    Called from :func:`_state.state_db.init_schema` so a fresh
    state.db carries the table; safe to call multiple times.
    """
    from .state_db import open_db

    with open_db(db_path) as conn:
        conn.executescript(_SCHEMA)


def block_send(
    *,
    sender: str,
    target: str,
    db_path: Path | None = None,
    note: str | None = None,
) -> None:
    """Persist a ``sender → target`` block.

    Idempotent — re-blocking the same pair leaves the row untouched
    (timestamp not bumped). The optional ``note`` is a free-form
    audit annotation (e.g. the prompt msg_id the receiver was
    responding to).

    Fail-loud: empty ``sender`` / ``target`` raise ``ValueError``.
    """
    if not sender or not target:
        raise ValueError("block_send: sender and target must be non-empty")
    from .state_db import open_db

    ensure_comms_blocks_table(db_path)
    with open_db(db_path) as conn:
        existing = conn.execute(
            "SELECT 1 FROM comms_blocks WHERE sender_name = ? AND target_name = ?",
            (sender, target),
        ).fetchone()
        if existing is not None:
            return
        conn.execute(
            "INSERT INTO comms_blocks "
            "(sender_name, target_name, created_at, note) "
            "VALUES (?, ?, ?, ?)",
            (sender, target, time.time(), note),
        )


def unblock_send(
    *,
    sender: str,
    target: str,
    db_path: Path | None = None,
) -> bool:
    """Remove a ``sender → target`` block. Returns ``True`` iff a row
    was removed.

    ``unblock_send`` only deletes the block row — it does NOT write a
    grant. The receiver-side decision flow is "unblock = grant + clear
    pending"; the CLI/handler stitches the two calls together (see
    ``cli_pkg/a2a_group.py::a2a_unblock``).
    """
    if not sender or not target:
        return False
    from .state_db import open_db

    ensure_comms_blocks_table(db_path)
    with open_db(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM comms_blocks WHERE sender_name = ? AND target_name = ?",
            (sender, target),
        )
    return int(cur.rowcount or 0) > 0


def has_block(
    *,
    sender: str,
    target: str,
    db_path: Path | None = None,
) -> bool:
    """Return True iff ``(sender → target)`` is currently blocked.

    Used by :func:`_listen._acl.check_send_acl` as the FIRST gate
    after the trivial self-send / phase-3 checks. A blocked sender is
    silently dropped — no 403 reason, no receiver push, no
    approve-prompt re-fire.
    """
    if not sender or not target:
        return False
    from .state_db import open_db

    ensure_comms_blocks_table(db_path)
    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM comms_blocks WHERE sender_name = ? AND target_name = ?",
            (sender, target),
        ).fetchone()
    return row is not None
