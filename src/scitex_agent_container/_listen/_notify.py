"""``POST /v1/notify`` — interim card-event delivery to a containerized agent.

Why this exists (scitex-todo escalation, P1)
=============================================
scitex-todo's board resolves an agent's ``turn_url`` and does a DIRECT
HTTP POST to it (``http://<host>:<port>/v1/turn``). For a CONTAINERIZED
agent that POST gets ``Connection refused [Errno 111]`` — the agent's
a2a port is NOT reachable inbound from the board's host context.

By contrast, sac's a2a delivery reaches containers because a
containerized agent SUBSCRIBES OUTBOUND to the central ``sac listen``
daemon's a2a bus (the SSE stream at ``/agents/<name>/inbox/stream``) and
the daemon PUBLISHES down that connection — nothing is ever POSTed into
the container. So fleet notify-delivery to containers MUST go through
sac's a2a router (the listen daemon's publish-to-bus), never a direct
POST.

This endpoint is the INTERIM unblock: the board POSTs here (loopback
``127.0.0.1:7878`` from its host context, same bearer as the rest of the
``sac listen`` control-plane) INSTEAD of the agent's ``turn_url``. The
body is published into the named agent's inbox bus via the EXACT same
:class:`~scitex_agent_container.a2a._inbox_bus.Broker` publish path that
``/agents/<name>/message:send`` (a2a_send) uses, so it reaches a
subscribed (containerized) agent.

The full event-driven rail (C10) is :mod:`._card_event_delivery`, which
registers a ``scitex_todo.hooks`` consumer and calls the same
:func:`publish_to_agent` helper. This module owns the HTTP seam; that
one owns the bus-consumer seam. Both deliver through ``publish_to_agent``
so there is ONE router-publish code path.

Contract (so the board can call it)
====================================
``POST /v1/notify``  (bearer-gated by ``BearerAuthMiddleware``)

Request JSON::

    {"agent": "<owner-agent-name>",   # required, non-empty
     "body": "<notification text>",   # required, non-empty
     "card_id": "<card id>",          # optional — rides on the envelope
     "from_agent": "<sender>"}        # optional — defaults to "scitex-cards"

Response ``200``::

    {"agent": "<name>", "msg_id": "<hex>",
     "delivered_subscriber_count": <int>}

A ``delivered_subscriber_count`` of ``0`` is NOT an error — the event is
persisted to ``channel_events`` first, so an agent whose container is
momentarily disconnected receives it on its next ``inbox/stream``
connect (durability/replay, mirroring ``node_message_send``). The count
is surfaced so the board has delivery visibility.

Fail-loud (handoff §0): malformed JSON / missing-or-empty ``agent`` /
missing-or-empty ``body`` → ``400`` with a reason; a persist/publish
failure → ``500`` with the reason. No silent drops.
"""

from __future__ import annotations

import json
import logging
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from .._lifecycle._off_loop import run_blocking
from .._state.state_db_channel import persist_event
from ..a2a._inbox_bus import Broker, mint_event

logger = logging.getLogger(__name__)

# Default sender identity stamped on the envelope when the caller does
# not supply ``from_agent``. The board (scitex-cards) is the canonical
# producer for this endpoint. Free-form display provenance, not a lookup
# key — so the rename is a straight flip, no dual-name tolerance needed.
DEFAULT_NOTIFY_FROM = "scitex-cards"

__all__ = ["notify", "publish_to_agent"]


async def publish_to_agent(
    broker: Broker,
    *,
    agent: str,
    body: str,
    from_agent: str | None = None,
    card_id: str | None = None,
    kind: str = "message",
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist + publish ``body`` into ``agent``'s inbox bus.

    THE single router-publish helper shared by the HTTP endpoint
    (:func:`notify`) and the C10 entry-point consumer
    (:mod:`._card_event_delivery`). It mirrors the durability contract
    of :func:`scitex_agent_container._listen._node_channel.node_message_send`:
    the event is persisted to ``channel_events`` BEFORE publish so an
    event addressed to an agent with no live subscriber is not silently
    dropped — it is replayed on the next ``inbox/stream`` connect. The
    persisted row id rides on the envelope as ``_row_id`` so the SSE
    handler stamps it onto the ``id:`` line (the ``Last-Event-ID``
    cursor).

    ``card_id`` (when given) is threaded into the envelope's ``extra``
    block so a card-aware consumer can correlate the push back to the
    originating card without parsing the free-form ``body``.

    Returns ``{"msg_id": str, "delivered_subscriber_count": int}``.
    Raises on a persist/publish failure (the caller maps it to a loud
    non-2xx / logged warning).
    """
    merged_extra: dict[str, Any] = dict(extra) if extra else {}
    if card_id:
        merged_extra.setdefault("card_id", card_id)

    event = mint_event(
        agent,
        content=body,
        from_agent=from_agent or DEFAULT_NOTIFY_FROM,
        kind=kind,
        extra=merged_extra or None,
    )
    # Durability first (handoff §0): persist BEFORE publish so a
    # not-yet-connected container still receives the event on reconnect.
    #
    # OFF THE EVENT LOOP: ``persist_event`` is a PostgreSQL round trip
    # since 2026-08-28, and this coroutine runs inside the ``sac listen``
    # daemon, where a blocking network call blocks EVERY request the
    # daemon is serving. Same fix as the SSE generators and the
    # ``is_local_node`` hop in ``_node_channel``.
    row_id = await run_blocking(persist_event, target=agent, event=event)
    event["_row_id"] = row_id
    delivered = await broker.publish(agent, event)
    return {"msg_id": event["msg_id"], "delivered_subscriber_count": delivered}


async def notify(request: Request) -> Response:
    """``POST /v1/notify`` — see module docstring for the full contract.

    Bearer auth is enforced by :class:`BearerAuthMiddleware` (the route
    is not in its ``PUBLIC_PATHS``). This handler validates the body
    shape, then delegates to :func:`publish_to_agent` so the bus-publish
    path is identical to the C10 rail and to ``a2a_send``.
    """
    try:
        body_json = await request.json()
    except (ValueError, json.JSONDecodeError) as exc:
        return JSONResponse(
            {"error": f"body must be valid JSON: {exc}"}, status_code=400
        )
    if not isinstance(body_json, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)

    agent = body_json.get("agent")
    if not isinstance(agent, str) or not agent.strip():
        return JSONResponse(
            {"error": "field 'agent' is required and must be a non-empty string"},
            status_code=400,
        )
    agent = agent.strip()

    text = body_json.get("body")
    if not isinstance(text, str) or not text.strip():
        return JSONResponse(
            {"error": "field 'body' is required and must be a non-empty string"},
            status_code=400,
        )

    card_id = body_json.get("card_id")
    if card_id is not None and not isinstance(card_id, str):
        return JSONResponse(
            {"error": "field 'card_id' must be a string when set"},
            status_code=400,
        )
    from_agent = body_json.get("from_agent")
    if from_agent is not None and not isinstance(from_agent, str):
        return JSONResponse(
            {"error": "field 'from_agent' must be a string when set"},
            status_code=400,
        )

    broker: Broker = request.app.state.inbox
    try:
        result = await publish_to_agent(
            broker,
            agent=agent,
            body=text,
            from_agent=from_agent,
            card_id=card_id,
        )
    except Exception as exc:  # stx-allow: fallback (reason: a persist/publish failure must be a LOUD 500 with the reason, never a silent drop)
        logger.warning("notify: publish to %r failed: %s", agent, exc)
        return JSONResponse(
            {"error": f"failed to deliver to {agent!r}: {exc}"}, status_code=500
        )

    return JSONResponse(
        {
            "agent": agent,
            "msg_id": result["msg_id"],
            "delivered_subscriber_count": result["delivered_subscriber_count"],
        }
    )
