"""ACL approve-prompt helpers (task #27 — operator-requested via lead).

Background: a cross-group ACL deny used to push only a metadata-only
``denied_attempt`` envelope at the receiver — the operator on the
other end saw a confusing "silent 403" and had no in-band way to
approve the sender. Task #27 adds two pieces alongside the existing
``denied_attempt`` notify:

1. The original (denied) message is HELD in the
   ``pending_approvals`` table (see
   :mod:`_state.state_db_pending_approval`) keyed on
   ``(sender, target)`` with latest-wins dedupe.
2. A NORMAL push prompt is emitted at the receiver carrying the
   exact ``sac a2a grant <sender> <target>`` command. On grant the
   held event is flushed into the receiver's inbox.

This module owns the two pure helpers that the
:func:`_listen._node_channel.node_message_send` deny branch and the
:mod:`cli_pkg.a2a_group` grant verb both call:

* :func:`_looks_like_cross_group_deny` — branch only on the deny
  reason that the receiver can REMEDY via grant. ``missing target``
  / spoof-identity denies are NOT remediable by granting and stay
  out of the approve-prompt path (they keep the existing
  ``denied_attempt`` notify-only behaviour).
* :func:`_mint_approval_prompt` — mint the receiver-facing push
  envelope. The ``content`` is the human-readable prompt the
  operator's Telegram bridge surfaces verbatim; the ``extra`` dict
  carries the structured fields a richer client could branch on
  (``approval_prompt: True``, ``approval_sender``,
  ``approval_grant_command``).

Pure functions — no I/O, no state-db access. Tests import them
directly without an in-process listen.
"""

from __future__ import annotations

from typing import Any

from ..a2a._inbox_bus import mint_event

__all__ = [
    "_looks_like_cross_group_deny",
    "_mint_approval_prompt",
    "approval_prompt_content",
    "block_command",
    "grant_command",
    "unblock_command",
]


# The ACL gate emits this distinctive substring on the cross-group
# deny path (see ``_listen._acl.check_send_acl``: ``"cross-group
# send: sender ..."``). We branch on the substring rather than the
# ``decision`` value because ``check_send_acl`` returns ``"deny"``
# for several reasons (spoof identity, missing target, phase-3
# relationship deny, ...) and only the cross-group case is remediable
# by a ``grant_send``. The other denies stay on the existing
# ``denied_attempt``-only path.
_CROSS_GROUP_DENY_MARKER = "cross-group send"


def _looks_like_cross_group_deny(reason: str | None) -> bool:
    """Return True iff the deny reason is a cross-group grant-remediable one.

    Spoof-identity / missing-target / phase-3 relationship denies are
    NOT in this set — they cannot be fixed by the receiver granting,
    so the approve-prompt path is skipped (the existing
    ``denied_attempt`` notify is enough).
    """
    if not reason:
        return False
    return _CROSS_GROUP_DENY_MARKER in reason


def unblock_command(sender: str, target: str) -> str:
    """Render the operator-facing CLI command that UNBLOCKS the pair.

    UNBLOCK = grant the sender + clear any block + clear the pending
    prompt row. The sender's future messages pass; the original
    denied message is NOT replayed (sender resends if needed). The
    shape mirrors the existing sac CLI conventions: positional
    ``<sender> <target>`` (sender first because that is the
    direction the grant points).
    """
    return f"sac a2a unblock {sender} {target}"


def block_command(sender: str, target: str) -> str:
    """Render the operator-facing CLI command that BLOCKS the pair.

    BLOCK persists ``sender → target`` in ``comms_blocks``; the
    sender's future attempts are silently dropped (no 403 trail at
    the receiver, no approve-prompt re-fire). Receiver chose to
    silence.
    """
    return f"sac a2a block {sender} {target}"


def grant_command(sender: str, target: str) -> str:
    """Render the legacy ``sac a2a grant`` command — alias of unblock.

    Kept as a thin alias for back-compat: the existing
    ``sac a2a grant <s> <t>`` verb still writes the
    ``comms_grants`` row directly. UNBLOCK is the receiver-driven
    framing introduced for task #27.
    """
    return f"sac a2a grant {sender} {target}"


def approval_prompt_content(sender: str, target: str) -> str:
    """Render the human-readable prompt content for the push.

    The Telegram bridge + any minimal inbox-rendering consumer
    surfaces this string verbatim. Body intentionally omits the
    actual message content — receivers are deciding on identity,
    not on message content (revealing the denied body pre-decision
    would defeat the ACL). Embeds BOTH the UNBLOCK and BLOCK
    commands so the receiver picks one.
    """
    return (
        f"Message from {sender!r} (content hidden). "
        f"UNBLOCK to allow this sender's future messages:\n\n"
        f"  {unblock_command(sender, target)}\n\n"
        f"BLOCK to silence this sender (future attempts dropped):\n\n"
        f"  {block_command(sender, target)}\n\n"
        "Both decisions are PERSISTENT. The original denied message "
        "is NOT replayed on unblock; the sender resends if needed."
    )


def _mint_approval_prompt(*, target: str, sender: str) -> dict[str, Any]:
    """Mint the approve-prompt push envelope (kind=``message``).

    Uses ``kind="message"`` so existing inbox consumers (Telegram
    bridge, SSE subscribers, …) surface it via the normal-message
    rendering path with zero code change. Structured fields ride
    in ``extra`` so a richer client can branch on
    ``extra.approval_prompt`` to render a dedicated UI element.
    """
    return mint_event(
        target,
        content=approval_prompt_content(sender, target),
        from_agent=sender,
        priority="normal",
        kind="message",
        extra={
            "approval_prompt": True,
            "approval_sender": sender,
            "approval_unblock_command": unblock_command(sender, target),
            "approval_block_command": block_command(sender, target),
        },
    )
