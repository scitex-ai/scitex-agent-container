"""``sac db ...`` tools (F-CS15) — Python API + MCP wrappers."""

from __future__ import annotations

from typing import Any

from ._helpers import invoke_cli_json, invoke_cli_text


def db_show() -> dict[str, Any]:
    """Print high-level state-db row counts (definitions, instances,
    heartbeats, events, attempts). Mirrors ``sac db show --json``."""
    return invoke_cli_json(["db", "show", "--json"])


def db_query(
    table: str | None = None,
    agent: str | None = None,
    host: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Query the state-db. Filter by table / agent / host. Mirrors
    ``sac db query --json``."""
    argv = ["db", "query", "--json", "--limit", str(limit)]
    if table:
        argv += ["--table", table]
    if agent:
        argv += ["--agent", agent]
    if host:
        argv += ["--host", host]
    return invoke_cli_json(argv)


def db_clean(heartbeat_stale_seconds: int = 600) -> dict[str, Any]:
    """Sweep dead instances. Mirrors ``sac db clean --json``."""
    return invoke_cli_json(
        [
            "db",
            "clean",
            "--heartbeat-stale-seconds",
            str(heartbeat_stale_seconds),
            "--json",
        ]
    )


def db_tick(heartbeat_stale_seconds: int = 600) -> dict[str, Any]:
    """One round of background reconciliation (clean + import legacy
    registry). Mirrors ``sac db tick``."""
    return invoke_cli_text(
        ["db", "tick", "--heartbeat-stale-seconds", str(heartbeat_stale_seconds)]
    )


def db_migrate(force: bool = False) -> dict[str, Any]:
    """Pull legacy ``registry/*.json`` rows into the state-db.
    Idempotent. Mirrors ``sac db migrate``."""
    argv = ["db", "migrate"]
    if force:
        argv.append("--force")
    return invoke_cli_text(argv)


def db_export(since: str | None = None, host: str | None = None) -> dict[str, Any]:
    """Export state-db rows for cross-host pull. Mirrors
    ``sac db export --json``."""
    argv = ["db", "export"]
    if since:
        argv += ["--since", since]
    if host:
        argv += ["--host", host]
    return invoke_cli_text(argv)


def db_import(input_path: str) -> dict[str, Any]:
    """Import state-db rows from a peer's ``db export`` blob.
    Mirrors ``sac db import <path> --json``."""
    return invoke_cli_json(["db", "import", input_path, "--json"])


def register_db_tools(mcp) -> None:
    for fn in (
        db_show,
        db_query,
        db_clean,
        db_tick,
        db_migrate,
        db_export,
        db_import,
    ):
        mcp.tool()(fn)


__all__ = [
    "db_show",
    "db_query",
    "db_clean",
    "db_tick",
    "db_migrate",
    "db_export",
    "db_import",
    "register_db_tools",
]
