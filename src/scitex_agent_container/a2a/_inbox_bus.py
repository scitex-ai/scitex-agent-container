"""Per-agent inbox pub/sub for the A2A channel primitive.

A turn POSTed at ``/agents/<name>`` lands in the SDK dispatcher
(which handles the JSON-RPC reply path), but it also fans out to every
SSE subscriber currently connected to
``/agents/<name>/inbox/stream``.

That stream is what ``sac mcp channel`` consumes inside the agent's
container, turning each fan-out event into a
``notifications/claude/channel`` push so the running Claude session
sees ``<channel source="..." ts="..." msg_id="...">`` tags in real
time. Non-sac A2A clients can poll the same stream — it's plain SSE
over HTTP, no sac dependency.

The bus is intentionally in-process and in-memory:

* One ``Broker`` per sac-listen process (host or per-agent sidecar).
* One bounded ``asyncio.Queue`` per (agent, subscriber). Bounded so a
  slow consumer can't grow the queue without limit; oldest pending
  message drops when the cap is hit.
* No persistence — peers that POST while no subscriber is connected
  do not have their messages buffered. The receiving agent's Claude
  session is the authoritative consumer; replays land on disk via
  ``session.jsonl``, not via the bus.

The broker has no auth — the routes that publish / subscribe enforce
the same bearer-auth shape as the rest of sac listen.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import defaultdict
from typing import Any

# Per-subscriber queue cap. 64 is plenty for interactive flows; a flood
# beyond that is almost certainly a misbehaving sender — we'd rather
# drop oldest than block the publisher's HTTP handler.
_QUEUE_CAP = 64

# Canonical ``from_agent`` (sender) value for messages sac's OWN
# daemon originates — as opposed to an agent's a2a reasoning, which
# keeps its own ``from_agent``.
#
# Operator directive (2026-07-05, bracket form): the channel tag an
# agent sees renders as ``<- <source> [<sender>]`` where ``source``
# stays the CLEAN, unsuffixed channel name (sac's channel identity,
# e.g. ``sac``) and the SENDER (bracket) says whether the frame came
# from the daemon or a peer agent. ``_build_notification`` projects
# the event's ``from_agent`` into ``meta.source`` (the bracket), so a
# sac daemon frame with ``from_agent=DAEMON_SENDER`` renders as
# ``<- sac [daemon]`` and an agent frame renders ``<- sac [<agent>]``.
#
# This tag is applied ONLY to messages sac itself originates as the
# daemon with no sender-supplied source. A message that already
# carries a sender's ``from_agent`` (agent a2a) or another channel's
# tag passes through UNCHANGED — sac never re-tags those, and the
# source is never suffixed.
DAEMON_SENDER = "daemon"


class Broker:
    """In-memory pub/sub keyed by agent name."""

    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue[dict[str, Any]]]] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def subscribe(self, agent: str) -> asyncio.Queue[dict[str, Any]]:
        q: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=_QUEUE_CAP)
        async with self._lock:
            self._subs[agent].add(q)
        return q

    async def unsubscribe(self, agent: str, q: asyncio.Queue[dict[str, Any]]) -> None:
        async with self._lock:
            self._subs.get(agent, set()).discard(q)
            if not self._subs.get(agent):
                self._subs.pop(agent, None)

    async def publish(self, agent: str, event: dict[str, Any]) -> int:
        """Fan ``event`` out to every subscriber for ``agent``.

        Returns the count of subscribers that accepted the event. When
        a queue is full we drop the oldest entry to make room — slow
        consumer cannot back up the publisher.
        """
        async with self._lock:
            queues = list(self._subs.get(agent, ()))
        delivered = 0
        for q in queues:
            try:
                q.put_nowait(event)
                delivered += 1
            except asyncio.QueueFull:
                # Drop oldest; retry once. If still full the consumer
                # is wedged — skip this delivery (next event has a
                # fresh shot at fitting after the drop).
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:  # pragma: no cover
                    pass
                try:
                    q.put_nowait(event)
                    delivered += 1
                except asyncio.QueueFull:  # pragma: no cover
                    pass
        return delivered

    async def subscriber_count(self, agent: str) -> int:
        async with self._lock:
            return len(self._subs.get(agent, ()))


def mint_event(
    agent: str,
    content: str,
    *,
    from_agent: str | None = None,
    conversation_id: str | None = None,
    in_reply_to: str | None = None,
    priority: str = "normal",
    requires_reply: bool = False,
    ack: bool = False,
    extra: dict[str, Any] | None = None,
    kind: str | None = None,
    dispatch_id: str | None = None,
) -> dict[str, Any]:
    """Mint the event shape sac's channel publishes.

    Sender side fills ``content`` + optional metadata; sac listen
    enriches with ``msg_id`` and ``ts`` so receivers can ack/reply
    against stable identifiers.

    ``ack`` marks the event as itself a read-receipt (the same flag
    the ``a2a_ack`` tool stamps under ``metadata.ack``). It must
    survive minting so the receiving adapter's auto-ack loop-guard
    can recognise an ack and decline to ack it back — otherwise two
    auto-ack adapters ping-pong forever.

    ``dispatch_id`` is the SENDER-minted dispatch-ledger id (see
    :mod:`scitex_agent_container._state.dispatch_ledger`). It rides on
    the event so the channel wake path can thread it onto the woken
    turn's ``/v1/turn`` body, letting the receiver's Stop hook correlate
    its completion push back to the originating dispatch row. Omitted
    when the sender did not mint a ledger id.
    """
    event: dict[str, Any] = {
        "msg_id": uuid.uuid4().hex,
        "to_agent": agent,
        "from_agent": from_agent or "unknown",
        "ts": time.time(),
        "content": content,
        "priority": priority,
        "requires_reply": requires_reply,
        "ack": ack,
    }
    if conversation_id:
        event["conversation_id"] = conversation_id
    if in_reply_to:
        event["in_reply_to"] = in_reply_to
    if dispatch_id:
        event["dispatch_id"] = dispatch_id
    if extra:
        event["extra"] = extra
    if kind:
        event["kind"] = kind
    return event


def mint_deny_notification(
    *,
    target: str,
    from_agent: str | None,
    reason: str,
) -> dict[str, Any]:
    """Mint a denied-attempt notification event for the RECEIVER.

    Comms item D (fail-loud on ACL-denied sends): when ``check_send_acl``
    refuses an inbound ``message:send``, the sender already gets a 403
    with the reason — but the receiver was previously told nothing,
    leaving them unable to decide whether to grant the sender. This
    helper produces the notification the receiver sees instead.

    The shape mirrors a normal :func:`mint_event` envelope so SSE
    consumers and ``channel_events`` persistence work unchanged, with
    two deliberate differences:

    * ``content`` is the empty string — **the message body never
      leaks** to an unauthorized receiver. Only attempt metadata
      (from / to / reason / timestamp) is published.
    * ``kind`` is ``"denied_attempt"`` so receivers can distinguish
      it from a real message at a glance, and the deny reason rides
      under ``extra.deny_reason``.

    ``from_agent`` should be the *effective* sender identity from the
    ACL decision (authenticated bearer name, or the claimed name on
    the admin-caller path). ``None`` is rendered as ``"unknown"`` by
    ``mint_event`` — that's the only honest answer when neither was
    presented (the "no identity at all" deny).
    """
    return mint_event(
        target,
        content="",
        from_agent=from_agent,
        priority="normal",
        kind="denied_attempt",
        extra={"deny_reason": reason},
    )


def mint_acl_deny_synthetic_notification(
    *,
    target: str,
    sender: str,
    reason: str,
) -> dict[str, Any]:
    """Mint the rate-limited synthetic system notification for ACL-deny.

    sac-comms item D (lead a2a ``c42b3e3c`` — merged with
    ``lead-sac-acl-blocked-attempt-notification``). When an outbound
    ``a2a_send(sender, target)`` is ACL-denied, the receiver gets ONE
    synthetic system-level notification per cool-down window (default
    30 min, see :mod:`_state.state_db_acl_deny_notify`).

    Differences from :func:`mint_deny_notification`:

    * ``from_agent`` is the canonical daemon sender
      :data:`DAEMON_SENDER` (``"daemon"``, not the would-be sender) —
      the receiver MUST know the notification did not originate from a
      granted peer (the message body never leaks pre-decision, and
      surfacing the sender as the apparent author is the same leak in a
      different shape). Rendered in the channel bracket as
      ``<- sac [daemon]`` so the receiver distinguishes this SAC daemon
      frame from an agent-authored message.
    * ``content`` carries a human-readable, operator-actionable
      string embedding the exact ``sac a2a grant`` command keyed to
      the actual ``<sender>`` / ``<target>`` names. This is the
      "synthetic notification that bypasses ACL" the operator can
      act on without scrolling structured fields.
    * ``kind`` is ``"acl_deny_notify"`` so receivers can distinguish
      a SYNTHETIC system frame from a real (granted-and-delivered)
      message or the per-attempt ``"denied_attempt"`` envelope.

    The ``extra`` block carries the structured fields a richer
    client (Telegram bridge / dedicated UI) can branch on without
    re-parsing the content string.

    REPLACES the prior parent/child auto-grant policy: the operator
    sees the attempt and grants if intended, rather than the system
    auto-granting on a lineage heuristic.
    """
    content = (
        f"[system] Sender {sender!r} attempted a send to {target!r} "
        "and was blocked by ACL.\n"
        f"Grant via `sac a2a grant {sender} {target}` if intended."
    )
    return mint_event(
        target,
        content=content,
        from_agent=DAEMON_SENDER,
        priority="normal",
        kind="acl_deny_notify",
        extra={
            "acl_deny_notify": True,
            "blocked_sender": sender,
            "blocked_target": target,
            "deny_reason": reason,
            "grant_command": f"sac a2a grant {sender} {target}",
        },
    )


__all__ = [
    "DAEMON_SENDER",
    "Broker",
    "mint_event",
    "mint_deny_notification",
    "mint_acl_deny_synthetic_notification",
]
