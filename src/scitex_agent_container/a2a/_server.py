"""Starlette-based A2A HTTP server (sac-side).

This module is a thin shim around the official `a2a-sdk` Starlette
routes (`create_jsonrpc_routes` + `LegacyRequestHandler` with
`enable_v0_3_compat=True`), plus a small native legacy compat layer
that preserves byte-compatibility with sac's pre-SDK
``tasks/send`` / ``tasks/get`` JSON shape.

Routes (mirroring the spec):

* ``GET /.well-known/agent.json`` — fleet AgentCard
* ``GET /v1/agents/`` — JSON list of agents
* ``GET /v1/agents/<name>/.well-known/agent.json`` — per-agent AgentCard
* ``POST /v1/agents/<name>`` — JSON-RPC endpoint:

  - ``tasks/send`` / ``tasks/get`` are handled natively (legacy sac shape).
  - All other methods (``message/send``, ``message/stream``,
    ``tasks/get``, ``tasks/cancel``, ``tasks/resubscribe``, push-notif
    config, etc.) are dispatched through the SDK's JsonRpcDispatcher
    with ``enable_v0_3_compat=True``. ``message/stream`` returns SSE.

Card projection is unchanged — see :mod:`._card`. The legacy compat
path uses sac's existing sync ``HANDLERS``; the SDK path uses the new
``AgentExecutor`` subclasses in :mod:`.executors` (selectable per
agent via yaml ``spec.a2a.handler``).

Task store is in-memory per the migration doc's recommendation for
sac standalone use. Orochi can later override this with a DB-backed
store.
"""

from __future__ import annotations

import json
import logging
import socket
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from google.protobuf.json_format import ParseDict
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from a2a.server.agent_execution import AgentExecutor
from a2a.server.request_handlers import LegacyRequestHandler
from a2a.server.routes import create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore
from a2a.types import AgentCard

from scitex_agent_container.a2a._card import fleet_card, project_card
from scitex_agent_container.a2a._handlers import HANDLERS, HandlerError
from scitex_agent_container.a2a.executors import EXECUTORS, BaseSyncExecutor

log = logging.getLogger(__name__)

# Legacy in-memory store for ``tasks/send`` / ``tasks/get`` byte-compat path.
# (The SDK has its own InMemoryTaskStore; the two are independent —
# legacy clients never see SDK tasks and vice versa.)
_LEGACY_TASKS: dict[str, dict[str, Any]] = {}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


# ---------------------------------------------------------------------
# AgentCard proto adapter
# ---------------------------------------------------------------------


def _proto_agent_card(card_dict: dict[str, Any]) -> AgentCard:
    """Adapt sac's dict AgentCard projection to the SDK's proto AgentCard.

    The SDK's `LegacyRequestHandler` requires a proto `AgentCard` for
    methods like `agent/getAuthenticatedExtendedCard`. Sac's projection
    intentionally produces a dict (the canonical shape, kept stable
    across the ecosystem). We translate the subset of fields the proto
    schema actually accepts; sac-only extension fields like
    ``x-scitex-agent-container`` are dropped (they're served as-is on
    the GET ``.well-known/agent.json`` route, which uses the dict).
    """
    keep_keys = {
        "name",
        "description",
        "version",
        "provider",
        "defaultInputModes",
        "defaultOutputModes",
    }
    minimal: dict[str, Any] = {k: card_dict[k] for k in keep_keys if k in card_dict}

    # Capabilities — only the proto-supported subset. Force ``streaming:
    # true`` regardless of what the dict card says; sac executors enqueue
    # task-update events to support `message/stream`. (The dict card
    # served at GET /.well-known/agent.json reflects the historical
    # value — preserved for byte-compat.)
    caps_in = card_dict.get("capabilities") or {}
    minimal["capabilities"] = {
        "streaming": True,
        "pushNotifications": bool(caps_in.get("pushNotifications", False)),
    }

    # Skills — strip unknown fields.
    skills = []
    skill_keep = {"id", "name", "description", "tags"}
    for skill in card_dict.get("skills", []) or []:
        skills.append({k: skill[k] for k in skill_keep if k in skill})
    minimal["skills"] = skills

    # Provider — strip unknown fields.
    prov = card_dict.get("provider") or {}
    minimal["provider"] = {
        k: prov[k] for k in ("organization", "url") if k in prov
    }

    return ParseDict(minimal, AgentCard())


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
        card_dict = project_card(name, v3, base_url)
        proto_card = _proto_agent_card(card_dict)
        self.request_handler = LegacyRequestHandler(
            agent_executor=executor,
            task_store=self.task_store,
            agent_card=proto_card,
        )
        # Build the SDK's JSON-RPC routes (with v0.3 compat enabled so
        # external clients can use `message/send`, `message/stream`,
        # `tasks/get`, `tasks/cancel`).
        self.routes: list[Route] = create_jsonrpc_routes(
            request_handler=self.request_handler,
            rpc_url=f"/_sdk/v1/agents/{name}",
            enable_v0_3_compat=True,
        )


# ---------------------------------------------------------------------
# Legacy `tasks/send` / `tasks/get` byte-compat handler
# ---------------------------------------------------------------------


def _legacy_handle(name: str, handler_key: str, body: dict[str, Any]) -> dict[str, Any]:
    """Implement the pre-SDK ``tasks/send`` / ``tasks/get`` JSON-RPC shape.

    Returns the JSON-RPC envelope dict (``{"jsonrpc": "2.0", "id": ...,
    "result": ...}``) so callers can wrap it in a JSONResponse.
    """
    rpc_id = body.get("id")
    method = body.get("method")
    params = body.get("params") or {}

    if method == "tasks/get":
        tid = params.get("id")
        if not tid or tid not in _LEGACY_TASKS:
            return {
                "jsonrpc": "2.0",
                "id": rpc_id,
                "error": {"code": -32000, "message": f"task not found: {tid}"},
            }
        return {"jsonrpc": "2.0", "id": rpc_id, "result": _LEGACY_TASKS[tid]}

    if method != "tasks/send":  # pragma: no cover — caller should filter.
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {"code": -32601, "message": f"method not found: {method}"},
        }

    msg = params.get("message", {}) or {}
    parts = msg.get("parts", []) or []
    user_text = next(
        (p.get("text", "") for p in parts if p.get("type") == "text"),
        "",
    )

    handler_fn = HANDLERS.get(handler_key)
    if handler_fn is None:  # pragma: no cover — selection layer guards.
        return {
            "jsonrpc": "2.0",
            "id": rpc_id,
            "error": {
                "code": -32603,
                "message": f"unknown handler: {handler_key!r}",
            },
        }

    err_msg = None
    try:
        reply = handler_fn(name, user_text)
        state = "completed"
    except HandlerError as exc:
        reply = str(exc)
        state = "failed"
        err_msg = {"text": str(exc)}
    except Exception as exc:  # noqa: BLE001
        log.exception("legacy handler crashed for %s", name)
        reply = f"handler crashed: {exc}"
        state = "failed"
        err_msg = {"text": str(exc)}

    task = {
        "id": params.get("id") or f"task-{uuid.uuid4().hex[:12]}",
        "sessionId": params.get("sessionId"),
        "status": {"state": state, "message": err_msg, "timestamp": _now_iso()},
        "history": [
            msg,
            {"role": "agent", "parts": [{"type": "text", "text": reply}]},
        ],
        "artifacts": [],
        "metadata": {
            "x-scitex-agent-container": {
                "agent": name,
                "served_by": "sac-a2a",
                "generated_at": _now_iso(),
            }
        },
    }
    _LEGACY_TASKS[task["id"]] = task
    return {"jsonrpc": "2.0", "id": rpc_id, "result": task}


# ---------------------------------------------------------------------
# Starlette route handlers
# ---------------------------------------------------------------------


class _ServerCtx:
    """Per-server state passed to the Starlette route handlers."""

    def __init__(
        self,
        yamls: dict[str, dict[str, Any]],
        handler_keys: dict[str, str],
        dispatchers: dict[str, _AgentDispatcher],
    ) -> None:
        self.yamls = yamls
        self.handler_keys = handler_keys
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
            body = await request.json()
        except (ValueError, json.JSONDecodeError) as exc:
            return JSONResponse({"error": f"bad JSON: {exc}"}, status_code=400)

        method = body.get("method")
        if method in ("tasks/send", "tasks/get"):
            handler_key = ctx.handler_keys[name]
            return JSONResponse(_legacy_handle(name, handler_key, body))

        # Forward to SDK dispatcher (handles message/send,
        # message/stream → SSE, tasks/get, tasks/cancel,
        # tasks/pushNotificationConfig/*, tasks/resubscribe).
        dispatcher = ctx.dispatchers[name]
        # The SDK dispatcher only exposes one route — POST. Reuse it
        # by calling its endpoint function directly.
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
    handler_keys: dict[str, str] = {}
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
        handler_keys[name] = handler_key
        executor = _build_executor(name, handler_key)
        dispatchers[name] = _AgentDispatcher(name, v3, executor, base_url)

    if not yamls:
        raise ValueError("no agent YAMLs supplied")

    ctx = _ServerCtx(yamls=yamls, handler_keys=handler_keys, dispatchers=dispatchers)
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
