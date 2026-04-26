"""Starlette-based A2A HTTP server (sac-side).

Pure ``a2a-sdk`` 1.0.x dispatch. Every JSON-RPC method (``message/send``,
``message/stream``, ``tasks/get``, ``tasks/cancel``,
``tasks/resubscribe``, ``tasks/pushNotificationConfig/*``) goes through
the SDK's :class:`DefaultRequestHandler` + :func:`create_jsonrpc_routes`.
There is no legacy compat layer — sac speaks current A2A only.

Routes (mirroring the spec):

* ``GET /.well-known/agent.json`` — fleet AgentCard (sac dict shape).
* ``GET /v1/agents/`` — JSON list of agents.
* ``GET /v1/agents/<name>/.well-known/agent.json`` — per-agent AgentCard
  (sac dict shape; preserves the ``x-scitex-agent-container`` extension).
* ``POST /v1/agents/<name>`` — SDK JSON-RPC dispatcher.

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

import yaml
from a2a.server.agent_execution import AgentExecutor
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from scitex_agent_container.a2a._card import (
    fleet_card,
    project_card,
    project_card_proto,
)
from scitex_agent_container.a2a._handlers import HANDLERS
from scitex_agent_container.a2a.executors import EXECUTORS, BaseSyncExecutor

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
            rpc_url=f"/_sdk/v1/agents/{name}",
        )


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


def _build_app(ctx: _ServerCtx) -> Starlette:
    async def get_fleet_card(request: Request) -> Response:
        base = _base_url(request)
        agents = sorted(ctx.yamls.keys())
        return JSONResponse(fleet_card(base, agents))

    async def list_agents(request: Request) -> Response:
        base = _base_url(request)
        agents = sorted(ctx.yamls.keys())
        return JSONResponse(
            {"agents": [{"name": n, "url": f"{base}/v1/agents/{n}"} for n in agents]}
        )

    async def get_agent_card(request: Request) -> Response:
        name = request.path_params["name"]
        v3 = ctx.yamls.get(name)
        if v3 is None:
            return JSONResponse({"error": f"unknown agent: {name}"}, status_code=404)
        base = _base_url(request)
        return JSONResponse(project_card(name, v3, base))

    async def post_agent(request: Request) -> Response:
        name = request.path_params["name"]
        if name not in ctx.yamls:
            return JSONResponse({"error": f"unknown agent: {name}"}, status_code=404)

        try:
            await request.json()
        except (ValueError, json.JSONDecodeError) as exc:
            return JSONResponse({"error": f"bad JSON: {exc}"}, status_code=400)

        # Forward to SDK dispatcher (handles message/send, message/stream
        # → SSE, tasks/get, tasks/cancel, tasks/pushNotificationConfig/*,
        # tasks/resubscribe).
        dispatcher = ctx.dispatchers[name]
        sdk_route = dispatcher.routes[0]
        return await sdk_route.endpoint(request)  # type: ignore[no-any-return]

    routes = [
        Route("/.well-known/agent.json", get_fleet_card, methods=["GET"]),
        Route("/v1/agents/", list_agents, methods=["GET"]),
        Route(
            "/v1/agents/{name}/.well-known/agent.json",
            get_agent_card,
            methods=["GET"],
        ),
        Route("/v1/agents/{name}", post_agent, methods=["POST"]),
        Route("/v1/agents/{name}/", post_agent, methods=["POST"]),
    ]

    return Starlette(routes=routes)


def _base_url(request: Request) -> str:
    # Use the request URL's scheme + netloc (Host header) for self-references.
    scheme = request.url.scheme or "http"
    netloc = request.headers.get("host") or request.url.netloc or "localhost"
    return f"{scheme}://{netloc}"


# ---------------------------------------------------------------------
# YAML loading + executor selection
# ---------------------------------------------------------------------


def _load_yaml(path: Path) -> dict[str, Any]:
    return yaml.safe_load(path.read_text()) or {}


def _agent_name_from_yaml(path: Path, v3: dict[str, Any]) -> str:
    meta = v3.get("metadata") or {}
    name = meta.get("name")
    if isinstance(name, str) and name.strip():
        return name.strip()
    return path.stem


def _select_handler_key(v3: dict[str, Any], default: str) -> str:
    """Read ``spec.a2a.handler`` from v3 yaml (falling back to ``default``)."""
    a2a_block = (v3.get("spec") or {}).get("a2a") or {}
    key = a2a_block.get("handler")
    if isinstance(key, str) and key.strip():
        return key.strip()
    return default


def _build_executor(name: str, handler_key: str) -> BaseSyncExecutor:
    cls = EXECUTORS.get(handler_key)
    if cls is None:
        raise ValueError(
            f"unknown a2a handler {handler_key!r}; pick one of {sorted(EXECUTORS)}"
        )
    return cls(agent_name=name)


# ---------------------------------------------------------------------
# Public serve()
# ---------------------------------------------------------------------


def build_app(
    agent_yamls: list[Path],
    *,
    default_handler: str = "echo",
    base_url: str = "http://localhost",
) -> Starlette:
    """Build the Starlette app for the given agent YAMLs.

    Exposed as a separate function so tests can drive it without
    binding a TCP socket.

    ``base_url`` is used only to seed the SDK's per-agent AgentCard
    (proto). Live requests build their own self-URL from the Host
    header.
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
        executor = _build_executor(name, handler_key)
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
    except ImportError as exc:  # pragma: no cover
        raise ImportError(
            "uvicorn is required to run 'sac a2a serve'; install with "
            "'pip install uvicorn'."
        ) from exc

    base_url = f"http://{host}:{port}"
    app = build_app(agent_yamls, default_handler=handler, base_url=base_url)

    log.info(
        "sac-a2a (a2a-sdk) listening on http://%s:%d (default handler: %s)",
        host,
        port,
        handler,
    )
    socket.setdefaulttimeout(60)
    uvicorn.run(app, host=host, port=port, log_level="info")


__all__ = ["build_app", "serve"]
