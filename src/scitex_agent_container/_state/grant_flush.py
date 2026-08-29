"""Decision helpers for the ACL block/unblock flow (task #27).

Lead's design amendment (2026-06-01) SUPERSEDED the held-message-
replay flow; the primitive is now just BLOCK / UNBLOCK. This module
owns the two decision helpers the CLI verbs (``sac a2a unblock`` and
``sac a2a block``, plus the legacy alias ``sac a2a grant``) share:

* :func:`unblock_and_clear_pending` — write ``comms_grants`` (so the
  sender's future messages pass) AND remove the row from
  ``comms_blocks`` (if previously blocked) AND clear the
  ``pending_prompts`` row so the receiver is not re-prompted on the
  next denied attempt (there should be none — they're granted now).
* :func:`block_and_clear_pending` — write ``comms_blocks`` (so the
  sender's future attempts are silently dropped) AND clear the
  ``pending_prompts`` row.

Both helpers are DB-only (no Starlette / broker dependency) so the
CLI verbs can run on the bare host without a live listen process.

No held-message replay: per lead's amendment, the sender resends if
they want their message delivered post-unblock. Trade-off accepted
in exchange for dropping the fragile TTL / dedupe / replay machinery.

Block precedence over grant: a (sender, target) pair with BOTH
rows is denied — the block wins. The receiver explicitly silenced
the sender after some earlier grant — the more recent veto is
honoured. :func:`_listen._acl.check_send_acl` enforces this at the
ACL check; this module just keeps the two stores independent.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

__all__ = [
    "block_and_clear_pending",
    "unblock_and_clear_pending",
]


def unblock_and_clear_pending(
    *,
    sender: str,
    target: str,
    note: str | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Grant + remove any block + clear the pending-prompt row.

    UNBLOCK semantics (lead's amendment): the sender's FUTURE messages
    pass. There is no held message to deliver — the sender resends if
    they want their original message through.

    Returns a JSON-friendly envelope::

      {
        "sender": str,
        "target": str,
        "granted": True,           # ``comms_grants`` row exists post-call
        "unblocked": bool,         # True iff a ``comms_blocks`` row was removed
        "cleared_pending": bool,   # True iff a ``pending_prompts`` row was removed
      }

    Idempotent — repeat calls on an already-granted pair with no
    block + no pending leave the state untouched and return
    ``unblocked=False, cleared_pending=False``.
    """
    if not sender or not target:
        raise ValueError(
            "unblock_and_clear_pending: sender and target must be non-empty"
        )
    # Local imports so the module loads cleanly even when state_db
    # is mid-migration (the schema-init paths reach into siblings
    # circularly; lazy is safer).
    from .state_db_blocks import unblock_send
    from .state_db_nodes import grant_send
    from .state_db_pending_approval import clear_pending_prompt

    grant_send(sender=sender, target=target, note=note)
    unblocked = unblock_send(sender=sender, target=target)
    cleared = clear_pending_prompt(sender=sender, target=target)
    return {
        "sender": sender,
        "target": target,
        "granted": True,
        "unblocked": unblocked,
        "cleared_pending": cleared,
    }


def block_and_clear_pending(
    *,
    sender: str,
    target: str,
    note: str | None = None,
    db_path: Path | None = None,
) -> dict[str, Any]:
    """Block + clear the pending-prompt row.

    BLOCK semantics: the sender's FUTURE attempts are silently dropped
    by :func:`_listen._acl.check_send_acl` (no 403 trail, no receiver
    push, no approve-prompt re-fire). The receiver chose to silence
    the sender; the system honours that without further surface.

    Returns::

      {
        "sender": str,
        "target": str,
        "blocked": True,
        "cleared_pending": bool,
      }

    Idempotent on the block side (repeat ``block_send`` is a no-op
    against the existing row). Does NOT remove an existing
    ``comms_grants`` row — the precedence rule in ``check_send_acl``
    (block wins) handles the simultaneous-grant case at decision
    time without needing to scrub the grants table.
    """
    if not sender or not target:
        raise ValueError("block_and_clear_pending: sender and target must be non-empty")
    from .state_db_blocks import block_send
    from .state_db_pending_approval import clear_pending_prompt

    block_send(sender=sender, target=target, note=note)
    cleared = clear_pending_prompt(sender=sender, target=target)
    return {
        "sender": sender,
        "target": target,
        "blocked": True,
        "cleared_pending": cleared,
    }
