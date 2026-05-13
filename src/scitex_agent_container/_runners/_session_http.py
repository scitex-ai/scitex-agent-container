"""HTTP inbound-turn endpoint for the claude-session runner.

PR2 of the inbound-turn channel. When the runner is invoked with
``--a2a-port N``, ``run()`` spawns ``serve_inbound`` as an asyncio task.
The endpoint accepts POST requests with a JSON body and enqueues a
``TurnEnvelope`` on the shared inbox; the conversation task drains it,
calls ``client.query()``, and resolves the future with the assistant's
reply. The HTTP handler awaits the future and returns the reply as JSON.

Wire format (PR2-min — A2A JSON-RPC compat lands later):

    POST /v1/turn
    Content-Type: application/json
    {"text": "your message", "exit_after": false}

    200 OK
    {"reply": "...", "exit_after": false}

Bound to ``127.0.0.1`` by default — operators who want LAN exposure can
set ``host`` explicitly via the runner's ``--a2a-host`` flag (added
alongside ``--a2a-port``).
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ._session_inbox import Envelope

logger = logging.getLogger(__name__)


async def serve_inbound(
    inbox: "asyncio.Queue[Envelope]",
    *,
    host: str,
    port: int,
    stop: asyncio.Event,
    turn_timeout_s: float = 600.0,
    agent_name: str = "",
    spec_yaml_path: str = "",
) -> None:
    """Run an HTTP server that feeds turn envelopes into ``inbox``.

    Returns when ``stop`` is set; cancels the uvicorn server task.
    """
    try:
        import uvicorn
        from starlette.applications import Starlette
        from starlette.requests import Request
        from starlette.responses import JSONResponse
        from starlette.routing import Route
    except ImportError as exc:  # stx-allow: fallback (reason: optional dep — runner must keep heart-beating even if HTTP deps missing)
        logger.error("inbound HTTP requires starlette+uvicorn: %s", exc)
        return

    from ._session_inbox import TurnEnvelope

    async def post_turn(request: Request) -> JSONResponse:
        try:
            body = await request.json()
        except ValueError as exc:  # stx-allow: fallback (reason: malformed JSON tolerated; surfaced as 400)
            return JSONResponse({"error": f"bad JSON: {exc}"}, status_code=400)
        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str) or not text.strip():
            return JSONResponse(
                {"error": "missing or empty 'text' field"}, status_code=400
            )
        exit_after = bool(body.get("exit_after", False))

        loop = asyncio.get_running_loop()
        env = TurnEnvelope(
            text=text, response=loop.create_future(), exit_after=exit_after
        )
        await inbox.put(env)
        try:
            reply = await asyncio.wait_for(env.response, timeout=turn_timeout_s)
        except asyncio.TimeoutError:
            return JSONResponse(
                {"error": f"turn timeout after {turn_timeout_s:.0f}s"}, status_code=504
            )
        except Exception as exc:  # stx-allow: fallback (reason: surface SDK errors as 502 instead of crashing the server)
            logger.warning("inbound turn failed: %s", exc)
            return JSONResponse({"error": f"turn failed: {exc}"}, status_code=502)
        return JSONResponse({"reply": reply, "exit_after": exit_after})

    async def get_health(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    async def get_agent_card(request: Request) -> JSONResponse:
        if not agent_name or not spec_yaml_path:
            return JSONResponse(
                {"error": "agent card unavailable (no --a2a-card-yaml)"},
                status_code=404,
            )
        try:
            import yaml as _yaml

            from ..a2a._card import project_card
        except ImportError as exc:
            return JSONResponse({"error": f"card deps missing: {exc}"}, status_code=500)
        try:
            v3 = _yaml.safe_load(open(spec_yaml_path).read()) or {}
        except OSError as exc:
            return JSONResponse(
                {"error": f"cannot read {spec_yaml_path}: {exc}"}, status_code=500
            )
        # Per Layer-5 of auto-port-allocation: prefer the host-stable
        # ``SAC_LISTEN_BASE_URL`` (injected by the apptainer runtime)
        # over ``request.base_url``. Under auto-allocation the runner's
        # own port changes every restart, so anything cached against
        # ``request.base_url`` would dangle the moment the agent is
        # restarted. Falling back to ``request.base_url`` keeps direct
        # ``curl http://127.0.0.1:<runner-port>/.well-known/agent-card.json``
        # working in non-apptainer test harnesses.
        import os as _os

        env_base = _os.environ.get("SAC_LISTEN_BASE_URL", "").strip()
        base_url = (
            env_base.rstrip("/") if env_base else str(request.base_url).rstrip("/")
        )
        return JSONResponse(project_card(agent_name, v3, base_url))

    # Per-agent sidecar routes — mirror sac listen's path shape so the
    # same URL works whether the client routes through the host control
    # plane or POSTs directly to the agent's port. Both namespaces
    # (canonical `/v1/sac/agents/...` and A2A-compat `/v1/a2a/agents/...`)
    # point at the same handlers; the path ``{name}`` segment is ignored
    # because the port already identifies the agent.
    async def post_turn_named(request: Request) -> JSONResponse:
        # `{name}` is informational — port routing already pinned us
        # to one agent. Honor it as a sanity check; mismatch is 404.
        path_name = request.path_params.get("name", "")
        if agent_name and path_name and path_name != agent_name:
            return JSONResponse(
                {"error": f"this port serves agent '{agent_name}', not '{path_name}'"},
                status_code=404,
            )
        return await post_turn(request)

    # Per-agent sidecar routes — mirror sac listen's path shape so the
    # same URL works whether the client routes through the host control
    # plane or POSTs directly to the agent's port. Per the original
    # sac/orochi contract: ``/v1/sac/...`` (canonical) and
    # ``/v1/a2a/...`` (A2A-protocol-compat mirror) — symmetric, both
    # serving identical data.
    routes = [
        # Bare shortcut — port already identifies the agent.
        Route("/v1/turn", post_turn, methods=["POST"]),
        # Canonical sac namespace.
        Route("/v1/sac/agents/{name}/send", post_turn_named, methods=["POST"]),
        Route("/v1/sac/agents/{name}/turn", post_turn_named, methods=["POST"]),
        Route("/v1/sac/agents/{name}/card", get_agent_card, methods=["GET"]),
        # A2A-protocol-compat mirror (same handlers).
        Route("/v1/a2a/agents/{name}/send", post_turn_named, methods=["POST"]),
        Route("/v1/a2a/agents/{name}/turn", post_turn_named, methods=["POST"]),
        Route("/v1/a2a/agents/{name}/card", get_agent_card, methods=["GET"]),
        # Discovery (A2A convention — both filenames are widely tried).
        Route("/.well-known/agent-card.json", get_agent_card, methods=["GET"]),
        Route("/.well-known/agent.json", get_agent_card, methods=["GET"]),
        Route("/health", get_health, methods=["GET"]),
    ]
    app = Starlette(routes=routes)

    config = uvicorn.Config(
        app,
        host=host,
        port=port,
        log_level="warning",
        ws="none",
        lifespan="off",
    )
    server = uvicorn.Server(config)
    serve_task = asyncio.create_task(server.serve())
    try:
        await stop.wait()
    finally:
        server.should_exit = True
        try:
            await asyncio.wait_for(serve_task, timeout=5.0)
        except (
            asyncio.TimeoutError,
            asyncio.CancelledError,
        ):  # stx-allow: fallback (reason: must exit even if uvicorn hangs on shutdown)
            serve_task.cancel()
            try:
                await serve_task
            except (
                asyncio.CancelledError,
                Exception,
            ):  # stx-allow: fallback (reason: defensive cleanup)
                pass


__all__ = ["serve_inbound"]
