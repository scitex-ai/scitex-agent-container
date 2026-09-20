"""Authoritative fleet inventory route owned by the bare-host listener."""

from __future__ import annotations

import asyncio
import socket
from typing import Any

from pydantic import ValidationError
from starlette.requests import Request
from starlette.responses import JSONResponse

from .. import __version__
from .._inventory import FLEET_INVENTORY_SCHEMA, FleetInventoryResponse


def _invoke_inventory(argv: list[str]) -> dict[str, Any]:
    from .._mcp._tools._helpers import invoke_cli_json

    return invoke_cli_json(argv)


def _argv(request: Request) -> list[str]:
    argv = ["agents", "list", "--json"]
    for key, flag in (("capability", "--capability"), ("machine", "--machine")):
        value = (request.query_params.get(key) or "").strip()
        if value:
            argv += [flag, value]
    return argv


async def fleet_inventory(request: Request) -> JSONResponse:
    """Return the host CLI's fleet view with versioned authority provenance.

    The CLI call runs off the event loop because fleet fan-out can wait on SSH.
    Any command, JSON, or schema failure is an HTTP 502: callers must never
    substitute their container-local registry for a missing authority.
    """
    try:
        result = await asyncio.to_thread(_invoke_inventory, _argv(request))
    except Exception as exc:  # stx-allow: fallback (reason: the subprocess-like CLI boundary can raise arbitrary command/runtime exceptions; convert every one to the endpoint's explicit 502 contract instead of leaking an ASGI traceback)
        return JSONResponse(
            {
                "error": "authoritative fleet inventory command raised",
                "detail": f"{type(exc).__name__}: {exc}",
            },
            status_code=502,
        )
    if result.get("exit_code") != 0:
        return JSONResponse(
            {
                "error": "authoritative fleet inventory command failed",
                "exit_code": result.get("exit_code"),
                "stderr": result.get("stderr", ""),
            },
            status_code=502,
        )
    data = result.get("data")
    if not isinstance(data, dict):
        return JSONResponse(
            {
                "error": "authoritative fleet inventory returned malformed JSON",
                "stdout": result.get("stdout", ""),
            },
            status_code=502,
        )
    candidate = {
        "schema_version": FLEET_INVENTORY_SCHEMA,
        "inventory_kind": "fleet",
        "authority": {
            "kind": "sac-host-listener",
            "host": socket.gethostname(),
            "sac_version": __version__,
        },
        "semantics": {
            "agent_status_axis": "definition-and-process-liveness",
            # ``running`` proves process liveness, not whether the model is in
            # a turn.  The fleet CLI has no authoritative cross-host activity
            # signal, so saying unknown is safer than guessing from heartbeat
            # age or transcript movement.
            "activity_axis": "not-reported-do-not-infer",
        },
        "agents": data.get("agents"),
        "hosts": data.get("hosts"),
    }
    try:
        payload = FleetInventoryResponse.model_validate(candidate)
    except ValidationError as exc:
        return JSONResponse(
            {
                "error": "authoritative fleet inventory violated its schema",
                "schema_version": FLEET_INVENTORY_SCHEMA,
                "detail": exc.errors(include_url=False),
            },
            status_code=502,
        )
    return JSONResponse(payload.wire_dict())


__all__ = ["fleet_inventory"]
