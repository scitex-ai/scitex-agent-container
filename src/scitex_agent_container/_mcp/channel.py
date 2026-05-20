"""sac MCP **channel** server — the receive-side Claude-session adapter.

Run as a stdio MCP subprocess of Claude Code:

    sac mcp channel --name <node-id> [--listen-url http://127.0.0.1:7878]

Behaviour:

1. Speaks the standard MCP handshake over stdio (so `claude
   --dangerously-load-development-channels server:sac` is happy).
2. After initialise, opens an HTTP SSE connection to the local
   `sac listen` at ``/agents/<node-id>/inbox/stream`` (ADR-0004,
   ADR-0008).
3. For every event the bus pushes, emits a JSON-RPC notification:

       method: notifications/claude/channel
       params: { content, meta }

   so Claude renders ``<channel source="..." chat_id="..." ...>`` in
   the running session (see Claude Code channels reference).

This module hosts the receive-side adapter **and** the send-side
``a2a_*`` MCP tools (``a2a_send``, ``a2a_reply``, ``a2a_ack``,
``a2a_peers``, ``a2a_inbox``). They live together because they
share the per-process inbox ring buffer (``_recent``) that
``a2a_reply`` / ``a2a_ack`` consult to look up the original sender
of a msg_id.

Per ADR-0008 (sac node-transport boundary), a *node* — sac-managed
or external — joins the comms graph by running this command. An
external node (e.g. a plain ``claude`` CLI session) has no
container and no spec; its AgentCard is synthesised by
``_listen/_nodes.py::synthesize_external_card`` at first connect.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from collections import deque
from typing import Any

log = logging.getLogger(__name__)

# Bounded ring buffer of recently received events so the a2a_reply +
# a2a_ack tools can look up the original sender by msg_id without the
# agent having to thread that data through itself (tools-as-contract).
_INBOX_CAP = 200
_recent: "deque[dict[str, Any]]" = deque(maxlen=_INBOX_CAP)


async def _consume_sse(
    url: str,
    bearer: str | None,
    on_event: "callable[[dict[str, Any]], asyncio.Future[None]]",
) -> None:
    """Long-lived SSE consumer. Reconnects with backoff on disconnect.

    Each `event: message` frame's `data:` line is JSON-decoded and
    handed to ``on_event``. Comment frames (``: ...``) are ignored —
    sac listen emits one at connection time as a keep-alive hint.
    """
    try:
        import httpx
    except Exception as exc:  # pragma: no cover
        # Catch broadly: optional deps can fail at *import time* with
        # non-ImportError errors (e.g. a misconfigured transitive dep
        # raising RuntimeError). Surface them as an actionable
        # ImportError so the caller knows install/upgrade is needed.
        raise ImportError(
            "httpx is required for sac mcp channel — install with `pip install httpx`"
        ) from exc

    headers = {"Accept": "text/event-stream"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"

    backoff = 0.5
    while True:
        try:
            async with httpx.AsyncClient(timeout=None) as client:
                async with client.stream("GET", url, headers=headers) as resp:
                    if resp.status_code != 200:
                        body = await resp.aread()
                        log.warning(
                            "sac channel SSE %s returned %d: %s",
                            url,
                            resp.status_code,
                            body[:200],
                        )
                    else:
                        backoff = 0.5
                        data_lines: list[str] = []
                        async for line in resp.aiter_lines():
                            if not line:
                                # frame separator — dispatch what we have
                                if data_lines:
                                    payload = "\n".join(data_lines)
                                    data_lines = []
                                    try:
                                        event = json.loads(payload)
                                    except json.JSONDecodeError:
                                        log.warning(
                                            "sac channel SSE bad JSON: %r",
                                            payload[:200],
                                        )
                                        continue
                                    await on_event(event)
                                continue
                            if line.startswith(":"):
                                continue  # comment frame
                            if line.startswith("data:"):
                                data_lines.append(line[5:].lstrip())
        except Exception as exc:  # stx-allow: fallback (reason: long-lived SSE — must retry on any transient error)
            log.warning(
                "sac channel SSE error (%s); reconnecting in %.1fs", exc, backoff
            )
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 30.0)


def _meta_str(value: Any) -> str:
    """Coerce a meta value to the string the Claude Code client demands.

    Claude Code's ``claude/channel`` notification schema types **every**
    ``meta`` value as a string. A raw bool (``requires_reply``) trips the
    client's Zod validator — its notification handler throws and the
    pushed turn is silently dropped (mcp-logs-sac: "Uncaught error in
    notification handler: ZodError ... requires_reply: expected string,
    received boolean"). Render bools JSON-style so a receiving agent can
    compare them verbatim against ``"true"`` / ``"false"``.
    """
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _build_notification(event: dict[str, Any]) -> dict[str, Any]:
    """Project a bus event onto the Claude Code channel notification
    shape: ``{content, meta: {source, chat_id, ts, ...}}``.

    Every ``meta`` value is stringified via :func:`_meta_str` — the
    client schema rejects non-string values (see that helper).
    """
    meta: dict[str, Any] = {
        "source": _meta_str(event.get("from_agent", "unknown")),
        "ts": _meta_str(event.get("ts", "")),
        "msg_id": _meta_str(event.get("msg_id", "")),
    }
    for k in ("conversation_id", "in_reply_to", "priority", "requires_reply"):
        if k in event:
            meta[k] = _meta_str(event[k])
    return {
        "content": event.get("content", ""),
        "meta": meta,
    }


async def _push_channel_event(session: Any, event: dict[str, Any]) -> None:
    """Buffer ``event`` and push it to the client as a
    ``notifications/claude/channel`` message through ``session``.

    Split out of the SSE consumer so the receive→inject path is
    directly testable end-to-end (see ``tests/.../_mcp/test_channel.py``
    ``test_serve_pushes_sse_event_to_client_as_channel_notification``).
    That seam used to be untested, which let a silent drop ship.
    """
    from mcp.shared.message import SessionMessage
    from mcp.types import JSONRPCMessage, JSONRPCNotification

    # Buffer for a2a_reply / a2a_ack lookups by msg_id.
    _recent.append(event)
    params = _build_notification(event)
    msg = JSONRPCMessage(
        JSONRPCNotification(
            jsonrpc="2.0",
            method="notifications/claude/channel",
            params=params,
        )
    )
    await session.send_message(SessionMessage(msg))


async def _serve(
    read_stream: Any,
    write_stream: Any,
    *,
    name: str,
    listen_url: str,
    bearer: str | None,
) -> None:
    """Drive the MCP session **and** the SSE consumer over the given
    streams, keeping a handle to the session so the consumer can push.

    We deliberately do not call ``Server.run``: it constructs its
    ``ServerSession`` internally and never exposes it, so a side
    channel (the inbox SSE consumer) would have no session to push
    ``notifications/claude/channel`` through — exactly the bug that
    silently dropped every inbound event. Owning the session here is
    the supported way to send server-initiated notifications with the
    low-level API (mcp >= 1.x; pinned).
    """
    from contextlib import AsyncExitStack

    import anyio
    from mcp.server.lowlevel import Server
    from mcp.server.session import ServerSession

    server = Server(name=f"sac-channel-{name}")
    _register_tools(server, agent_name=name, listen_url=listen_url, bearer=bearer)
    sse_url = f"{listen_url.rstrip('/')}/agents/{name}/inbox/stream"

    async with AsyncExitStack() as stack:
        lifespan_context = await stack.enter_async_context(server.lifespan(server))
        session = await stack.enter_async_context(
            ServerSession(
                read_stream,
                write_stream,
                # Declare the `claude/channel` experimental capability in the
                # initialize response. Without it Claude Code logs "Channel
                # notifications skipped: server did not declare claude/channel
                # capability" and drops every notifications/claude/channel we
                # push — the receive→inject seam dies on the *client* side even
                # though send_message succeeded. This is distinct from the
                # earlier silent-drop (the server not pushing at all).
                server.create_initialization_options(
                    experimental_capabilities={"claude/channel": {}},
                ),
            )
        )

        async def on_event(event: dict[str, Any]) -> None:
            try:
                await _push_channel_event(session, event)
            except Exception as exc:  # stx-allow: fallback (reason: one failed push must not kill the long-lived SSE consumer; logged loudly, never silent)
                log.warning("sac channel: pushing inbox event failed: %s", exc)

        sse_task: asyncio.Task[None] = asyncio.create_task(
            _consume_sse(sse_url, bearer, on_event)
        )
        try:
            async with anyio.create_task_group() as tg:
                async for message in session.incoming_messages:
                    tg.start_soon(
                        server._handle_message,
                        message,
                        session,
                        lifespan_context,
                        False,
                    )
        finally:
            sse_task.cancel()


async def _run(name: str, listen_url: str, bearer: str | None) -> None:
    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read_stream, write_stream):
        await _serve(
            read_stream,
            write_stream,
            name=name,
            listen_url=listen_url,
            bearer=bearer,
        )


def _register_tools(
    server, *, agent_name: str, listen_url: str, bearer: str | None
) -> None:
    """Wire the a2a_* tools onto the channel server.

    All tools speak HTTP to local `sac listen`; receivers' inbox state
    (for reply/ack lookups) lives in the module-level ``_recent`` ring.
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
            res = await _post(f"/agents/{target}/message:send", payload)
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
            res = await _post(f"/agents/{target}/message:send", payload)
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
            res = await _post(f"/agents/{target}/message:send", payload)
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


def main(name: str, listen_url: str | None = None) -> None:
    """CLI entry point. Bearer comes from ``SAC_LISTEN_BEARER`` env."""
    listen = listen_url or os.environ.get(
        "SAC_LISTEN_BASE_URL", "http://127.0.0.1:7878"
    )
    bearer = os.environ.get("SAC_LISTEN_BEARER")
    asyncio.run(_run(name, listen, bearer))


__all__ = ["main"]
