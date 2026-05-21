"""Node inbox-channel routes for ``sac listen`` (extracted from server.py).

Hosts the A2A node-comms surface — the publish + subscribe halves of the
per-node inbox channel plus the cross-host forwarder they depend on:

    POST /agents/<name>/message:send   → :func:`node_message_send`
    GET  /agents/<name>/inbox/stream    → :func:`node_inbox_stream`

Split out of :mod:`scitex_agent_container._listen.server` (which grew
past the per-file line cap). ``server.py`` re-imports the three handlers
so route registration (:func:`_v1_agent_routes`) and the historical
``from ..._listen.server import node_message_send`` test import path keep
working unchanged. No behaviour change — this is a pure extraction of one
cohesive responsibility (node comms) away from the agent-lifecycle /
card responsibility that stays in ``server.py``.
"""

from __future__ import annotations

import json
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from .._state.state_db_channel import (
    list_since_id,
    list_undelivered,
    mark_delivered,
    persist_event,
)
from ..a2a._inbox_bus import mint_deny_notification, mint_event
from ._acl import check_send_acl, deny_response
from ._nodes import Broker, NodeRegistry

__all__ = ["_forward_to_remote", "node_message_send", "node_inbox_stream"]


async def _forward_to_remote(
    request: Request,
    *,
    body: dict[str, Any],
    target_host: str,
    target_port: int | None,
    target_name: str,
) -> Response:
    """WI-4 cross-host forwarder. Reposts ``body`` to the destination
    host's ``sac listen`` and proxies the response back.

    **Bearer handling — per-host bearer registry** (Q4 (b)). The
    destination's host bearer is read from
    ``peer-tokens/<target_host>.token`` on the forwarding host. The
    operator populates that registry with ``sac host add-peer <host>
    <token>`` (one entry per peer). The forwarder uses that bearer
    on the wire — not its own, not the original caller's. This
    keeps the **per-host blast radius** the lead asked for: leaking
    one host's listen bearer compromises only that host.

    Missing ``peer-tokens/<host>.token`` is a **loud failure**: 502
    with a clear "no peer token for X" message that names the file
    and the ``sac host add-peer`` fix. Never silently drop a forward
    (handoff §0 Hard rules).

    **ACL handling**: the body is unchanged, so the destination
    re-runs ``check_send_acl`` against the same
    ``metadata.from_agent``. Because the forwarder authenticates
    with the destination's *host* bearer (administrative caller),
    ``authenticated_node`` is ``None`` at the destination and the
    ACL gates on the metadata claim — exactly the cross-host shape
    the lead documented under Q1's restored design. Cross-group
    denials fire at the receiving host (handoff §4 acceptance "ACL
    is enforced at the receiving host").
    """
    if not target_port:
        return JSONResponse(
            {
                "error": (
                    f"cannot forward to {target_name!r} on host "
                    f"{target_host!r}: missing a2a_port in instances row"
                )
            },
            status_code=502,
        )

    # ``state_db.resolve_node_host`` returns the *canonical* host
    # name; we trust that to be reachable (handoff §2 "sac assumes
    # reachability; orochi establishes it"). For loopback test
    # scenarios callers set ``a2a_port`` to a 127.0.0.1 port and
    # the test fixtures match the canonical host name to "host-a"
    # / "host-b" via SAC_HOST.
    import httpx as _httpx

    from .peer_tokens import PeerTokenError, read_peer_token

    forward_url = (
        f"http://{target_host}:{target_port}/agents/{target_name}/message:send"
    )
    # In our test loopback both hosts live on 127.0.0.1; the canonical
    # host name is a label, not a routable address. Rewrite to
    # 127.0.0.1 when the resolved host is a known-loopback alias so
    # the test fixtures can drive both legs on one machine. Real
    # deployments use ssh-alias / tunnel hostnames and route as-is.
    if target_host in ("host-a", "host-b") or target_host.startswith("host-"):
        forward_url = (
            f"http://127.0.0.1:{target_port}/agents/{target_name}/message:send"
        )

    # WI-4 Q4(b) — per-host bearer registry. Pull the destination's
    # host bearer; loud 502 if it's missing.
    try:
        peer_bearer = read_peer_token(peer_host=target_host)
    except PeerTokenError as exc:
        return JSONResponse(
            {"error": f"cross-host forward refused: {exc}"},
            status_code=502,
        )

    forward_headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {peer_bearer}",
    }

    try:
        async with _httpx.AsyncClient(timeout=15.0) as ac:
            resp = await ac.post(forward_url, json=body, headers=forward_headers)
    except _httpx.HTTPError as exc:
        # Loud failure (handoff §0): the operator needs to see when
        # cross-host reachability breaks, not get a silent 200.
        return JSONResponse(
            {"error": (f"cross-host forward to {forward_url!r} failed: {exc}")},
            status_code=502,
        )

    # Pass through the destination's response, including its 403 / 400
    # / 200 status. Body is JSON or text — try JSON first.
    try:
        return JSONResponse(resp.json(), status_code=resp.status_code)
    except Exception:  # noqa: BLE001  # stx-allow: fallback (reason: non-JSON destination body is tolerated; surfaced as text)
        return JSONResponse(
            {"forwarded_body_text": resp.text}, status_code=resp.status_code
        )


async def node_message_send(request: Request) -> Response:
    """``POST /agents/<name>/message:send`` — publish an A2A
    ``SendMessage`` body to the local node's inbox bus.

    Implicitly registers ``<name>`` as an external node on first use
    so the synthesised AgentCard is available for the well-known
    lookup. The publish is **always loud** — a malformed body returns
    400, never a silent drop (handoff §0 Hard rules).

    WI-2 ACL gate: every send is checked by
    :func:`_acl.check_send_acl` before publish:

    * **Per-node bearer** pins identity — ``metadata.from_agent``
      must match the resolved name, else 403 "identity spoof"
      (handoff §4 acceptance).
    * **Cross-group** is denied by default; intra-group
      (parent↔child and sibling↔sibling) is allowed.
    * **Explicit cross-group grants** (``comms_grants`` table) flip
      a deny to an allow.
    * **Self-send** is always allowed.
    * The **host-wide bearer** is the administrative / cross-host
      forwarding caller; it honours ``metadata.from_agent``
      verbatim (used by WI-4 forwarders authenticating with the
      destination's host bearer from ``peer-tokens/`` registry).

    Bearer auth is enforced by :class:`BearerAuthMiddleware` (outer
    perimeter) and identity resolution by :class:`NodeAuthMiddleware`
    (sets ``request.state.authenticated_node``).
    """
    name = request.path_params["name"]
    try:
        body = await request.json()
    except (ValueError, json.JSONDecodeError) as exc:
        return JSONResponse(
            {"error": f"body must be valid JSON: {exc}"}, status_code=400
        )
    if not isinstance(body, dict):
        return JSONResponse({"error": "body must be a JSON object"}, status_code=400)

    method = body.get("method")
    if method not in ("message/send", "SendMessage", "SendStreamingMessage"):
        return JSONResponse(
            {
                "error": (
                    f"unsupported method {method!r}; expected one of "
                    "'message/send', 'SendMessage', 'SendStreamingMessage'"
                )
            },
            status_code=400,
        )

    # WI-4 cross-host forward. If the target lives on a different
    # host, forward the body unchanged to that host's sac listen.
    # The destination re-runs the ACL check against the same
    # ``metadata.from_agent`` we received, so cross-group denials
    # fire at the receiving host (handoff §4 acceptance).
    from .._state.state_db import _resolve_host as _resolve_local_host
    from .._state.state_db_nodes import is_local_node, resolve_node_host

    # Prefer the per-app ``local_host`` configured at ``create_app``
    # time; fall back to the env-based resolver for callers that
    # haven't pinned one. Per-app config matters for in-process
    # multi-host tests where the env is shared.
    local_host = getattr(request.app.state, "local_host", None) or _resolve_local_host(
        None
    )
    if not is_local_node(name=name, local_host=local_host):
        target_info = resolve_node_host(name=name)
        if target_info is None:
            return JSONResponse(
                {
                    "error": (
                        f"target {name!r} resolves to a non-local host but no "
                        "instance row carries its address — cannot forward"
                    )
                },
                status_code=502,
            )
        return await _forward_to_remote(
            request,
            body=body,
            target_host=target_info["host"],
            target_port=target_info["a2a_port"],
            target_name=name,
        )

    params = body.get("params") or {}
    if not isinstance(params, dict):
        return JSONResponse({"error": "params must be a JSON object"}, status_code=400)
    message = params.get("message") or {}
    parts = message.get("parts") if isinstance(message, dict) else None
    text = ""
    if isinstance(parts, list):
        for p in parts:
            if isinstance(p, dict) and isinstance(p.get("text"), str):
                text += p["text"]

    # sac-extension metadata: same convention as a2a/_server.py — under
    # ``params.metadata`` first, then ``message.metadata`` as a
    # secondary, since some clients prefer message-scoped metadata.
    sac_meta: dict[str, Any] = {}
    for src in (params.get("metadata"), message.get("metadata")):
        if isinstance(src, dict):
            sac_meta.update(src)

    # WI-2 ACL check. ``authenticated_node`` is set by
    # :class:`NodeAuthMiddleware` — ``None`` means the host-wide
    # bearer was presented (administrative caller). With a per-node
    # bearer, ``metadata.from_agent`` MUST match the resolved name
    # so identity cannot be spoofed via a metadata field (handoff
    # §4 acceptance). See :func:`_acl.check_send_acl`.
    authenticated_node = getattr(request.state, "authenticated_node", None)
    decision, reason = check_send_acl(
        authenticated_node=authenticated_node,
        claimed_from_agent=sac_meta.get("from_agent"),
        target=name,
    )
    if decision == "deny":
        # Comms item D — fail-loud on the RECEIVER side too. The sender
        # gets a 403 with the reason; without this notification the
        # receiver would never learn that someone tried to reach them
        # and was denied, so they couldn't decide whether to grant. We
        # publish a ``kind="denied_attempt"`` envelope onto the target's
        # inbox channel carrying only attempt METADATA — never the
        # message body, which must not leak to an unauthorized
        # receiver. Persist + publish mirrors the success path
        # (handoff §0 durability — a notification with no live
        # subscriber must not be silently dropped) but the
        # ``channel_events`` row's ``content`` column stays empty.
        # The ``missing target`` deny has no inbox to notify, so we
        # skip publishing in that case (still 403 to the sender).
        if name:
            broker: Broker = request.app.state.inbox
            notif = mint_deny_notification(
                target=name,
                from_agent=(authenticated_node or sac_meta.get("from_agent")),
                reason=reason or "ACL deny",
            )
            row_id = persist_event(target=name, event=notif)
            notif["_row_id"] = row_id
            await broker.publish(name, notif)
        return deny_response(reason or "ACL deny")

    event = mint_event(
        name,
        content=text,
        from_agent=sac_meta.get("from_agent"),
        conversation_id=sac_meta.get("conversation_id"),
        in_reply_to=sac_meta.get("in_reply_to"),
        priority=str(sac_meta.get("priority", "normal")),
        requires_reply=bool(sac_meta.get("requires_reply", False)),
        ack=bool(sac_meta.get("ack", False)),
        dispatch_id=sac_meta.get("dispatch_id"),
    )

    # Implicit registration — handoff §4 "A2A compliance without a
    # YAML": synthesise the card the first time the name is touched.
    base_url = str(request.base_url).rstrip("/")
    nodes: NodeRegistry = request.app.state.nodes
    broker: Broker = request.app.state.inbox
    nodes.register(name, base_url)

    # WI-1 finish-work (Q5 — handoff §4 durability acceptance applied
    # to the ``sac listen`` surface, not just ``a2a/_server.py``).
    # Persist BEFORE publish so an event POSTed with no subscriber is
    # not silently dropped (handoff §0 hard rule). The persisted row
    # id is attached to the envelope as ``_row_id``; the SSE stream
    # stamps it onto the ``id:`` line so clients can resume with
    # ``Last-Event-ID``. A denied send (returned earlier with 403)
    # never reaches this point — denial leaves no ``content`` on
    # ``channel_events`` and emits no ``kind="message"`` event on the
    # broker. (Comms item D adds a separate ``kind="denied_attempt"``
    # envelope on the deny branch above so the receiver still learns
    # of the attempt; only attempt metadata is published — never the
    # message body.)
    row_id = persist_event(target=name, event=event)
    event["_row_id"] = row_id

    delivered = await broker.publish(name, event)
    return JSONResponse(
        {
            "msg_id": event["msg_id"],
            "to_agent": name,
            "delivered_subscriber_count": delivered,
        }
    )


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
        ``channel_events`` table BEFORE accepting any new live event.
        Replay source:

          - if the client passed ``Last-Event-ID``, replay every row
            with ``id > Last-Event-ID``;
          - otherwise replay every undelivered row (fresh-subscriber
            case — handoff acceptance "an event POSTed with no
            subscriber is delivered on connect").

      * Each replay frame stamps the SQLite row id onto the SSE
        ``id:`` line so the client can echo it back as
        ``Last-Event-ID`` after a reconnect.

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
            if last_event_id is not None:
                replay = list_since_id(target=name, since_id=last_event_id)
            else:
                replay = list_undelivered(target=name)
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
                mark_delivered([row_id])

            while True:
                if await request.is_disconnected():
                    return
                event = await queue.get()
                # The publish path stamps the persisted row id onto
                # the envelope as ``_row_id`` (see
                # :func:`node_message_send`). We surface it as the SSE
                # ``id:`` line and mark the row delivered.
                row_id = event.pop("_row_id", None)
                data = json.dumps(event, ensure_ascii=False)
                if row_id is not None:
                    yield (f"id: {row_id}\nevent: message\ndata: {data}\n\n").encode(
                        "utf-8"
                    )
                    mark_delivered([int(row_id)])
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
