"""HTTP inbound-turn endpoint for the claude-session runner.

PR2 of the inbound-turn channel. When the runner is invoked with
``--a2a-port N``, ``run()`` spawns ``serve_inbound`` as an asyncio task.
The endpoint accepts POST requests with a JSON body and enqueues a
``TurnEnvelope`` on the shared inbox; the conversation task drains it,
calls ``client.query()``, and resolves the future with the assistant's
reply. The HTTP handler awaits the future and returns the reply as JSON.

Wire format:

    POST /v1/turn
    Content-Type: application/json
    {"text": "your message", "exit_after": false}

The request field is ``text`` — that's the sac sidecar shape. Callers
sending ``{"prompt": "..."}`` (e.g. some early lead helpers) get a
``400`` with ``"missing or empty 'text' field"`` so the schema mismatch
is loud, not a hang. Use ``text`` end-to-end.

    200 OK
    {
      "text": "<final assistant text>",
      "session_id": "<sdk session id or null>",
      "exit_after": false,
      "metadata": {"timeout_s": 120.0}
    }

    504 Gateway Timeout
    {
      "error": "turn exceeded <N>s timeout",
      "timeout_s": <N>,
      "session_id": "<sdk session id if known or null>"
    }

A2A v1.0 ``/agents/<name>/send`` / ``.../turn`` use the same body shape
via :func:`post_turn_named`.

Per-turn timeout: ``turn_timeout_s`` is the BOUNDED wait the handler
imposes on the SDK envelope's future. Default is 120 s, overridable
process-wide via ``SAC_A2A_TURN_TIMEOUT_S``. When the SDK keeps
yielding past the deadline the handler returns a 504 (loud failure)
rather than hanging the HTTP request indefinitely — the long-form
session.jsonl trail still records what the SDK produced, but the
HTTP caller is no longer left waiting on a never-arriving response.

Bound to ``127.0.0.1`` by default — operators who want LAN exposure can
set ``host`` explicitly via the runner's ``--a2a-host`` flag (added
alongside ``--a2a-port``).
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ._session_inbox import Envelope

logger = logging.getLogger(__name__)


# Default bounded wait for the SDK to drain one turn. Sized to outlast
# normal multi-tool turns (file reads, MCP calls) but bounded enough
# that a wedged turn surfaces as a 504 within a couple of minutes
# rather than the 60 s curl default (or worse, a 10-minute ssh hang).
DEFAULT_TURN_TIMEOUT_S: float = 120.0

# Env override used by both ``serve_inbound`` and operators who want
# to tune the cap without redeploying the runner. Parsed once per call,
# not at import time, so a test can poke it before spinning the sidecar.
TURN_TIMEOUT_ENV_VAR: str = "SAC_A2A_TURN_TIMEOUT_S"


def _resolve_turn_timeout(explicit: float | None) -> float:
    """Pick the effective turn timeout (explicit > env > default).

    Loud on a malformed env value — a silent default-substitution here
    would let an operator think they'd configured a longer cap when in
    fact the typo got swallowed. STX hard-rule "no silent fallbacks".
    """
    if explicit is not None:
        return float(explicit)
    raw = os.environ.get(TURN_TIMEOUT_ENV_VAR, "").strip()
    if not raw:
        return DEFAULT_TURN_TIMEOUT_S
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(
            f"{TURN_TIMEOUT_ENV_VAR}={raw!r} is not a valid float seconds value"
        ) from exc


async def serve_inbound(
    inbox: "asyncio.Queue[Envelope]",
    *,
    host: str,
    port: int,
    stop: asyncio.Event,
    turn_timeout_s: float | None = None,
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

    # Resolve the effective bounded-wait once per serve_inbound() call.
    # Per-request override via env would be racy and rarely useful — the
    # operator either trusts the agent for long turns or doesn't.
    effective_turn_timeout_s = _resolve_turn_timeout(turn_timeout_s)

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
            reply = await asyncio.wait_for(
                env.response, timeout=effective_turn_timeout_s
            )
        except asyncio.TimeoutError:
            # 504: BOUNDED wait elapsed before the SDK finished draining.
            # Include the timeout value and (if known) the session_id so
            # the caller can either retry, raise the cap, or follow up via
            # session.jsonl. ``env.session_id`` is only populated if the
            # SDK already streamed a ResultMessage before we tripped the
            # timeout, which on a true hang will normally be ``None``.
            return JSONResponse(
                {
                    "error": (f"turn exceeded {effective_turn_timeout_s:.0f}s timeout"),
                    "timeout_s": effective_turn_timeout_s,
                    "session_id": env.session_id,
                },
                status_code=504,
            )
        except Exception as exc:  # stx-allow: fallback (reason: surface SDK errors as 502 instead of crashing the server)
            logger.warning("inbound turn failed: %s", exc)
            return JSONResponse({"error": f"turn failed: {exc}"}, status_code=502)
        # A2A v1.0 response: ``text`` is the single canonical field.
        # ``session_id`` lets the caller resume / correlate;
        # ``metadata.timeout_s`` records the cap that was in force
        # when this turn ran.
        return JSONResponse(
            {
                "text": reply,
                "session_id": env.session_id,
                "exit_after": exit_after,
                "metadata": {"timeout_s": effective_turn_timeout_s},
            }
        )

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
    # plane or POSTs directly to the agent's port. The path ``{name}``
    # segment is ignored because the port already identifies the agent.
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

    # Per-agent sidecar routes (ADR-0004 — A2A v1.0). The legacy
    # ``/v1/a2a/...`` protocol-compat mirror was removed wholesale.
    routes = [
        # Bare shortcut — port already identifies the agent.
        Route("/v1/turn", post_turn, methods=["POST"]),
        # Canonical sac namespace.
        Route("/agents/{name}/send", post_turn_named, methods=["POST"]),
        Route("/agents/{name}/turn", post_turn_named, methods=["POST"]),
        Route("/agents/{name}/card", get_agent_card, methods=["GET"]),
        # Discovery (A2A v1 convention).
        Route("/.well-known/agent-card.json", get_agent_card, methods=["GET"]),
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
