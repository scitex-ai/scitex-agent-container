"""sac MCP **channel** send-side ``a2a_*`` tool surface.

Extracted from :mod:`scitex_agent_container._mcp.channel` (which grew past
the module size budget). Hosts the send-side tools the agent calls
explicitly — ``a2a_send``, ``a2a_reply``, ``a2a_ack``, ``a2a_peers``,
``a2a_inbox`` — while the receive-side adapter (SSE consumer, session
push, the automatic ``a2a_ack`` side-effect) stays in ``channel``.

The two halves share the per-process inbox ring buffer ``_recent``,
which lives in ``channel`` (the receive side fills it; these tools read
it). Importing it from ``channel`` keeps a single source of truth and
avoids a circular import (``channel`` imports *this* module, not the
other way around at module load).
"""

from __future__ import annotations

import json
import logging
from typing import Any

from .channel import _recent

log = logging.getLogger(__name__)


class SendError(RuntimeError):
    """A send/push could NOT reach or wake the target agent.

    Raised by the send helper when delivery demonstrably failed:

    * the transport raised (agent down / connection refused),
    * the listen server returned a non-2xx status (delivery error), or
    * the publish reported ``delivered_subscriber_count == 0`` — no live
      inbox subscriber, so the message woke nobody.

    The send-side ``a2a_*`` tools translate this into a loud, explicit
    ``{"error": ...}`` result for the calling agent (never a misleading
    success) and log it. STX hard rule: fail loudly, never silently drop
    or return a misleading success.
    """


def register_tools(
    server, *, agent_name: str, listen_url: str, bearer: str | None
) -> None:
    """Wire the a2a_* tools onto the channel server.

    All tools speak HTTP to local `sac listen`; receivers' inbox state
    (for reply/ack lookups) lives in the shared ``_recent`` ring.
    Tools-as-contract: each tool sets `from_agent`, `ts`, `msg_id`,
    `conversation_id` correctly so the calling agent can't get it wrong.
    """
    import uuid as _uuid

    from mcp.types import TextContent, Tool

    base = listen_url.rstrip("/")
    headers = {"Content-Type": "application/json"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"

    async def _post(path: str, payload: dict[str, Any]) -> dict[str, Any]:
        import httpx

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.post(f"{base}{path}", json=payload, headers=headers)
            try:
                return {"status": resp.status_code, "body": resp.json()}
            except Exception:  # stx-allow: fallback (reason: non-JSON body tolerated)
                return {"status": resp.status_code, "body": resp.text}

    async def _get(path: str) -> dict[str, Any]:
        import httpx

        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{base}{path}", headers=headers)
            try:
                return {"status": resp.status_code, "body": resp.json()}
            except Exception:  # stx-allow: fallback (reason: non-JSON body tolerated)
                return {"status": resp.status_code, "body": resp.text}

    async def _send_or_raise(
        target: str, path: str, payload: dict[str, Any]
    ) -> dict[str, Any]:
        """POST a send/push and FAIL LOUDLY when it cannot reach/wake (WI-2).

        Returns the parsed ``{status, body}`` on success. Raises
        :class:`SendError` — never a misleading success — when:

        * the transport raises (target agent down / connection refused),
        * the HTTP status is 5xx (server / infra delivery error), or
        * ``body.delivered_subscriber_count == 0`` — no live inbox
          subscriber, so the push woke nobody.

        A **4xx** is passed through verbatim (returned, not raised). A 4xx —
        notably the 403 ACL deny carrying ``body.reason`` — is a deliberate,
        structured policy/client response, already loud and actionable for
        the agent ("denial is the policy working"). Reshaping it into an
        opaque error string would lose the structured ``reason`` and is not
        the silent-drop/misleading-success the fail-loud rule targets.

        ``delivered_subscriber_count`` ABSENT is NOT treated as zero: some
        responses (cross-host forwards) don't carry it, and inventing a
        zero would be a false-positive failure. Only an explicit ``0``
        from the local publish path is the no-subscriber signal.
        """
        import httpx

        try:
            res = await _post(path, payload)
        except httpx.HTTPError as exc:
            log.warning("sac a2a: send to %r failed (transport): %s", target, exc)
            raise SendError(
                f"send to {target!r} failed: agent unreachable ({exc})"
            ) from exc

        status = res.get("status")
        # 5xx (and any non-int / sub-200) = server/infra delivery failure.
        # 4xx passes through (deliberate policy/client response — see above).
        if isinstance(status, int) and status >= 500:
            body = res.get("body")
            log.warning(
                "sac a2a: send to %r returned HTTP %s: %s", target, status, body
            )
            raise SendError(
                f"send to {target!r} failed: listen returned HTTP {status} ({body})"
            )
        if not isinstance(status, int) or status < 200:
            body = res.get("body")
            log.warning(
                "sac a2a: send to %r returned unexpected status %r: %s",
                target,
                status,
                body,
            )
            raise SendError(
                f"send to {target!r} failed: unexpected status {status!r} ({body})"
            )

        body = res.get("body")
        if isinstance(body, dict):
            delivered = body.get("delivered_subscriber_count")
            if isinstance(delivered, int) and delivered == 0:
                log.warning(
                    "sac a2a: send to %r reached no subscriber "
                    "(delivered_subscriber_count=0)",
                    target,
                )
                raise SendError(
                    f"send to {target!r} reached no live subscriber "
                    "(delivered_subscriber_count=0): the agent is not "
                    "subscribed to its inbox (down, not started, or its "
                    "channel adapter is not connected) — the message woke "
                    "nobody and was not delivered."
                )
        return res

    def _error_result(exc: SendError) -> "list[TextContent]":
        """Render a :class:`SendError` as a loud tool result."""
        return [TextContent(type="text", text=json.dumps({"error": str(exc)}))]

    def _find(msg_id: str) -> dict[str, Any] | None:
        for ev in reversed(_recent):
            if ev.get("msg_id") == msg_id:
                return ev
        return None

    def _wrap_message_send(content: str, **extra: Any) -> dict[str, Any]:
        # sac-extension fields (from_agent, conversation_id, ...) live
        # under ``params.metadata`` per A2A v1 — the SDK's strict proto
        # validator rejects unknown top-level params fields, so we
        # CANNOT splat them at the params root.
        metadata: dict[str, Any] = {"from_agent": agent_name}
        metadata.update({k: v for k, v in extra.items() if v is not None})
        params: dict[str, Any] = {
            "message": {
                "message_id": _uuid.uuid4().hex,
                "role": "ROLE_USER",
                "parts": [{"text": content}],
            },
            "metadata": metadata,
        }
        return {
            "jsonrpc": "2.0",
            "id": _uuid.uuid4().hex,
            # v1 gRPC-style method name; sac's `_publish_channel_event`
            # accepts both `SendMessage` and legacy `message/send`.
            "method": "SendMessage",
            "params": params,
        }

    @server.list_tools()
    async def _list_tools() -> list[Tool]:
        return [
            Tool(
                name="a2a_send",
                description=(
                    "Send a message to another agent on this sac listen. "
                    "Sets from_agent automatically; mints conversation_id "
                    "when omitted."
                ),
                inputSchema={
                    "type": "object",
                    "required": ["target", "content"],
                    "properties": {
                        "target": {"type": "string"},
                        "content": {"type": "string"},
                        "conversation_id": {"type": "string"},
                        "priority": {
                            "type": "string",
                            "enum": ["low", "normal", "high"],
                        },
                        "requires_reply": {"type": "boolean"},
                    },
                },
            ),
            Tool(
                name="a2a_reply",
                description=(
                    "Reply to a received message. Looks up the original "
                    "sender by msg_id; carries the same conversation_id."
                ),
                inputSchema={
                    "type": "object",
                    "required": ["in_reply_to", "content"],
                    "properties": {
                        "in_reply_to": {"type": "string"},
                        "content": {"type": "string"},
                    },
                },
            ),
            Tool(
                name="a2a_ack",
                description=(
                    "Acknowledge a received message without content. "
                    "Cheap 'got it' to a sender that set requires_reply=true."
                ),
                inputSchema={
                    "type": "object",
                    "required": ["msg_id"],
                    "properties": {"msg_id": {"type": "string"}},
                },
            ),
            Tool(
                name="a2a_peers",
                description="List reachable agents on this sac listen.",
                inputSchema={"type": "object", "properties": {}},
            ),
            Tool(
                name="a2a_inbox",
                description=(
                    "Return up to `limit` most recent received messages "
                    "from this agent's inbox buffer (default 20)."
                ),
                inputSchema={
                    "type": "object",
                    "properties": {
                        "limit": {"type": "integer", "minimum": 1, "maximum": 200},
                    },
                },
            ),
        ]

    @server.call_tool()
    async def _call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
        if name == "a2a_send":
            target = arguments["target"]
            content = arguments["content"]
            payload = _wrap_message_send(
                content,
                conversation_id=arguments.get("conversation_id") or _uuid.uuid4().hex,
                priority=arguments.get("priority"),
                requires_reply=arguments.get("requires_reply"),
            )
            try:
                res = await _send_or_raise(
                    target, f"/agents/{target}/message:send", payload
                )
            except SendError as exc:
                return _error_result(exc)
            return [TextContent(type="text", text=json.dumps(res))]

        if name == "a2a_reply":
            mid = arguments["in_reply_to"]
            orig = _find(mid)
            if orig is None:
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {"error": f"unknown msg_id {mid} (inbox window)"}
                        ),
                    )
                ]
            target = orig.get("from_agent", "")
            if not target:
                return [
                    TextContent(
                        type="text",
                        text=json.dumps({"error": "original sender unknown"}),
                    )
                ]
            payload = _wrap_message_send(
                arguments["content"],
                conversation_id=orig.get("conversation_id"),
                in_reply_to=mid,
            )
            try:
                res = await _send_or_raise(
                    target, f"/agents/{target}/message:send", payload
                )
            except SendError as exc:
                return _error_result(exc)
            return [TextContent(type="text", text=json.dumps(res))]

        if name == "a2a_ack":
            mid = arguments["msg_id"]
            orig = _find(mid)
            if orig is None:
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {"error": f"unknown msg_id {mid} (inbox window)"}
                        ),
                    )
                ]
            target = orig.get("from_agent", "")
            if not target:
                return [
                    TextContent(
                        type="text",
                        text=json.dumps({"error": "original sender unknown"}),
                    )
                ]
            payload = _wrap_message_send(
                "",
                conversation_id=orig.get("conversation_id"),
                in_reply_to=mid,
                ack=True,
            )
            try:
                res = await _send_or_raise(
                    target, f"/agents/{target}/message:send", payload
                )
            except SendError as exc:
                return _error_result(exc)
            return [TextContent(type="text", text=json.dumps(res))]

        if name == "a2a_peers":
            # No trailing slash: sac listen registers `/agents` and a GET to
            # `/agents/` 307-redirects, which httpx does not follow by default
            # (a2a_peers then returns a bare 307 with empty body).
            res = await _get("/agents")
            return [TextContent(type="text", text=json.dumps(res))]

        if name == "a2a_inbox":
            limit = int(arguments.get("limit") or 20)
            items = list(_recent)[-limit:]
            return [
                TextContent(
                    type="text", text=json.dumps({"count": len(items), "items": items})
                )
            ]

        return [
            TextContent(
                type="text", text=json.dumps({"error": f"unknown tool: {name}"})
            )
        ]


__all__ = ["SendError", "register_tools"]
