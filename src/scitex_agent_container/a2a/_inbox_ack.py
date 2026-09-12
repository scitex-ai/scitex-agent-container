"""Explicit acknowledgement for durable SAC inbox consumers."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .._lifecycle._off_loop import run_blocking
from .._state.state_store_channel import mark_delivered


async def inbox_ack(
    request: Request,
    *,
    known_names: Collection[str] | None = None,
    mark: Callable[..., Any] = mark_delivered,
    run: Callable[..., Awaitable[Any]] = run_blocking,
) -> Response:
    """Mark one event delivered after a consumer has accepted it."""
    name = request.path_params["name"]
    if known_names is not None and name not in known_names:
        return JSONResponse({"error": f"unknown agent: {name}"}, status_code=404)
    try:
        body = await request.json()
        event_id = int(body["id"])
        if event_id <= 0:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        return JSONResponse(
            {"error": "request body must contain a positive integer id"},
            status_code=400,
        )
    await run(mark, [event_id], target=name)
    return JSONResponse({"acknowledged": event_id})


def inbox_ack_route(path: str, *, known_names: Collection[str] | None = None) -> Route:
    """Build the shared POST route without duplicating server wrappers."""

    async def endpoint(request: Request) -> Response:
        return await inbox_ack(request, known_names=known_names)

    return Route(path, endpoint, methods=["POST"])


__all__ = ["inbox_ack", "inbox_ack_route"]
