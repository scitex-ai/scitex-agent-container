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

# Operator directive (2026-07-09): rendered <channel source="..."> must be
# the fixed SYSTEM identity ("sac"), not a raw agent name -- matches cct's
# source="cct" / scitex-todo's source="stodo". Sender identity moves to a
# separate from_agent meta key. Env-overridable (SAC_MCP_* convention, #591).
_CHANNEL_SOURCE_ENV_VAR = "SAC_MCP_CHANNEL_SOURCE"
_CHANNEL_SOURCE_DEFAULT = "sac"

# WI-1 wake-on-push primitives live in ``_channel_wake`` (extracted to keep
# this receive-side adapter under the module size budget). Re-exported here
# so the historical ``from .channel import _wake_turn`` import path — used by
# ``_push_channel_event`` below and the test suite — keeps working.
# Auto-ack subsystem (env gate, loop-guard, rate limiter, outbound POST)
# lives in ``_channel_auto_ack`` so this receive-side adapter stays under
# the size budget. Re-exported here for historical import paths:
# ``from scitex_agent_container._mcp.channel import _post_auto_ack``.
from ._channel_auto_ack import (  # noqa: E402,F401
    _AUTO_ACK_RATE_MAX_DEFAULT,
    _AUTO_ACK_RATE_WINDOW_DEFAULT,
    _auto_ack_enabled,
    _auto_ack_rate_allow,
    _auto_ack_rate_limits,
    _auto_ack_tripped,
    _auto_ack_window,
    _post_auto_ack,
    _should_auto_ack,
)
from ._channel_post_deliver import run_post_deliver_receipts  # noqa: E402
from ._channel_reaction_ack import (  # noqa: E402,F401
    absorb_reaction_ack,
    is_reaction_event,
    post_reaction_ack,
    reaction_ack_enabled,
    should_emit_reaction_ack,
)
from ._channel_self_register import (  # noqa: E402
    refresh_node as _refresh_comms_node,
)

# The SSE inbox consumer + its reconnect policy (bounded connect, bounded
# read, jittered backoff) live in ``_channel_sse`` — the component whose
# failure deafens an agent, kept readable on its own. Re-exported here so the
# historical ``from ...channel import _consume_sse`` path (used by the tests
# and by ``_serve`` below) keeps working unchanged.
from ._channel_sse import (  # noqa: E402,F401
    _SSE_CONNECT_TIMEOUT_S,
    _consume_sse,
    _jittered_backoff,
    _sse_read_timeout_s,
)
from ._channel_wake import _should_wake_turn, _wake_text, _wake_turn  # noqa: E402,F401


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
    shape: ``{content, meta: {source, from_agent, chat_id, ts, ...}}``.

    ``meta.source`` is the fixed system identity ("sac", overridable via
    :data:`_CHANNEL_SOURCE_ENV_VAR`) — the sender's own identity lives in
    ``meta.from_agent`` instead, so the two never collide in the rendered
    ``<channel source="..." from_agent="..." ...>`` tag.

    Every ``meta`` value is stringified via :func:`_meta_str` — the
    client schema rejects non-string values (see that helper).
    ``meta.ts`` is rendered as ISO-8601 UTC via
    :func:`_state.state_db_channel.format_ts_iso` so a receiving
    session sees ``<channel ts="2026-04-21T09:30:00Z" ...>`` instead
    of the raw unix-seconds float the bus stores. On-disk storage of
    ``channel_events.ts`` is unchanged — only the rendered form
    here is ISO-8601.
    """
    from .._state.state_db_channel import format_ts_iso

    source = (
        os.environ.get(_CHANNEL_SOURCE_ENV_VAR, "").strip() or _CHANNEL_SOURCE_DEFAULT
    )
    meta: dict[str, Any] = {
        "source": source,
        "from_agent": _meta_str(event.get("from_agent", "unknown")),
        "ts": format_ts_iso(event.get("ts", "")),
        "msg_id": _meta_str(event.get("msg_id", "")),
    }
    # Operator #16: surface the sender's account + live quota fields to
    # the receiving agent so peer-side back-pressure logic (and the
    # human reader of <channel> tags) can see "this came from
    # `ywatanabe` at 5h:19% / 7d:3% / TTL=7.7h" at a glance. Names
    # match the wire keys emitted by
    # ``_account.quota_cache.build_a2a_metadata``.
    for k in (
        "conversation_id",
        "in_reply_to",
        "priority",
        "requires_reply",
        "account",
        "used_pct_5h",
        "used_pct_7d",
        "token_ttl_hours",
    ):
        if k in event:
            meta[k] = _meta_str(event[k])
    return {
        "content": event.get("content", ""),
        "meta": meta,
    }


async def _push_channel_event(
    session: Any,
    event: dict[str, Any],
    *,
    agent_name: str | None = None,
    listen_url: str | None = None,
    bearer: str | None = None,
    turn_url: str | None = None,
) -> None:
    """Deliver ``event`` to the agent, then emit the stage-2 read-receipt.

    Delivery has two modes:

    * **Wake-on-push (WI-1)** — when ``turn_url`` is set and the event
      qualifies (:func:`_should_wake_turn`), POST it to the agent's own
      ``/v1/turn`` so an IDLE session WAKES and processes the message now.
      Push then behaves like the lead's Telegram channel. The driven turn
      carries the message as its input, so the ``notifications/claude/
      channel`` push is intentionally SKIPPED in this mode — pushing it too
      would make the agent see the same message twice (once as turn input,
      once as a buffered ``<channel>`` tag on the next turn boundary).
    * **Notification-only (legacy / external nodes)** — when no ``turn_url``
      is configured (a plain ``claude`` node with no colocated runner), push
      the ``notifications/claude/channel`` message through ``session`` so an
      already-active turn renders the ``<channel>`` tag. This cannot wake an
      idle session — that is exactly the limitation ``turn_url`` removes.

    Split out of the SSE consumer so the receive→inject path is directly
    testable end-to-end (see ``tests/.../_mcp/test_channel.py``). That seam
    used to be untested, which let a silent drop ship.

    The auto-ack is a best-effort side-effect: it runs only *after* delivery
    and its failure is logged loudly but never re-raised, so a flaky receipt
    can neither block delivery nor kill the long-lived SSE consumer.

    **Reaction-ack ABSORPTION (sender-side)** — a ``kind="reaction"``
    event is the receiver's structural 👀 receipt to one of OUR previous
    sends. It is absorbed (dispatch ledger updated to ``STATUS_REACTED``)
    and NOT injected into the running session — receipts are a wire
    signal, not a user-visible message. See ``_channel_reaction_ack``.
    """
    from mcp.shared.message import SessionMessage
    from mcp.types import JSONRPCMessage, JSONRPCNotification

    # Sender-side absorption: a structural reaction-ack updates the
    # dispatch ledger and is then suppressed from session injection.
    # The event is still buffered into ``_recent`` so a2a_inbox callers
    # can audit that the receipt landed.
    if is_reaction_event(event):
        _recent.append(event)
        absorb_reaction_ack(event)
        return

    # Buffer for a2a_reply / a2a_ack lookups by msg_id.
    _recent.append(event)

    woke = False
    if turn_url is not None and _should_wake_turn(event):
        # Wake path: drive a turn on the agent's own runner. A failure here
        # is NOT silently contained — re-raise so the SSE consumer's
        # on_event wrapper logs it loudly (WI-2 fail-loud) while keeping the
        # long-lived stream alive. We must never pretend a wake succeeded.
        await _wake_turn(event, turn_url=turn_url, bearer=bearer)
        woke = True

    if not woke:
        # Notification-only delivery (no colocated runner to wake, or an
        # ack/empty event that does not warrant a driven turn).
        params = _build_notification(event)
        content = params.get("content", "")
        if isinstance(content, str) and content.strip():
            msg = JSONRPCMessage(
                JSONRPCNotification(
                    jsonrpc="2.0",
                    method="notifications/claude/channel",
                    params=params,
                )
            )
            await session.send_message(SessionMessage(msg))

    # Post-delivery receipts: contentless auto-ack (legacy noise-filtered
    # path) + structural reaction-ack (the comm-miss-detectable signal,
    # lead a2a 1781e82a). Both are best-effort and share the per-sender
    # rate cap; see ``_channel_post_deliver.run_post_deliver_receipts``.
    await run_post_deliver_receipts(
        event,
        agent_name=agent_name,
        listen_url=listen_url,
        bearer=bearer,
    )


async def _serve(
    read_stream: Any,
    write_stream: Any,
    *,
    name: str,
    listen_url: str,
    bearer: str | None,
    turn_url: str | None = None,
) -> None:
    """Drive the MCP session **and** the SSE consumer over the given
    streams, keeping a handle to the session so the consumer can push.

    ``turn_url`` (WI-1) is the agent's own colocated ``/v1/turn`` endpoint.
    When set, each received bus event WAKES the session by driving a turn
    there (push ≡ Telegram); when ``None`` the adapter falls back to the
    notification-only push that cannot advance an idle turn.

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
                await _push_channel_event(
                    session,
                    event,
                    agent_name=name,
                    listen_url=listen_url,
                    bearer=bearer,
                    turn_url=turn_url,
                )
            except Exception as exc:  # stx-allow: fallback (reason: one failed push/wake must not kill the long-lived SSE consumer; logged loudly, never silent)
                log.warning("sac channel: delivering inbox event failed: %s", exc)

        sse_task: asyncio.Task[None] = asyncio.create_task(
            _consume_sse(sse_url, bearer, on_event)
        )

        # ADR-0014 + lead-row-port-zero bug fix (2026-06-03):
        # Self-register THIS channel into ``comms_nodes`` so the federated
        # graph contains the lead (or any sac mcp channel node) durably.
        # The initial UPSERT happens INSIDE refresh_node's first iteration
        # (no leading sleep), so a single task covers both startup register
        # and periodic ``updated_at`` refresh. Best-effort: a failed write
        # logs a warning but never kills the SSE consumer or the MCP
        # handshake. Cancelled in ``finally`` alongside the SSE task.
        reg_task: asyncio.Task[None] = asyncio.create_task(
            _refresh_comms_node(name=name, listen_url=listen_url)
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
            reg_task.cancel()


async def _run(
    name: str, listen_url: str, bearer: str | None, turn_url: str | None = None
) -> None:
    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read_stream, write_stream):
        await _serve(
            read_stream,
            write_stream,
            name=name,
            listen_url=listen_url,
            bearer=bearer,
            turn_url=turn_url,
        )


def _register_tools(
    server, *, agent_name: str, listen_url: str, bearer: str | None
) -> None:
    """Wire the send-side ``a2a_*`` tools onto the channel server.

    Thin re-export of :func:`scitex_agent_container._mcp._channel_tools.register_tools`.
    The tool surface was extracted to its own module to keep this
    receive-side adapter under the module size budget; this wrapper
    preserves the historical import path (callers and tests import
    ``_register_tools`` from ``channel``).
    """
    from ._channel_tools import register_tools

    register_tools(server, agent_name=agent_name, listen_url=listen_url, bearer=bearer)


def main(
    name: str | None = None,
    listen_url: str | None = None,
    turn_url: str | None = None,
) -> None:
    """CLI entry point. Bearer comes from ``SAC_LISTEN_BEARER`` env.

    ``name`` is OPTIONAL (TG 12706 / #356 follow-up). When omitted, the
    channel walks the current working directory UPWARD for the first
    ``.scitex/agent-container/agents/self/spec.yaml`` hit (see
    :func:`_channel_self_peer_discovery.discover_self_identity` — ONE
    generic shape, no per-node exceptions, no home-scope fallback). The
    discovered identity supplies the name; the discovered ``listen_url``
    is used unless explicitly overridden (precedence: explicit
    ``listen_url`` arg > discovered.listen_url > ``$SAC_LISTEN_BASE_URL``
    > ``http://127.0.0.1:7878``). When ``name`` is omitted AND discovery
    returns ``None``, a :class:`RuntimeError` is raised naming the
    spec-path convention so the operator knows where to drop the file.

    ``turn_url`` (WI-1) is the agent's own ``/v1/turn`` endpoint; when set,
    each received bus event WAKES the session by driving a turn there so a
    push to an idle agent is processed immediately (push ≡ Telegram).
    """
    discovered_listen_url: str | None = None
    if name is None:
        from ._channel_self_peer_discovery import discover_self_identity

        discovered = discover_self_identity()
        if discovered is None:
            raise RuntimeError(
                "sac mcp channel: --name was omitted and no self spec was "
                "found by walking the cwd upward for "
                "'.scitex/agent-container/agents/self/spec.yaml'. "
                "Either pass --name <agent>, or drop a self spec at "
                "<project-root>/.scitex/agent-container/agents/self/spec.yaml "
                "with a 'listen_url:' field (see "
                "scitex_agent_container._listen._self_peers for the shape)."
            )
        name = discovered.name
        discovered_listen_url = discovered.listen_url

    listen = (
        listen_url
        or discovered_listen_url
        or os.environ.get("SAC_LISTEN_BASE_URL", "http://127.0.0.1:7878")
    )
    bearer = os.environ.get("SAC_LISTEN_BEARER")
    asyncio.run(_run(name, listen, bearer, turn_url))


__all__ = ["main"]
