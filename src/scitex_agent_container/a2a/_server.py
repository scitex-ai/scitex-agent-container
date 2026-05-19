"""Starlette-based A2A HTTP server (sac-side).

Pure ``a2a-sdk`` 1.0.x dispatch. Every JSON-RPC method (``message/send``,
``message/stream``, ``tasks/get``, ``tasks/cancel``,
``tasks/resubscribe``, ``tasks/pushNotificationConfig/*``) goes through
the SDK's :class:`DefaultRequestHandler` + :func:`create_jsonrpc_routes`.
There is no legacy compat layer — sac speaks current A2A only.

Routes (mirroring the spec):

* ``GET /.well-known/agent-card.json`` — fleet AgentCard (sac dict shape).
* ``GET /agents/`` — JSON list of agents.
* ``GET /agents/<name>/.well-known/agent-card.json`` — per-agent AgentCard
  (sac dict shape; preserves the ``x-scitex-agent-container`` extension).
* ``POST /agents/<name>`` — SDK JSON-RPC dispatcher.

Card projection: see :mod:`._card`. The HTTP ``.well-known`` route serves
the dict projection (which keeps sac-extension fields). The SDK's
``LegacyRequestHandler``-style ``agent/getAuthenticatedExtendedCard`` is
served from the equivalent proto card built by
:func:`._card.project_card_proto`.

Task store is in-memory per the migration doc's recommendation for
sac standalone use. Orochi can later override this with a DB-backed
store.
"""

from __future__ import annotations

import json
import logging
import socket
from pathlib import Path
from typing import Any

from a2a.server.agent_execution import AgentExecutor
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from scitex_agent_container._state.state_db_channel import (
    list_since_id,
    list_undelivered,
    mark_delivered,
    persist_event,
)
from scitex_agent_container.a2a._card import (
    CardSchemaError,
    fleet_card,
    project_card,
    project_card_proto,
    validate_card_v1,
)
from scitex_agent_container.a2a._handlers import HANDLERS
from scitex_agent_container.a2a._inbox_bus import Broker, mint_event

log = logging.getLogger(__name__)


# ---------------------------------------------------------------------
# Per-agent SDK plumbing
# ---------------------------------------------------------------------


class _AgentDispatcher:
    """Bundle the SDK's per-agent components (executor, task store,
    handler, jsonrpc dispatcher route) under a single object.
    """

    def __init__(
        self,
        name: str,
        v3: dict[str, Any],
        executor: AgentExecutor,
        base_url: str,
    ) -> None:
        self.name = name
        self.v3 = v3
        self.executor = executor
        self.task_store = InMemoryTaskStore()
        proto_card = project_card_proto(name, v3, base_url)
        self.request_handler = DefaultRequestHandler(
            agent_executor=executor,
            task_store=self.task_store,
            agent_card=proto_card,
        )
        # SDK JSON-RPC routes — current A2A spec only, no v0.3 compat.
        self.routes: list[Route] = create_jsonrpc_routes(
            request_handler=self.request_handler,
            rpc_url=f"/_sdk/agents/{name}",
        )

    async def snapshot_active_tasks(self) -> list[dict[str, Any]]:
        """Return a JSON-serialisable snapshot of every task currently in
        the per-agent in-memory store.

        Reaches into ``InMemoryTaskStore``'s wrapped impl to enumerate
        tasks regardless of owner (the public ``list()`` API filters by
        the call context's resolved owner, which would scope an
        observability route too narrowly).

        Each entry: ``{"id": str, "state": str, "last_event_at": str|None}``.
        ``state`` is the protobuf enum name (e.g. ``"TASK_STATE_WORKING"``);
        ``last_event_at`` is the task status timestamp in ISO 8601 (or
        ``None`` if the SDK hasn't set one yet).
        """
        from a2a.types import a2a_pb2

        # InMemoryTaskStore -> CopyingTaskStoreAdapter -> _InMemoryTaskStoreImpl
        impl = self.task_store._store._store  # type: ignore[attr-defined]
        out: list[dict[str, Any]] = []
        async with impl.lock:
            for owner_tasks in impl.tasks.values():
                for task_id, task in owner_tasks.items():
                    state_enum = task.status.state if task.HasField("status") else 0
                    state_name = a2a_pb2.TaskState.Name(state_enum)
                    ts = None
                    if task.HasField("status") and task.status.HasField("timestamp"):
                        ts = task.status.timestamp.ToJsonString()
                    out.append(
                        {"id": task_id, "state": state_name, "last_event_at": ts}
                    )
        return out


# ---------------------------------------------------------------------
# Starlette route handlers
# ---------------------------------------------------------------------


class _ServerCtx:
    """Per-server state passed to the Starlette route handlers."""

    def __init__(
        self,
        yamls: dict[str, dict[str, Any]],
        dispatchers: dict[str, _AgentDispatcher],
    ) -> None:
        self.yamls = yamls
        self.dispatchers = dispatchers
        # One in-process broker per sac-listen — fans inbound POSTs out
        # to every SSE subscriber on /agents/<name>/inbox/stream.
        self.inbox = Broker()


def _build_app(ctx: _ServerCtx) -> Starlette:
    def _validated(card: dict[str, Any], label: str) -> Response:
        try:
            validate_card_v1(card)
        except CardSchemaError as exc:
            log.error("%s failed A2A v1 validation: %s", label, exc)
            return JSONResponse(
                {"error": f"{label} failed v1 validation", "detail": str(exc)},
                status_code=500,
            )
        return JSONResponse(card)

    async def get_fleet_card(request: Request) -> Response:
        base = _base_url(request)
        agents = sorted(ctx.yamls.keys())
        return _validated(fleet_card(base, agents), "fleet card")

    async def list_agents(request: Request) -> Response:
        base = _base_url(request)
        agents = sorted(ctx.yamls.keys())
        # Each member entry mirrors the v1 AgentCard shape: binding URLs
        # live under ``supportedInterfaces[]`` (ADR-0004 D11), not a
        # top-level ``url`` (which v1 dropped).
        return JSONResponse(
            {
                "agents": [
                    {
                        "name": n,
                        "supportedInterfaces": [
                            {
                                "url": f"{base}/agents/{n}",
                                "protocolBinding": "HTTP+JSON",
                                "tenant": n,
                                "protocolVersion": "1.0",
                            }
                        ],
                    }
                    for n in agents
                ]
            }
        )

    async def get_agent_card(request: Request) -> Response:
        name = request.path_params["name"]
        v3 = ctx.yamls.get(name)
        if v3 is None:
            return JSONResponse({"error": f"unknown agent: {name}"}, status_code=404)
        base = _base_url(request)
        return _validated(project_card(name, v3, base), f"agent card {name!r}")

    async def post_agent(request: Request) -> Response:
        name = request.path_params["name"]
        if name not in ctx.yamls:
            return JSONResponse({"error": f"unknown agent: {name}"}, status_code=404)

        try:
            body = await request.json()
        except (
            ValueError,
            json.JSONDecodeError,
        ) as exc:  # stx-allow: fallback (reason: malformed JSON tolerated)
            return JSONResponse({"error": f"bad JSON: {exc}"}, status_code=400)

        # Channel fan-out: when the JSON-RPC payload is a message/send,
        # mint a stable event and publish to all inbox subscribers.
        # The SDK still owns the response path; this is purely the
        # push-side bus that `sac mcp channel` (commit 2) consumes.
        await _publish_channel_event(ctx, name, body)

        # Forward to SDK dispatcher (handles message/send, message/stream
        # → SSE, tasks/get, tasks/cancel, tasks/pushNotificationConfig/*,
        # tasks/resubscribe).
        dispatcher = ctx.dispatchers[name]
        sdk_route = dispatcher.routes[0]
        # Restore the body so the SDK route can re-read it (request.json()
        # caches but the SDK reads the raw stream).
        request._body = json.dumps(body).encode("utf-8")  # type: ignore[attr-defined]
        return await sdk_route.endpoint(request)  # type: ignore[no-any-return]

    async def get_inbox_stream(request: Request) -> Response:
        """SSE: one frame per inbound event addressed to /agents/<name>.

        Consumed by `sac mcp channel` (commit 2) inside the agent's
        container — each frame turns into a notifications/claude/channel
        push so Claude sees `<channel source="..." msg_id="..." ...>`
        tags in real time. Plain SSE — non-sac A2A clients work too.

        WI-1 durability semantics (handoff §4 "Durability /
        replay-on-reconnect"):

          * On connect, the handler replays missed events from the
            persistent ``channel_events`` table BEFORE accepting any
            new live events. Replay source:

              - if the client passed ``Last-Event-ID``, replay every
                row with ``id > Last-Event-ID``;
              - otherwise replay every undelivered row (the fresh-
                subscriber case — handoff acceptance "an event POSTed
                with no subscriber is delivered on connect").

          * Each replay frame stamps the SQLite row id onto the SSE
            ``id:`` line so the client can echo it back as
            ``Last-Event-ID`` after a reconnect.

          * After yielding a replay frame the handler marks that row
            ``delivered_at`` so a subsequent fresh-subscriber connect
            does not re-yield it.
        """
        name = request.path_params["name"]
        if name not in ctx.yamls:
            return JSONResponse({"error": f"unknown agent: {name}"}, status_code=404)
        from starlette.responses import StreamingResponse

        last_event_id_raw = request.headers.get("last-event-id")
        last_event_id: int | None = None
        if last_event_id_raw is not None:
            # Loud failure on a malformed header: a corrupt cursor
            # would silently disable replay if we tolerated it.
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

        queue = await ctx.inbox.subscribe(name)

        async def stream():
            try:
                # Send a comment-only frame so HTTP clients see the
                # connection open before any real event arrives.
                yield b": sac-channel ready\n\n"

                # WI-1: replay missed events from state.db. Mark each
                # row delivered as soon as we ship its SSE frame so a
                # second fresh subscriber does not re-receive it.
                if last_event_id is not None:
                    replay = list_since_id(target=name, since_id=last_event_id)
                else:
                    replay = list_undelivered(target=name)
                for entry in replay:
                    if await request.is_disconnected():
                        return
                    row_id = entry["id"]
                    event = entry["event"]
                    data = json.dumps(event, ensure_ascii=False)
                    yield (
                        f"id: {row_id}\nevent: message\ndata: {data}\n\n"
                    ).encode("utf-8")
                    mark_delivered([row_id])

                while True:
                    if await request.is_disconnected():
                        return
                    event = await queue.get()
                    # The publish path stamps the persisted row id onto
                    # the envelope as ``_row_id`` (see
                    # :func:`_publish_channel_event`). We surface it
                    # as the SSE ``id:`` and mark the row delivered.
                    row_id = event.pop("_row_id", None)
                    data = json.dumps(event, ensure_ascii=False)
                    if row_id is not None:
                        yield (
                            f"id: {row_id}\nevent: message\ndata: {data}\n\n"
                        ).encode("utf-8")
                        mark_delivered([int(row_id)])
                    else:
                        # No row id means the event was injected by a
                        # path that did NOT persist (lifecycle event
                        # fan-out, future ACL-reject notice, …).
                        # Deliver it but skip the marker.
                        yield f"event: message\ndata: {data}\n\n".encode(
                            "utf-8"
                        )
            finally:
                await ctx.inbox.unsubscribe(name, queue)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    async def get_active_tasks(request: Request) -> Response:
        """Sac-side observability: list every task currently in the
        per-agent in-memory store.

        Not part of the A2A spec — the spec's ``tasks/get`` requires a
        task ID which only the original caller knows. This route lets a
        co-located observer (e.g. a heartbeat collector) read the live
        state without coupling to any specific consumer.

        Returns ``{"tasks": [{"id", "state", "last_event_at"}, ...]}``.
        """
        name = request.path_params["name"]
        dispatcher = ctx.dispatchers.get(name)
        if dispatcher is None:
            return JSONResponse({"error": f"unknown agent: {name}"}, status_code=404)
        # stx-allow: fallback (reason: observability endpoint must not crash the server; returns 500 with details instead)
        try:
            tasks = await dispatcher.snapshot_active_tasks()
        except Exception as exc:  # pragma: no cover — defense in depth  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
            log.warning("snapshot_active_tasks(%s) failed: %s", name, exc)
            return JSONResponse(
                {"error": "snapshot failed", "detail": str(exc)}, status_code=500
            )
        return JSONResponse({"tasks": tasks})

    # ADR-0004: A2A v1.0 REST binding. /v1/ prefix is prohibited by the
    # v1 spec; sac uses /agents/<name>/... as its multi-agent extension
    # over the single-agent v1 REST endpoints.
    routes = [
        Route("/.well-known/agent-card.json", get_fleet_card, methods=["GET"]),
        Route("/agents/", list_agents, methods=["GET"]),
        Route(
            "/agents/{name}/.well-known/agent-card.json",
            get_agent_card,
            methods=["GET"],
        ),
        # A2A v1 REST binding: POST /message:send. sac prefixes per agent.
        Route("/agents/{name}/message:send", post_agent, methods=["POST"]),
        # sac extensions (not in A2A spec):
        Route(
            "/agents/{name}/inbox/stream",
            get_inbox_stream,
            methods=["GET"],
        ),
        Route(
            "/agents/{name}/_active",
            get_active_tasks,
            methods=["GET"],
        ),
    ]

    return Starlette(routes=routes)


async def _publish_channel_event(
    ctx: _ServerCtx, name: str, body: dict[str, Any]
) -> None:
    """Extract a publishable event from a JSON-RPC ``message/send`` body
    and fan it out to inbox subscribers.

    The body shape sac accepts:
      ``{"jsonrpc": "2.0", "method": "message/send", "params": {...}, ...}``
    The params SHOULD carry the sac-channel meta (``from_agent``,
    ``conversation_id``, ``in_reply_to``, ``priority``, ``requires_reply``)
    in addition to the A2A-standard ``message`` field. Anything missing
    falls back to safe defaults (``from_agent="unknown"``,
    ``priority="normal"``, ``requires_reply=False``).

    Non-``message/send`` payloads (``tasks/get``, ``tasks/cancel``, …)
    do NOT fan out — they're protocol housekeeping, not new turns.

    WI-1 (handoff §4): every fan-out is persisted to ``channel_events``
    so an event POSTed while no subscriber is connected is delivered
    on the next ``inbox/stream`` connect. The persisted row id is
    attached to the envelope as ``_row_id`` so the SSE handler can
    stamp it onto the SSE ``id:`` line (Last-Event-ID cursor).
    """
    if not isinstance(body, dict):
        return
    # Accept both A2A method spellings:
    #   * legacy slash form ``message/send`` (pre-v1)
    #   * v1 gRPC-style ``SendMessage`` / ``SendStreamingMessage``
    if body.get("method") not in (
        "message/send",
        "SendMessage",
        "SendStreamingMessage",
    ):
        return
    params = body.get("params") or {}
    if not isinstance(params, dict):
        return
    # A2A ``message`` carries the actual content as parts[*].text.
    message = params.get("message") or {}
    parts = message.get("parts") if isinstance(message, dict) else None
    text = ""
    if isinstance(parts, list):
        for p in parts:
            if isinstance(p, dict) and isinstance(p.get("text"), str):
                text += p["text"]
    # sac-extension fields live in ``params.metadata`` per A2A v1
    # (the SDK rejects unknown top-level params fields under strict
    # proto validation). We also accept ``message.metadata`` as a
    # secondary location since some clients prefer message-scoped
    # metadata over request-scoped.
    sac_meta: dict[str, Any] = {}
    for src in (params.get("metadata"), message.get("metadata")):
        if isinstance(src, dict):
            sac_meta.update(src)
    event = mint_event(
        name,
        content=text,
        from_agent=sac_meta.get("from_agent"),
        conversation_id=sac_meta.get("conversation_id"),
        in_reply_to=sac_meta.get("in_reply_to"),
        priority=str(sac_meta.get("priority", "normal")),
        requires_reply=bool(sac_meta.get("requires_reply", False)),
    )

    # WI-1 durability: persist BEFORE publishing. If state.db is
    # unreachable we surface the error loudly (handoff §0) rather
    # than silently dropping the event to the bus only.
    row_id = persist_event(target=name, event=event)
    event["_row_id"] = row_id

    await ctx.inbox.publish(name, event)


def _base_url(request: Request) -> str:
    # Use the request URL's scheme + netloc (Host header) for self-references.
    scheme = request.url.scheme or "http"
    netloc = request.headers.get("host") or request.url.netloc or "localhost"
    return f"{scheme}://{netloc}"


# YAML loading + executor selection — extracted to ``_build.py``.
from scitex_agent_container.a2a._build import (  # noqa: E402
    agent_name_from_yaml as _agent_name_from_yaml,
)
from scitex_agent_container.a2a._build import (
    build_executor as _build_executor,
)
from scitex_agent_container.a2a._build import (
    load_yaml as _load_yaml,
)
from scitex_agent_container.a2a._build import (
    select_handler_key as _select_handler_key,
)

# ---------------------------------------------------------------------
# Public serve()
# ---------------------------------------------------------------------


def build_app(
    agent_yamls: list[Path],
    *,
    default_handler: str = "echo",
    base_url: str = "http://localhost",
    listen_port: int | None = None,
) -> Starlette:
    """Build the Starlette app for the given agent YAMLs.

    Exposed as a separate function so tests can drive it without
    binding a TCP socket.

    ``base_url`` is used only to seed the SDK's per-agent AgentCard
    (proto). Live requests build their own self-URL from the Host
    header.

    ``listen_port`` is the port the server WILL listen on at boot
    (which may differ from the yaml's declared ``spec.a2a.port`` when
    the operator overrides via ``--port``). The sac MCP sidecar's
    ``--listen-url`` is built from this so it always talks to the
    SAME server that hosts ``/agents/<name>/inbox/stream``. If
    omitted, falls back to the yaml's ``spec.a2a.port``.
    """
    yamls: dict[str, dict[str, Any]] = {}
    dispatchers: dict[str, _AgentDispatcher] = {}

    for p in agent_yamls:
        v3 = _load_yaml(p)
        name = _agent_name_from_yaml(p, v3)
        yamls[name] = v3
        handler_key = _select_handler_key(v3, default_handler)
        if handler_key not in HANDLERS:
            raise ValueError(
                f"agent {name!r}: unknown a2a handler {handler_key!r}; "
                f"pick one of {sorted(HANDLERS)}"
            )
        # Prefer the runtime listen_port over the yaml's declared port.
        # The sidecar MUST point at this process; using the yaml port
        # breaks when --port differs (e.g., ephemeral ports in tests
        # or port-conflict reallocation).
        a2a_port = listen_port
        if a2a_port is None:
            yaml_port = ((v3.get("spec") or {}).get("a2a") or {}).get("port")
            if isinstance(yaml_port, int):
                a2a_port = yaml_port
        executor = _build_executor(name, handler_key, v3, a2a_port)
        dispatchers[name] = _AgentDispatcher(name, v3, executor, base_url)

    if not yamls:
        raise ValueError("no agent YAMLs supplied")

    ctx = _ServerCtx(yamls=yamls, dispatchers=dispatchers)
    return _build_app(ctx)


def serve(
    agent_yamls: list[Path],
    *,
    host: str = "127.0.0.1",
    port: int = 8888,
    handler: str = "echo",
) -> None:
    """Run the A2A HTTP server in the foreground via uvicorn.

    ``handler`` is the *default* selector — agents whose yaml sets
    ``spec.a2a.handler: <key>`` override it.
    """
    try:
        import uvicorn
    except Exception as exc:  # pragma: no cover  # stx-allow: fallback (reason: optional dependency not installed)
        # Broaden: uvicorn import can fail with non-ImportError if a
        # transitive dep (httptools, websockets) is mis-built. Surface
        # any such failure as an actionable ImportError.
        raise ImportError(
            "uvicorn is required to run 'sac a2a serve'; install with "
            "'pip install uvicorn'."
        ) from exc

    base_url = f"http://{host}:{port}"
    app = build_app(
        agent_yamls,
        default_handler=handler,
        base_url=base_url,
        listen_port=port,
    )

    log.info(
        "sac-a2a (a2a-sdk) listening on http://%s:%d (default handler: %s)",
        host,
        port,
        handler,
    )
    socket.setdefaulttimeout(60)
    # ``ws="none"`` — A2A is HTTP/JSON-RPC + SSE only. Skipping the
    # websockets protocol avoids the uvicorn 0.27 ↔ websockets 15
    # incompatibility (``websockets.legacy`` removed in 14+).
    uvicorn.run(app, host=host, port=port, log_level="info", ws="none")


__all__ = ["build_app", "serve"]
