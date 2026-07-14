"""The per-agent sidecar's inbox SSE stream (extracted from ``_server.py``).

Serves ``GET /agents/<name>/inbox/stream`` on an agent's own a2a port — the
A2A-facing twin of the host control plane's stream in
:mod:`.._listen._node_channel`. Split out of :mod:`._server` (which sat over
the per-file line cap) exactly as ``node_inbox_stream`` was split out of
``_listen/server.py``: one cohesive responsibility per file, and the original
keeps a thin delegate so route registration and every historical import path
are unchanged.

The two streams are the SAME primitive and must not drift. Both:

  * emit one comment frame on connect so a client can detect "subscribed"
    without waiting for traffic;
  * replay durable ``channel_events`` rows before accepting live events, so an
    event published while nobody was subscribed is delivered on connect;
  * BEAT when idle (see below);
  * unsubscribe in a ``finally`` so the broker's subscriber count is honest.

Why an idle stream must still write
-----------------------------------
A stream that goes silent when there is nothing to say cannot be told apart from
a stream that has DIED silently — no FIN, no RST, just a socket nobody will ever
speak on again (a hard host death, a wedged uvicorn, an idle NAT/firewall flow
drop). The CLIENT then parks on an unbounded read believing it is subscribed,
while the broker holds no subscriber for it: every message aimed at that agent
lands on an empty bus, with no error raised anywhere, until someone restarts the
agent.

The keepalive beat gives the client bytes, so a bounded read deadline can fire
and it can re-dial. See :func:`.._inbox_bus.keepalive_interval_s` — including
the precise (and narrower) note on what the beat does and does not buy on the
SERVER side.
"""

from __future__ import annotations

import json
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from scitex_agent_container._state.state_db_channel import (
    list_since_id,
    list_undelivered,
    mark_delivered,
)
from scitex_agent_container.a2a._inbox_bus import KEEPALIVE, keepalive_interval_s

__all__ = ["inbox_stream"]


async def inbox_stream(request: Request, ctx: Any) -> Response:
    """SSE: one frame per inbound event addressed to ``/agents/<name>``.

    Consumed by ``sac mcp channel`` inside the agent's container — each frame
    turns into a ``notifications/claude/channel`` push so Claude sees
    ``<channel source="..." msg_id="..." ...>`` tags in real time. Plain SSE,
    so non-sac A2A clients work too.

    Durability / replay-on-reconnect (handoff §4):

      * On connect, replay missed events from the persistent ``channel_events``
        table BEFORE accepting any new live event. Replay source:

          - if the client passed ``Last-Event-ID``, replay every row with
            ``id > Last-Event-ID``;
          - otherwise replay every undelivered row (the fresh-subscriber case —
            handoff acceptance "an event POSTed with no subscriber is delivered
            on connect").

      * Each replay frame stamps the SQLite row id onto the SSE ``id:`` line so
        the client can echo it back as ``Last-Event-ID`` after a reconnect.

      * After yielding a replay frame the row is marked ``delivered_at`` so a
        subsequent fresh-subscriber connect does not re-yield it.

      * A malformed ``Last-Event-ID`` is a loud 400 — a corrupt cursor would
        silently disable replay if tolerated.
    """
    name = request.path_params["name"]
    if name not in ctx.yamls:
        return JSONResponse({"error": f"unknown agent: {name}"}, status_code=404)

    last_event_id_raw = request.headers.get("last-event-id")
    last_event_id: int | None = None
    if last_event_id_raw is not None:
        # Loud failure on a malformed header: a corrupt cursor would silently
        # disable replay if we tolerated it.
        try:
            last_event_id = int(last_event_id_raw)
        except ValueError:
            return JSONResponse(
                {
                    "error": (
                        "Last-Event-ID header must be an integer; got "
                        f"{last_event_id_raw!r}"
                    )
                },
                status_code=400,
            )

    broker = ctx.inbox
    queue = await broker.subscribe(name)

    async def stream():
        try:
            # Comment-only frame so HTTP clients see the connection open
            # before any real event arrives.
            yield b": sac-channel ready\n\n"

            # Replay missed events from state.db. Mark each row delivered as
            # soon as we ship its SSE frame so a second fresh subscriber does
            # not re-receive it.
            if last_event_id is not None:
                replay = list_since_id(target=name, since_id=last_event_id)
            else:
                replay = list_undelivered(target=name)
            for entry in replay:
                if await request.is_disconnected():
                    return
                row_id = entry["id"]
                event = entry["event"]
                # Strip the internal ``_row_id`` if a publish path stored it
                # inside ``meta_json``; the SSE ``id:`` line is the
                # authoritative cursor.
                event.pop("_row_id", None)
                data = json.dumps(event, ensure_ascii=False)
                yield (f"id: {row_id}\nevent: message\ndata: {data}\n\n").encode(
                    "utf-8"
                )
                mark_delivered([row_id])

            beat_s = keepalive_interval_s()
            while True:
                if await request.is_disconnected():
                    return
                # Three outcomes, never two: an event, a beat, or "closing".
                # ``get_or_close`` races the queue against the broker's
                # shutdown Event so a graceful SIGTERM cancels this in-flight
                # stream promptly instead of parking here.
                event = await broker.get_or_close(queue, keepalive_after=beat_s)
                if event is None:
                    return
                if event is KEEPALIVE:
                    # Idle — beat. Invisible as CONTENT (a ':' line is an SSE
                    # comment), but it is what lets a client tell "quiet" from
                    # "silently dead" and re-dial rather than park forever.
                    yield b": keepalive\n\n"
                    continue
                # The publish path stamps the persisted row id onto the
                # envelope as ``_row_id``. Surface it as the SSE ``id:`` line
                # and mark the row delivered.
                row_id = event.pop("_row_id", None)
                data = json.dumps(event, ensure_ascii=False)
                if row_id is not None:
                    yield (f"id: {row_id}\nevent: message\ndata: {data}\n\n").encode(
                        "utf-8"
                    )
                    mark_delivered([int(row_id)])
                else:
                    # No row id means the event was injected by a path that did
                    # NOT persist (lifecycle fan-out, ACL-reject notice, …).
                    # Deliver it but skip the marker.
                    yield f"event: message\ndata: {data}\n\n".encode("utf-8")
        finally:
            await broker.unsubscribe(name, queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
