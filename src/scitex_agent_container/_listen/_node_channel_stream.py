"""The listen daemon's node inbox SSE stream (extracted from ``_node_channel``).

Serves ``GET /agents/<name>/inbox/stream`` on the ``sac listen`` control
plane — the host-side twin of the per-agent sidecar's stream in
:mod:`..a2a._inbox_stream`. Split out of :mod:`._node_channel` (which sat
over the per-file line cap once the ``asyncio.to_thread`` hops below were
added) exactly as ``inbox_stream`` was split out of ``a2a/_server.py``: one
cohesive responsibility per file, and the original keeps a re-export so route
registration in :mod:`.server` and every historical import path are
unchanged.

``_node_channel`` keeps the PUBLISH half (``node_message_send``: the ACL
gate, the deny notifications, the cross-host forward, persist-then-publish);
this file holds the SUBSCRIBE half. The two are genuinely separate
responsibilities that happened to share a file.

The two SSE streams — this one and ``a2a/_inbox_stream`` — are the SAME
primitive and must not drift. Both emit a comment frame on connect, replay
durable ``sac_channel_events`` rows before accepting live events, beat when
idle, and unsubscribe in a ``finally``. Both now do every database call
through ``asyncio.to_thread`` for the reason spelled out inline below.
"""

from __future__ import annotations

import asyncio
import json

from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from .._lifecycle._off_loop import run_blocking
from .._state.state_db_channel import (
    list_since_id,
    list_undelivered,
    mark_delivered,
)
from ..a2a._inbox_bus import KEEPALIVE, keepalive_interval_s
from ._nodes import Broker, NodeRegistry

__all__ = ["node_inbox_stream"]


async def node_inbox_stream(request: Request) -> Response:
    """``GET /agents/<name>/inbox/stream`` — SSE: one frame per event
    published to ``<name>`` on this sac listen.

    Consumed by ``sac mcp channel --name <name>`` inside an external
    node's Claude session (or a sac-managed agent's container). The
    frame shape is identical to ``a2a/_server.py``'s stream so the
    same client adapter works for both kinds of node.

    Implicitly registers ``<name>`` as an external node on first
    connect.

    WI-1 finish-work (Q5 — handoff §4 durability acceptance applied
    to the ``sac listen`` surface, mirroring ``a2a/_server.py``):

      * On connect, replay missed events from the persistent
        ``sac_channel_events`` table BEFORE accepting any new live
        event. Replay source:

          - if the client passed ``Last-Event-ID``, replay every row
            with ``id > Last-Event-ID``;
          - otherwise replay every undelivered row (fresh-subscriber
            case — handoff acceptance "an event POSTed with no
            subscriber is delivered on connect").

      * Each replay frame stamps the row id onto the SSE ``id:`` line
        so the client can echo it back as ``Last-Event-ID`` after a
        reconnect. The id is PER-TARGET since the 2026-08-28 move to
        PostgreSQL, which is why every ``mark_delivered`` below passes
        ``target=name``.

      * After yielding a replay frame the row is marked
        ``delivered_at`` so a subsequent fresh-subscriber connect
        does not re-yield it.

      * A malformed ``Last-Event-ID`` header is a loud 400 — a
        corrupt cursor would silently disable replay if tolerated
        (handoff §0).
    """
    name = request.path_params["name"]
    base_url = str(request.base_url).rstrip("/")
    nodes: NodeRegistry = request.app.state.nodes
    broker: Broker = request.app.state.inbox
    nodes.register(name, base_url)

    last_event_id_raw = request.headers.get("last-event-id")
    last_event_id: int | None = None
    if last_event_id_raw is not None:
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

    queue = await broker.subscribe(name)

    async def stream():
        try:
            # Comment-only frame so HTTP clients see the connection
            # open immediately (and tests can race-free detect "I'm
            # subscribed" before publishing).
            yield b": sac-channel ready\n\n"

            # WI-1 replay: yield every missed durable row first, then
            # accept live events from the broker.
            #
            # OFF THE EVENT LOOP, every database call in this generator.
            # They were safe as sync calls while ``channel_events`` was a
            # local SQLite file; since 2026-08-28 each is a NETWORK round
            # trip, so a blackholed primary would stall THIS WHOLE DAEMON —
            # every request it is serving, not just this stream.
            # BOUNDED — and ``asyncio.to_thread`` was NOT, which an earlier
            # version of this comment got wrong. It justified the hop with the
            # store's ``connect_timeout``; libpq's connect_timeout bounds
            # CONNECTION ESTABLISHMENT ONLY. A primary that accepts TCP and
            # then stops answering blocks forever, holding the module-wide
            # ``_OP_LOCK``, while every other channel call queues behind it in
            # the event loop's SHARED default executor — min(32, cpu+4)
            # threads, 6-8 on a small runner. ``_lifecycle._off_loop`` was
            # written for exactly that failure and measured it: unbounded
            # ``to_thread`` callers "queue behind the wedged threads and hang
            # FOREVER". ``run_blocking`` uses a dedicated thread plus a hard
            # ``asyncio.wait_for``, so a wedged call raises here — the stream
            # dies and the client re-dials — instead of taking the daemon
            # with it.
            if last_event_id is not None:
                replay = await run_blocking(
                    list_since_id, target=name, since_id=last_event_id
                )
            else:
                replay = await run_blocking(list_undelivered, target=name)
            for entry in replay:
                if await request.is_disconnected():
                    return
                row_id = entry["id"]
                event = entry["event"]
                # Strip the internal ``_row_id`` if a previous publish
                # path stored it inside ``meta_json``; the SSE ``id:``
                # line is the authoritative cursor.
                event.pop("_row_id", None)
                data = json.dumps(event, ensure_ascii=False)
                yield (f"id: {row_id}\nevent: message\ndata: {data}\n\n").encode(
                    "utf-8"
                )
                await run_blocking(mark_delivered, [row_id], target=name)

            beat_s = keepalive_interval_s()
            while True:
                if await request.is_disconnected():
                    return
                # ``get_or_close`` races ``queue.get()`` against the
                # broker's shutdown Event so a graceful ``sac listen``
                # SIGTERM cancels this in-flight stream promptly instead
                # of parking here until restart --force SIGKILLs at 10 s
                # (card sac-listen-sigterm-sse-shutdown-hang). ``None``
                # means "broker closing" — return so the StreamingResponse
                # completes and the daemon exits cleanly.
                event = await broker.get_or_close(queue, keepalive_after=beat_s)
                if event is None:
                    return
                if event is KEEPALIVE:
                    # Idle stream — beat. A comment frame is a no-op as CONTENT
                    # to any SSE client (the adapter skips lines starting with
                    # ':'), but it is not a no-op as SIGNAL: it gives the client
                    # bytes, which is the ONLY way a bounded read deadline can
                    # tell "quiet" from "silently dead" and re-dial instead of
                    # parking forever on a socket nobody will ever speak on
                    # again. Without it, a listen that vanishes without closing
                    # deafens this agent until someone restarts it.
                    yield b": keepalive\n\n"
                    continue
                # The publish path stamps the persisted row id onto
                # the envelope as ``_row_id`` (see
                # :func:`._node_channel.node_message_send`). We surface it
                # as the SSE ``id:`` line and mark the row delivered.
                # READ, DO NOT POP. ``Broker.publish`` fans the SAME dict
                # out to every subscriber queue, so popping here mutates the
                # object the OTHER subscribers are about to read: the second
                # one finds no ``_row_id``, emits a frame with no ``id:``
                # line, and never marks the row delivered. The key is
                # excluded at serialize time instead, which leaves the
                # envelope every subscriber sees identical.
                row_id = event.get("_row_id")
                data = json.dumps(
                    {k: v for k, v in event.items() if k != "_row_id"},
                    ensure_ascii=False,
                )
                if row_id is not None:
                    yield (f"id: {row_id}\nevent: message\ndata: {data}\n\n").encode(
                        "utf-8"
                    )
                    await run_blocking(
                        mark_delivered, [int(row_id)], target=name
                    )
                else:
                    # No row id means the event was injected by a
                    # path that did NOT persist (future lifecycle
                    # fan-out, ACL-reject notice, …). Deliver it but
                    # skip the marker.
                    yield f"event: message\ndata: {data}\n\n".encode("utf-8")
        finally:
            await broker.unsubscribe(name, queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
