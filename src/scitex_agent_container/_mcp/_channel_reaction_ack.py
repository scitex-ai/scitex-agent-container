"""sac channel adapter — structural reaction-ack subsystem.

Operator mandate (lead a2a ``1781e82a``, 2026-06-14):

    "when an agent receives an a2a/telegram directive, it auto-reacts
    (👀/✓) so the sender KNOWS it landed — absence of the reaction =
    comm miss, detectable."

The existing :mod:`._channel_auto_ack` path emits a CONTENTLESS ack
that the sender-side empty-ack noise filter
(:mod:`._channel_ack_filter`) intentionally drops BEFORE it leaves the
outbound queue. That filter is the operator's "no contentless acks on
the wire" contract; it cannot be relaxed without re-introducing the
ack ping-pong noise.

This module adds a parallel, **structural** reaction-ack that DOES
reach the wire:

* Content is a non-empty marker (``👀`` by default — configurable via
  ``SAC_REACTION_ACK_MARKER``) so the empty-ack filter does NOT match.
* ``kind="reaction"`` so the sender's receive-side can recognise the
  envelope and update the dispatch ledger (mark ``reacted``) instead
  of just letting the marker land in the inbox as a regular message.
* Carries ``extra.reacted_dispatch_id`` (the SENDER's dispatch-ledger
  id, threaded through the original inbound event's ``dispatch_id``
  field) so the sender can mark the exact outbound row REACTED
  without a fuzzy text match.

The receive-side hook lives in
:mod:`._channel_reaction_ack`: :func:`should_emit_reaction_ack` is the
loop-guard predicate (never react to a reaction; never react if there
is no identifiable original sender), and :func:`post_reaction_ack`
posts the marker back to the sender's inbox.

Loop guards mirror the auto-ack path (event ``kind`` and ``ack`` flag
exclusions), with one extra exclusion for ``kind="reaction"`` itself
so a future regression cannot trigger a 👀-on-👀 cycle. There is also
a per-sender sliding-window rate cap (reusing the auto-ack rate cap's
env knobs — same rule applies: the structural ack is best-effort,
NEVER blocks delivery, and any failure is logged loudly).
"""

from __future__ import annotations

import logging
import os
from typing import Any

from .._env import getenv as _sac_env
from ..a2a._inbox_bus import DAEMON_SENDER

log = logging.getLogger(__name__)

__all__ = [
    "DEFAULT_REACTION_MARKER",
    "absorb_reaction_ack",
    "is_reaction_event",
    "post_reaction_ack",
    "reaction_ack_enabled",
    "reaction_ack_marker",
    "should_emit_reaction_ack",
]


def is_reaction_event(event: dict[str, Any]) -> bool:
    """Return True iff ``event`` is a structural reaction-ack envelope.

    Single source of truth for the predicate ``kind == "reaction"`` so
    callers don't sprinkle the string literal across multiple modules.
    """
    return event.get("kind") == "reaction"


def absorb_reaction_ack(event: dict[str, Any]) -> bool:
    """Update the dispatch ledger if ``event`` carries a structural reaction.

    The sender-side companion to :func:`post_reaction_ack`: when a
    ``kind="reaction"`` event lands on THIS agent's inbox, it is the
    receiver's 👀 receipt to one of OUR previous outbound dispatches.
    Mark the matching dispatch row ``STATUS_REACTED`` so the operator
    (and the ``sac a2a comm-miss`` surface) can see the receipt
    landed.

    Returns ``True`` iff a ledger row was updated. Best-effort: a
    write failure is logged but never re-raised — losing
    observability must not break the SSE consumer. A reaction
    without a ``reacted_dispatch_id`` in ``extra`` (legacy senders /
    senders that never minted a ledger row) is a no-op and returns
    ``False``; this is deliberate, not silent: there is simply no
    row to update.
    """
    if not is_reaction_event(event):
        return False
    extra = event.get("extra")
    if not isinstance(extra, dict):
        return False
    did = extra.get("reacted_dispatch_id")
    if not isinstance(did, str) or not did:
        return False
    try:
        from .._state.dispatch_ledger import mark_dispatch_reacted
    except Exception as exc:  # stx-allow: fallback (reason: optional state subsystem may fail to import in slim test environments)
        log.warning("sac channel: dispatch_ledger import failed: %s", exc)
        return False
    try:
        return mark_dispatch_reacted(did)
    except Exception as exc:  # stx-allow: fallback (reason: ledger is observability; a write failure must not break the SSE consumer — logged loudly, never silent)
        log.warning(
            "sac channel: mark_dispatch_reacted(%r) failed: %s",
            did,
            exc,
        )
        return False


# Default visible marker. Unicode "eyes" — the same emoji the lead's
# Telegram doctrine uses to acknowledge a received directive. Operators
# can pick a different marker (e.g. "✓") via env override. Multi-char
# strings are passed through verbatim so a workflow that wants
# "👀 seen" works without code changes.
DEFAULT_REACTION_MARKER = "\N{EYES}"


def reaction_ack_enabled() -> bool:
    """Whether the receive-side adapter emits a structural reaction-ack
    on every inbound bus event injection.

    Default ON. Disable with ``SAC_REACTION_ACK=0`` (also accepts
    ``false`` / ``no`` / ``off``, case-insensitive). The ``SAC_*`` prefix
    matches the rest of the channel env knobs so a single sac listen
    process toggles both halves of the receipt machinery from the same
    env scope.
    """
    raw = os.environ.get("SAC_REACTION_ACK")
    if raw is None:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off")


def reaction_ack_marker() -> str:
    """The content string the structural reaction-ack posts back.

    Reads ``SAC_REACTION_ACK_MARKER`` (also via the sac-env prefix
    fallback). Empty / whitespace-only overrides revert to
    :data:`DEFAULT_REACTION_MARKER` — an empty marker would be the very
    contentless ack the noise filter is designed to drop, so we
    refuse it loudly (via the silent default) rather than emit a
    suppressed-then-dropped pair.
    """
    raw = _sac_env("REACTION_ACK_MARKER", DEFAULT_REACTION_MARKER)
    if raw is None or not raw.strip():
        return DEFAULT_REACTION_MARKER
    return raw


def should_emit_reaction_ack(event: dict[str, Any]) -> bool:
    """Loop-guard for the structural reaction-ack side-effect.

    Skip when:

    * the event has no identifiable original sender
      (``from_agent`` missing/empty) — there is nowhere to send the
      reaction;
    * the event is itself a contentless ack (``ack`` flag truthy) —
      no point reacting to a delivery receipt;
    * the event is itself a structural reaction
      (``kind == "reaction"``) — belt-and-suspenders so a future
      regression in marker handling cannot start a 👀-on-👀 cycle;
    * the event is a daemon / synthetic notification
      (``from_agent`` is a bare ``"system"`` or the canonical daemon
      sender :data:`DAEMON_SENDER`, or ``kind`` in the synthetic-only
      allow-list) — reacting to a daemon notification would post a
      reaction back to a non-agent sender the daemon never reads and
      pollutes the dispatch ledger.

    The dispatch-id threading is OPTIONAL — a sender that did not
    mint a ledger row still benefits from the receipt landing in
    their inbox (and the receive-side updater simply no-ops when
    the dispatch row is absent). So we do NOT require
    ``event['dispatch_id']`` to be present.
    """
    if not event.get("from_agent"):
        return False
    if event.get("ack"):
        return False
    kind = event.get("kind")
    if kind == "reaction":
        return False
    if kind in {"denied_attempt", "acl_deny_notify"}:
        return False
    if event.get("from_agent") in ("system", DAEMON_SENDER):
        return False
    return True


async def post_reaction_ack(
    event: dict[str, Any],
    *,
    agent_name: str,
    listen_url: str,
    bearer: str | None,
) -> None:
    """POST a structural ``kind=reaction`` envelope back to the sender.

    Reuses the exact ``/agents/<sender>/message:send`` HTTP path the
    auto-ack and ``a2a_*`` tool surface use, with three distinguishing
    metadata fields:

    * ``kind="reaction"`` — receiver-side recognises this so the
      sender's adapter can route to the ledger updater rather than
      surface it as a normal inbox message.
    * ``ack=True`` — preserves the auto-ack subsystem's loop-guard
      (so the existing receive-side ``_should_auto_ack`` will not
      auto-ack the reaction in turn).
    * ``extra.reacted_dispatch_id`` — the SENDER's dispatch-ledger id
      threaded through the original inbound event. Omitted when the
      sender did not mint a ledger row (legacy clients).

    Content is the configured marker (``👀`` by default), which is
    non-empty so the sender-side empty-ack noise filter does NOT
    suppress it — that's the whole point of this code path: a
    STRUCTURAL receipt that survives the wire.

    Best-effort: the caller logs any exception but never re-raises,
    so a flaky receipt cannot block delivery or kill the SSE
    consumer.
    """
    import uuid as _uuid

    import httpx

    target = event["from_agent"]
    marker = reaction_ack_marker()
    metadata: dict[str, Any] = {
        "from_agent": agent_name,
        "ack": True,
        "kind": "reaction",
    }
    msg_id = event.get("msg_id")
    if msg_id:
        metadata["in_reply_to"] = msg_id
    conversation_id = event.get("conversation_id")
    if conversation_id:
        metadata["conversation_id"] = conversation_id
    extra: dict[str, Any] = {}
    dispatch_id = event.get("dispatch_id")
    if dispatch_id:
        extra["reacted_dispatch_id"] = dispatch_id
    if extra:
        metadata["extra"] = extra
    payload = {
        "jsonrpc": "2.0",
        "id": _uuid.uuid4().hex,
        "method": "SendMessage",
        "params": {
            "message": {
                "message_id": _uuid.uuid4().hex,
                "role": "ROLE_USER",
                "parts": [{"text": marker}],
            },
            "metadata": metadata,
        },
    }
    base = listen_url.rstrip("/")
    headers = {"Content-Type": "application/json"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{base}/agents/{target}/message:send",
            json=payload,
            headers=headers,
        )
        resp.raise_for_status()
