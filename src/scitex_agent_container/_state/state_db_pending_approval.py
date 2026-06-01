"""Pending-prompt flag for the ACL block/unblock flow (task #27).

Operator-requested via lead (2026-06-01). Lead's design amendment
SUPERSEDED an earlier "hold the original message + replay on grant"
design in favour of a simpler BLOCK / UNBLOCK primitive:

* When a cross-group sender is denied, the receiver gets ONE push
  prompt naming the sender (content hidden — no leak pre-decision).
  The prompt embeds BOTH ``sac a2a unblock <s> <t>`` and
  ``sac a2a block <s> <t>`` so the receiver picks the verb.
* While a (sender, target) pair has a pending prompt, subsequent
  denied attempts from the same sender DO NOT re-prompt — the
  receiver already has the decision in front of them. This module
  owns that "is there a pending row" flag.
* The receiver's decision (either ``unblock`` → ``grant_send`` or
  ``block`` → ``block_send``) clears the pending row in the same
  transaction. No held message replay — if the sender wants their
  message delivered after unblock, they resend.
* No TTL, no expiry sweep, no latest-wins dedupe. The original
  spec's fragile spam-debounce logic is intentionally dropped — the
  primitive is just "is there a pending decision yes/no".

Schema is minimal: ``(sender, target, ts)``. The original message
content is NEVER stored here (the receiver decides on identity, not
on message content).

No-mocks (PA-306) testing — real on-disk sqlite, no monkeypatch.
"""

from __future__ import annotations

import time
from pathlib import Path

__all__ = [
    "ensure_pending_prompts_table",
    "clear_pending_prompt",
    "has_pending_prompt",
    "record_pending_prompt",
]


_SCHEMA = """
CREATE TABLE IF NOT EXISTS pending_prompts (
    sender TEXT NOT NULL,
    target TEXT NOT NULL,
    ts     REAL NOT NULL,
    PRIMARY KEY (sender, target)
);
"""


def ensure_pending_prompts_table(db_path: Path | None = None) -> None:
    """Idempotent CREATE TABLE for the pending-prompt flag store.

    Called from :func:`_state.state_db.init_schema` so a fresh
    state.db carries the table; safe to call multiple times.
    """
    from .state_db import open_db

    with open_db(db_path) as conn:
        conn.executescript(_SCHEMA)


def record_pending_prompt(
    *,
    sender: str,
    target: str,
    db_path: Path | None = None,
) -> bool:
    """Mark ``(sender, target)`` as "prompt-emitted, awaiting decision".

    Returns ``True`` iff this call is the FIRST pending-prompt for
    the pair (caller should emit the receiver-facing push). Returns
    ``False`` when a pending row already exists (caller suppresses
    re-prompt). The check + insert is one atomic transaction so a
    concurrent burst of denied attempts emits exactly one prompt.

    Fail-loud: empty ``sender`` / ``target`` raise ``ValueError``.
    """
    if not sender or not target:
        raise ValueError("record_pending_prompt: sender and target must be non-empty")
    from .state_db import open_db

    ensure_pending_prompts_table(db_path)
    with open_db(db_path) as conn:
        existing = conn.execute(
            "SELECT 1 FROM pending_prompts WHERE sender = ? AND target = ?",
            (sender, target),
        ).fetchone()
        if existing is not None:
            return False
        conn.execute(
            "INSERT INTO pending_prompts (sender, target, ts) VALUES (?, ?, ?)",
            (sender, target, time.time()),
        )
    return True


def has_pending_prompt(
    *,
    sender: str,
    target: str,
    db_path: Path | None = None,
) -> bool:
    """Return True iff ``(sender, target)`` has a pending prompt awaiting
    the receiver's block/unblock decision."""
    if not sender or not target:
        return False
    from .state_db import open_db

    ensure_pending_prompts_table(db_path)
    with open_db(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM pending_prompts WHERE sender = ? AND target = ?",
            (sender, target),
        ).fetchone()
    return row is not None


def clear_pending_prompt(
    *,
    sender: str,
    target: str,
    db_path: Path | None = None,
) -> bool:
    """Delete the pending row for ``(sender, target)``. Returns True iff
    a row was removed. Idempotent on absent rows.

    Called from both decision paths: ``grant_send`` / ``block_send``
    (unblock or block) clear the pending prompt in the same workflow.
    """
    if not sender or not target:
        return False
    from .state_db import open_db

    ensure_pending_prompts_table(db_path)
    with open_db(db_path) as conn:
        cur = conn.execute(
            "DELETE FROM pending_prompts WHERE sender = ? AND target = ?",
            (sender, target),
        )
    return int(cur.rowcount or 0) > 0
