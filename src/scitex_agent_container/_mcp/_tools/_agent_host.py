"""MCP start requests must reach the host before container-local preflight."""

from __future__ import annotations

from typing import Any


def start_on_host(
    name: str, *, foreground: bool, session: str | None
) -> dict[str, Any]:
    """The existing spawn client retains bearer, caller and host ACL checks."""
    from ..._lifecycle._spawn_client import SpawnRequestError, request_spawn

    try:
        result = request_spawn(
            name, foreground=foreground, assume_yes=True, session=session
        )
    except SpawnRequestError as exc:
        return {
            "status": "error",
            "reason": str(exc),
            "http_status": exc.status,
            "body": exc.body,
        }
    if result.get("status") == "accepted":
        return {"status": "pending", "admission": "unproven", "result": result}
    returncode = result.get("returncode")
    if type(returncode) is not int or returncode != 0:
        return {"status": "error", "result": result}
    return {"status": "ok", "result": result}
