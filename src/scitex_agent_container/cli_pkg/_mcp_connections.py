"""Read-only Claude Code connection observations, without boot recovery."""

import json

import click


def connection_exit_code(result: dict) -> int:
    """Only a verified connected result is successful; unknown is not OK."""
    statuses = result.get("statuses") or {}
    healthy = (
        result.get("action") == "ok"
        and bool(statuses)
        and all(status == "connected" for status in statuses.values())
        and not result.get("failed")
        and not result.get("unknown")
    )
    return 0 if healthy else 1


def check_connections(*, as_json: bool) -> int:
    """Observe the existing client status provider with restart disabled."""
    from .._mcp._healthcheck import run_healthcheck

    result = run_healthcheck(allow_restart=False)
    code = connection_exit_code(result)
    if as_json:
        click.echo(json.dumps(result))
    else:
        label = "verified" if code == 0 else "unverified"
        click.echo(
            f"MCP connections (Claude Code): {label}; action={result.get('action')}"
        )
    return code
