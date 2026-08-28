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

import asyncio

import json
import logging
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from .._state.state_db_channel import (
    list_since_id,
    list_undelivered,
    mark_delivered,
    persist_event,
)
from ..a2a._delivery_report import report_zero_delivery
from ..a2a._inbox_bus import (
    KEEPALIVE,
    keepalive_interval_s,
    mint_acl_deny_synthetic_notification,
    mint_deny_notification,
    mint_event,
)
from ._acl import check_send_acl, deny_response
from ._acl_approve_prompt import (
    _looks_like_cross_group_deny,
    _mint_approval_prompt,
)
from ._node_channel_forwarders import _forward_to_remote
from ._nodes import Broker, NodeRegistry

__all__ = ["_forward_to_remote", "node_message_send", "node_inbox_stream"]

log = logging.getLogger(__name__)


async def node_message_send(request: Request) -> Response:
    """``POST /agents/<name>/message:send`` — publish an A2A
    ``SendMessage`` body to the local node's inbox bus.

    Implicitly registers ``<name>`` as an external node on first use
    so the synthesised AgentCard is available for the well-known
    lookup. The publish is **always loud** — a malformed body returns
    400, never a silent drop (handoff §0 Hard rules).

    WI-2 ACL gate: every send is checked by
    :func:`_acl.check_send_acl` before publish:

    * **Identity** is the ``metadata.from_agent`` claim. A per-node
      bearer used to pin it (mismatch → 403 "identity spoof"); that
      feature was removed 2026-08-28 having never been armed.
    * **Cross-group** is denied by default; intra-group
      (parent↔child and sibling↔sibling) is allowed.
    * **Explicit cross-group grants** (``comms_grants`` table) flip
      a deny to an allow.
    * **Self-send** is always allowed.
    * The **host-wide bearer** is the administrative / cross-host
      forwarding caller; it honours ``metadata.from_agent``
      verbatim (used by WI-4 forwarders authenticating with the
      destination's host bearer from ``peer-tokens/`` registry).

    Bearer auth is enforced by :class:`BearerAuthMiddleware`, which
    is the whole perimeter: the host-wide token, or 401/403.
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
    from .._state.state_db_forward import resolve_forward_target
    from .._state.state_db_nodes import is_local_node

    # Prefer the per-app ``local_host`` configured at ``create_app``
    # time; fall back to the env-based resolver for callers that
    # haven't pinned one. Per-app config matters for in-process
    # multi-host tests where the env is shared.
    local_host = getattr(request.app.state, "local_host", None) or _resolve_local_host(
        None
    )
    # OFF THE EVENT LOOP. Both resolvers are BLOCKING and, since the
    # comms_nodes directory moved to PostgreSQL on 2026-08-28, both can reach
    # the network: they fall through from the local SQLite ``instances``
    # lookup to a shared-store read. Called inline, a primary that swallows
    # SYN would stall THIS whole daemon — every request it is serving, not
    # just this one — for as long as the connect takes. The store's DSN now
    # carries an explicit ``connect_timeout`` (see
    # ``state_db_comms_nodes_store``), which bounds that to seconds; the
    # thread hop is what keeps even those seconds off the loop.
    if not await asyncio.to_thread(
        is_local_node, name=name, local_host=local_host
    ):
        # ADDRESSABILITY, not locality. `is_local_node` above answers "which
        # host", for which a live instances row suffices with or without a
        # port; this asks "where do I POST", for which it does not. They used
        # to be one call, so a live row with a NULL port was handed back as
        # the answer and 502'd here — without ever consulting comms_nodes,
        # which may hold a working address for the same name.
        target_info = await asyncio.to_thread(resolve_forward_target, name=name)
        if target_info is None:
            return JSONResponse(
                {
                    "error": (
                        f"target {name!r} resolves to a non-local host but "
                        "neither its instance row nor the comms_nodes graph "
                        "carries a usable address — cannot forward"
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

    # WI-2 ACL check. The sender is ``metadata.from_agent``, taken at
    # its word: the host-wide bearer is the only credential the
    # perimeter accepts, so every caller here is the administrative /
    # cross-host-forwarding one. This read used to consult
    # ``request.state.authenticated_node`` first (set by a per-node
    # bearer middleware removed 2026-08-28) — that value was ``None``
    # on every request ever served, so this is the same behaviour with
    # the never-taken path gone. See :func:`_acl.check_send_acl`.
    decision, reason = check_send_acl(
        claimed_from_agent=sac_meta.get("from_agent"),
        target=name,
    )
    if decision == "block":
        # Task #27 — block precedence path. The receiver previously
        # ran ``sac a2a block <sender> <target>``; honour the veto
        # without any receiver-side surface (no denied_attempt push,
        # no approve-prompt re-fire). The sender still gets a 403 —
        # they need to know their send did not land — but the
        # reason is intentionally generic so the block flag itself
        # does not leak. The receiver sees nothing.
        return deny_response(reason or "ACL deny")

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
            sender_id = sac_meta.get("from_agent")
            notif = mint_deny_notification(
                target=name,
                from_agent=sender_id,
                reason=reason or "ACL deny",
            )
            row_id = persist_event(target=name, event=notif)
            notif["_row_id"] = row_id
            report_zero_delivery(
                log,
                target=name,
                what="ACL-deny notification",
                delivered=await broker.publish(name, notif),
                row_id=row_id,
            )

            # sac-comms item D (lead a2a c42b3e3c, merged with
            # lead-sac-acl-blocked-attempt-notification). REPLACES
            # parent/child auto-grant. Publish a synthetic
            # system-level notification at the TARGET embedding the
            # exact ``sac a2a grant`` command, ACL-bypassing so the
            # operator can grant proactively. Rate-limited per
            # (sender, target) pair via the
            # ``acl_deny_notify_log`` table (cool-down default
            # 30 min, env-overridable via
            # ``SCITEX_ACL_DENY_NOTIFY_COOLDOWN_S``) so a misbehaving
            # sender cannot flood the receiver. ``should_notify_acl_deny``
            # is atomic (check + upsert in one tx) so a concurrent
            # burst publishes at most one synthetic frame per window.
            if sender_id:
                from .._state.state_db_acl_deny_notify import (
                    should_notify_acl_deny,
                )

                if should_notify_acl_deny(sender=sender_id, target=name):
                    synth = mint_acl_deny_synthetic_notification(
                        target=name,
                        sender=sender_id,
                        reason=reason or "ACL deny",
                    )
                    synth_row_id = persist_event(target=name, event=synth)
                    synth["_row_id"] = synth_row_id
                    report_zero_delivery(
                        log,
                        target=name,
                        what="grant-command notification",
                        delivered=await broker.publish(name, synth),
                        row_id=synth_row_id,
                    )

            # Task #27 — ACL approve-prompt flow (post-amendment).
            # On a CROSS-GROUP deny (the only deny reason the
            # receiver can REMEDY via grant/block), emit a single
            # receiver-facing prompt embedding BOTH the unblock
            # and block CLI commands so the receiver picks one.
            # Dedupe: ``record_pending_prompt`` returns True only
            # on the FIRST entry per (sender, target); subsequent
            # denied attempts return False and we suppress the
            # push (no flooding). The original message content is
            # NEVER stored — the receiver decides on identity,
            # not on content. If the receiver unblocks, the
            # sender resends.
            if sender_id and _looks_like_cross_group_deny(reason):
                from .._state.state_db_pending_approval import (
                    record_pending_prompt,
                )

                first_pending = record_pending_prompt(sender=sender_id, target=name)
                if first_pending:
                    prompt = _mint_approval_prompt(target=name, sender=sender_id)
                    prompt_row_id = persist_event(target=name, event=prompt)
                    prompt["_row_id"] = prompt_row_id
                    report_zero_delivery(
                        log,
                        target=name,
                        what="approval prompt",
                        delivered=await broker.publish(name, prompt),
                        row_id=prompt_row_id,
                    )
        return deny_response(reason or "ACL deny")

    # ADR-0013 Phase 1: typed event ``kind`` (``done`` / ``blocker`` /
    # ``status``) rides on metadata so the lead's inbox can filter
    # agent push events without parsing the free-form content. The
    # receiver accepts ANY string here — the sender side
    # (:mod:`scitex_agent_container._state.lead_inbox`) enforces the
    # allow-list so a typo fails at mint time, never after it has
    # landed in ``channel_events`` under the wrong label. Non-string
    # values are a loud 400 — silently coercing would let bad senders
    # poison the inbox shape.
    kind_meta = sac_meta.get("kind")
    if kind_meta is not None and not isinstance(kind_meta, str):
        return JSONResponse(
            {"error": "params.metadata.kind must be a string when set"},
            status_code=400,
        )

    # ``extra`` rides under metadata so structured side-channels (e.g.
    # the structural reaction-ack's ``reacted_dispatch_id``) survive the
    # publish path intact. A non-dict ``extra`` is dropped silently —
    # this is a permissive forwarder, not a schema validator, and the
    # consumers (reaction-ack updater, custom handlers) defensively
    # check shape before reading. Empty dicts are also dropped to keep
    # the persisted event row compact.
    extra_meta = sac_meta.get("extra")
    if not isinstance(extra_meta, dict) or not extra_meta:
        extra_meta = None

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
        kind=kind_meta,
        extra=extra_meta,
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
