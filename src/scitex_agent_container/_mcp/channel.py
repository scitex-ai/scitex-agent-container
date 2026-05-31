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
import time
from collections import deque
from typing import Any

from .._env import getenv as _sac_env

log = logging.getLogger(__name__)

# Bounded ring buffer of recently received events so the a2a_reply +
# a2a_ack tools can look up the original sender by msg_id without the
# agent having to thread that data through itself (tools-as-contract).
_INBOX_CAP = 200
_recent: "deque[dict[str, Any]]" = deque(maxlen=_INBOX_CAP)


def _auto_ack_enabled() -> bool:
    """Whether the receive-side adapter emits an automatic ``a2a_ack`` the
    moment it injects a received bus event into the session.

    Two-stage receipt, infra-automatic: the receiving agent calls nothing —
    the channel adapter does. Stage 1 ("delivered") is the send response's
    ``delivered_subscriber_count``; stage 2 ("read") is this auto-ack,
    emitted on injection. Default ON; set ``SAC_CHANNEL_AUTO_ACK`` to one of
    ``0/false/no/off`` (case-insensitive) to disable.
    """
    raw = os.environ.get("SAC_CHANNEL_AUTO_ACK")
    if raw is None:
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off")


# WI-1 wake-on-push primitives live in ``_channel_wake`` (extracted to keep
# this receive-side adapter under the module size budget). Re-exported here
# so the historical ``from .channel import _wake_turn`` import path — used by
# ``_push_channel_event`` below and the test suite — keeps working.
from ._channel_wake import _should_wake_turn, _wake_text, _wake_turn  # noqa: E402,F401


def _should_auto_ack(event: dict[str, Any]) -> bool:
    """Loop-guard for the auto-ack side-effect.

    An auto-ack is itself a message that lands in the sender's inbox and is
    injected by *their* adapter — which would auto-ack back, ad infinitum.
    Break the cycle:

    - never auto-ack an event that is itself an ack (truthy ``ack`` flag —
      the same metadata flag the ``a2a_ack`` tool stamps), and
    - never auto-ack an event with no identifiable original sender
      (``from_agent`` missing/empty): there is nowhere to send the receipt.
    """
    if not event.get("from_agent"):
        return False
    if event.get("ack"):
        return False
    return True


# --- Auto-ack rate limiter (belt-and-suspenders loop breaker) -------------
# Even with the ack-flag loop-guard (``_should_auto_ack``), a future
# regression in flag propagation could restart an ack-on-ack cycle. This
# sliding-window cap ensures any such loop self-terminates: once a sender
# exceeds the budget within the window we stop auto-acking it and log
# loudly (fail-loud, never a silent drop).
_AUTO_ACK_RATE_MAX_DEFAULT = 20
_AUTO_ACK_RATE_WINDOW_DEFAULT = 60.0
# Per-sender sliding window of auto-ack emission timestamps.
_auto_ack_window: "dict[str, deque[float]]" = {}
# Senders currently over budget — latches the loud log to once per trip.
_auto_ack_tripped: "set[str]" = set()


def _auto_ack_rate_limits() -> "tuple[int, float]":
    """Resolve ``(max_acks, window_seconds)`` for the auto-ack cap.

    Configurable via ``SAC_AUTO_ACK_RATE_MAX`` / ``SAC_AUTO_ACK_RATE_WINDOW_S``
    (and their ``SCITEX_AGENT_CONTAINER_*`` aliases). A non-positive max
    disables the cap (explicit opt-out).
    """
    raw_max = _sac_env("AUTO_ACK_RATE_MAX")
    raw_win = _sac_env("AUTO_ACK_RATE_WINDOW_S")
    try:
        max_n = int(raw_max) if raw_max is not None else _AUTO_ACK_RATE_MAX_DEFAULT
    except (TypeError, ValueError):
        max_n = _AUTO_ACK_RATE_MAX_DEFAULT
    try:
        window = (
            float(raw_win) if raw_win is not None else _AUTO_ACK_RATE_WINDOW_DEFAULT
        )
    except (TypeError, ValueError):
        window = _AUTO_ACK_RATE_WINDOW_DEFAULT
    return max_n, window


def _auto_ack_rate_allow(sender: str, *, now: "float | None" = None) -> bool:
    """Whether an auto-ack to ``sender`` is within the rate budget.

    Sliding-window cap keyed by sender. Returns ``True`` and records the
    emission when within budget; returns ``False`` (logging loudly, once
    per trip) when the cap is exceeded inside the window. After the window
    clears, emission resumes and the loud-log latch resets so a fresh loop
    is reported again. ``now`` is injectable for deterministic tests.
    """
    max_n, window = _auto_ack_rate_limits()
    if max_n <= 0:
        return True
    ts = time.monotonic() if now is None else now
    dq = _auto_ack_window.setdefault(sender, deque())
    cutoff = ts - window
    while dq and dq[0] < cutoff:
        dq.popleft()
    if len(dq) >= max_n:
        if sender not in _auto_ack_tripped:
            _auto_ack_tripped.add(sender)
            log.warning(
                "sac channel: auto-ack rate cap hit for sender %r "
                "(%d auto-acks within %.0fs) — suppressing further "
                "auto-acks to this sender until the window clears. "
                "Possible ack loop.",
                sender,
                len(dq),
                window,
            )
        return False
    dq.append(ts)
    _auto_ack_tripped.discard(sender)
    return True


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
    ``meta.ts`` is rendered as ISO-8601 UTC via
    :func:`_state.state_db_channel.format_ts_iso` so a receiving
    session sees ``<channel ts="2026-04-21T09:30:00Z" ...>`` instead
    of the raw unix-seconds float the bus stores. On-disk storage of
    ``channel_events.ts`` is unchanged — only the rendered form
    here is ISO-8601.
    """
    from .._state.state_db_channel import format_ts_iso

    meta: dict[str, Any] = {
        "source": _meta_str(event.get("from_agent", "unknown")),
        "ts": format_ts_iso(event.get("ts", "")),
        "msg_id": _meta_str(event.get("msg_id", "")),
    }
    for k in ("conversation_id", "in_reply_to", "priority", "requires_reply"):
        if k in event:
            meta[k] = _meta_str(event[k])
    return {
        "content": event.get("content", ""),
        "meta": meta,
    }


async def _post_auto_ack(
    event: dict[str, Any],
    *,
    agent_name: str,
    listen_url: str,
    bearer: str | None,
) -> None:
    """POST an automatic ``a2a_ack`` back to the sender of ``event``.

    Reuses the exact ``/agents/<sender>/message:send`` path and metadata
    shape the ``a2a_ack`` tool uses (``ack=True``, ``in_reply_to`` the
    original ``msg_id``, same ``conversation_id``). ``ack=True`` is the
    loop-guard marker: the sender's adapter sees it and won't ack back.

    Caller guarantees ``event`` passed :func:`_should_auto_ack` (so
    ``from_agent`` is present and the event is not itself an ack).
    """
    import uuid as _uuid

    import httpx

    target = event["from_agent"]
    metadata: dict[str, Any] = {"from_agent": agent_name, "ack": True}
    msg_id = event.get("msg_id")
    if msg_id:
        metadata["in_reply_to"] = msg_id
    conversation_id = event.get("conversation_id")
    if conversation_id:
        metadata["conversation_id"] = conversation_id
    payload = {
        "jsonrpc": "2.0",
        "id": _uuid.uuid4().hex,
        "method": "SendMessage",
        "params": {
            "message": {
                "message_id": _uuid.uuid4().hex,
                "role": "ROLE_USER",
                "parts": [{"text": ""}],
            },
            "metadata": metadata,
        },
    }
    base = listen_url.rstrip("/")
    headers = {"Content-Type": "application/json"}
    if bearer:
        headers["Authorization"] = f"Bearer {bearer}"
    async with httpx.AsyncClient(timeout=10.0) as client:
        resp = await client.post(
            f"{base}/agents/{target}/message:send", json=payload, headers=headers
        )
        resp.raise_for_status()


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
    """
    from mcp.shared.message import SessionMessage
    from mcp.types import JSONRPCMessage, JSONRPCNotification

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
        msg = JSONRPCMessage(
            JSONRPCNotification(
                jsonrpc="2.0",
                method="notifications/claude/channel",
                params=params,
            )
        )
        await session.send_message(SessionMessage(msg))

    # Stage-2 receipt: auto-ack AFTER successful delivery. Best-effort —
    # never let an ack failure propagate past this point.
    if (
        agent_name is not None
        and listen_url is not None
        and _auto_ack_enabled()
        and _should_auto_ack(event)
        # _should_auto_ack guarantees a truthy ``from_agent`` above, so the
        # short-circuit makes this index safe; the cap is the last gate.
        and _auto_ack_rate_allow(event["from_agent"])
    ):
        try:
            await _post_auto_ack(
                event,
                agent_name=agent_name,
                listen_url=listen_url,
                bearer=bearer,
            )
        except Exception as exc:  # stx-allow: fallback (reason: best-effort auto-ack; a failed receipt must not block injection or kill the SSE consumer — logged loudly, never silent)
            log.warning(
                "sac channel: auto-ack to %r failed: %s",
                event.get("from_agent"),
                exc,
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


def main(name: str, listen_url: str | None = None, turn_url: str | None = None) -> None:
    """CLI entry point. Bearer comes from ``SAC_LISTEN_BEARER`` env.

    ``turn_url`` (WI-1) is the agent's own ``/v1/turn`` endpoint; when set,
    each received bus event WAKES the session by driving a turn there so a
    push to an idle agent is processed immediately (push ≡ Telegram).
    """
    listen = listen_url or os.environ.get(
        "SAC_LISTEN_BASE_URL", "http://127.0.0.1:7878"
    )
    bearer = os.environ.get("SAC_LISTEN_BEARER")
    asyncio.run(_run(name, listen, bearer, turn_url))


__all__ = ["main"]
