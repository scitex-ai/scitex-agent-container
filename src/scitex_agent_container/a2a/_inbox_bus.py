"""Per-agent inbox pub/sub for the A2A channel primitive.

A turn POSTed at ``/v1/sac/agents/<name>`` lands in the SDK dispatcher
(which handles the JSON-RPC reply path), but it also fans out to every
SSE subscriber currently connected to
``/v1/sac/agents/<name>/inbox/stream``.

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
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Mint the event shape sac's channel publishes.

    Sender side fills ``content`` + optional metadata; sac listen
    enriches with ``msg_id`` and ``ts`` so receivers can ack/reply
    against stable identifiers.
    """
    event: dict[str, Any] = {
        "msg_id": uuid.uuid4().hex,
        "to_agent": agent,
        "from_agent": from_agent or "unknown",
        "ts": time.time(),
        "content": content,
        "priority": priority,
        "requires_reply": requires_reply,
    }
    if conversation_id:
        event["conversation_id"] = conversation_id
    if in_reply_to:
        event["in_reply_to"] = in_reply_to
    if extra:
        event["extra"] = extra
    return event


__all__ = ["Broker", "mint_event"]
