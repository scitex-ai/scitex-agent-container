"""Starlette app factory for ``sac listen``.

Hosts the ``/v1/sac/...`` namespace as designed in SAC_OROCHI_SCOPES.md.
v1 endpoints:

    GET    /v1/sac/health
    GET    /v1/sac/agents
    GET    /v1/sac/agents/<name>/status
    POST   /v1/sac/agents/<name>/send
    DELETE /v1/sac/agents/<name>

Tail (SSE), POST /v1/sac/agents (start from spec body), and the A2A
namespace under /v1/sac/a2a/ are reserved for steps 3-4 of §6.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .._runners._session_state import read_session_id, state_dir_for
from .._state.registry import Registry
from ..config import load_config
from ..config._resolve import resolve_config
from .auth import BearerAuthMiddleware


def _find_claude_binary() -> str:
    """Same resolver as send_cmds — bundled SDK copy first, then PATH."""
    bundled = (
        "/opt/venv-sac/lib/python3.12/site-packages/claude_agent_sdk/_bundled/claude"
    )
    if os.path.isfile(bundled) and os.access(bundled, os.X_OK):
        return bundled
    found = shutil.which("claude")
    if not found:
        raise RuntimeError("claude binary not found")
    return found


# --- Handlers --------------------------------------------------------------


async def health(_request: Request) -> JSONResponse:
    return JSONResponse({"ok": True, "service": "sac-listen", "v": 1})


async def list_agents(_request: Request) -> JSONResponse:
    """List agents the local Registry knows about."""
    try:
        reg = Registry()
        rows = reg.list_all()
    except Exception as exc:  # stx-allow: fallback (reason: surface a JSON error to the caller rather than ASGI 500 stack)
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse({"agents": rows})


async def agent_status(request: Request) -> JSONResponse:
    name = request.path_params["name"]
    try:
        spec_path = resolve_config(name)
        cfg = load_config(spec_path)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    sd = state_dir_for(name)
    sid = read_session_id(sd)
    return JSONResponse(
        {
            "name": name,
            "spec_path": str(spec_path),
            "workdir": cfg.expanded_workdir,
            "session_id": sid,
            "state_dir": str(sd),
        }
    )


async def agent_send(request: Request) -> Response:
    """POST /v1/sac/agents/<name>/send.

    Body discriminator:
        {"type":"prompt","prompt":"...","options":{"model":...,"max_turns":...}}
        {"type":"key","key":"ESC"}   # not yet implemented

    For v1, returns the buffered stdout (no SSE streaming). SSE comes in
    step 3 alongside long-lived mode.
    """
    name = request.path_params["name"]
    try:
        body = await request.json()
    except Exception:  # stx-allow: fallback (reason: malformed JSON → 400 with explanation rather than ASGI 500)
        return JSONResponse({"error": "body must be JSON"}, status_code=400)

    type_ = body.get("type", "prompt")
    if type_ == "key":
        return JSONResponse(
            {
                "error": (
                    "type=key not yet implemented (requires long-lived agent + "
                    "tty bridge; see SAC_OROCHI_SCOPES.md §6 step 3)"
                )
            },
            status_code=501,
        )
    if type_ != "prompt":
        return JSONResponse(
            {"error": f"unknown type {type_!r}; expected 'prompt' or 'key'"},
            status_code=400,
        )

    prompt = body.get("prompt")
    if not isinstance(prompt, str) or not prompt:
        return JSONResponse(
            {"error": "missing or empty 'prompt' string"}, status_code=400
        )

    try:
        spec_path = resolve_config(name)
        cfg = load_config(spec_path)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=404)
    sd = state_dir_for(name)
    sid = read_session_id(sd)
    if not sid:
        return JSONResponse(
            {"error": f"no session_id recorded for {name!r}"}, status_code=409
        )

    try:
        claude_bin = _find_claude_binary()
    except RuntimeError as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)

    options = body.get("options") or {}
    argv = [claude_bin, "--resume", sid, "-p", prompt]
    if "model" in options:
        argv += ["--model", str(options["model"])]
    if "max_turns" in options:
        argv += ["--max-turns", str(options["max_turns"])]

    workdir = cfg.expanded_workdir or os.getcwd()
    # Run blocking subprocess in a worker thread so the event loop stays free.
    proc = await asyncio.to_thread(
        subprocess.run,
        argv,
        cwd=workdir,
        capture_output=True,
        text=True,
        check=False,
    )
    return JSONResponse(
        {
            "name": name,
            "session_id": sid,
            "returncode": proc.returncode,
            "stdout": proc.stdout,
            "stderr": proc.stderr,
        }
    )


async def agent_delete(request: Request) -> JSONResponse:
    """DELETE /v1/sac/agents/<name> — stop the agent.

    v1: reads pid from state_dir and SIGTERMs it. Apptainer wrapper
    cleanup is delegated to the existing lifecycle.stop path in step 3.
    """
    name = request.path_params["name"]
    sd = state_dir_for(name)
    pid_file = sd / "pid"
    if not pid_file.is_file():
        return JSONResponse({"error": "no pid file"}, status_code=404)
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, 15)  # SIGTERM
    except (OSError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse({"name": name, "stopped": True, "pid": pid})


# --- App factory -----------------------------------------------------------


def create_app(*, token: str) -> Starlette:
    """Build the Starlette app with bearer auth + /v1/sac/ routes."""
    routes = [
        Route("/v1/sac/health", health, methods=["GET"]),
        Route("/v1/sac/agents", list_agents, methods=["GET"]),
        Route("/v1/sac/agents/{name}/status", agent_status, methods=["GET"]),
        Route("/v1/sac/agents/{name}/send", agent_send, methods=["POST"]),
        Route("/v1/sac/agents/{name}", agent_delete, methods=["DELETE"]),
    ]
    app = Starlette(routes=routes)
    app.add_middleware(BearerAuthMiddleware, token=token)
    return app
