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


__all__ = ["Broker", "mint_event", "mint_deny_notification"]
