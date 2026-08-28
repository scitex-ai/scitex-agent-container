"""``sac db ...`` tools (F-CS15) — Python API + MCP wrappers."""

from __future__ import annotations

from typing import Any

from ._helpers import invoke_cli_json, invoke_cli_text


def db_show() -> dict[str, Any]:
    """Print high-level state-db row counts (instances). Mirrors
    ``sac db show --json``.

    The counts follow :data:`KNOWN_TABLES`, and that tuple is what this
    docstring must track — it enumerated ``definitions, instances,
    heartbeats, events, channel_events`` and had been stale for a while by
    2026-08-28 (``heartbeats`` moved to PostgreSQL, ``attempts`` was
    deleted, then ``definitions`` / ``instance_heartbeats`` / ``events``,
    ``lineage`` and ``channel_events`` all went the same day). ONE name is
    the whole list now.

    ``lineage`` and ``channel_events`` did not VANISH the way the others
    did — the spawn DAG moved to the shared PostgreSQL store and the
    channel history became ``sac_channel_events`` (ADR-0023). This
    SQLite-shaped verb cannot see either, which is exactly why both names
    were removed rather than left here to answer zero: a count reported for
    a table sac no longer keeps here is the wrong-answer-that-looks-right
    the tuple has been shedding names to avoid."""
    return invoke_cli_json(["db", "show", "--json"])


def db_query(
    table: str | None = None,
    agent: str | None = None,
    host: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    """Query the state-db. Filter by table / agent / host. Mirrors
    ``sac db query --json``.

    The result carries ``store`` — the database the rows came from —
    because an MCP caller never sees the CLI's console rendering, and an
    empty ``data`` is otherwise indistinguishable from having queried the
    WRONG database. ``SCITEX_AGENT_CONTAINER_STATE_DB`` is set per-agent
    in every sac container, so each agent reads its own shard, which
    never holds fleet rows. On 2026-08-09 three agents independently
    concluded the fleet registry had been wiped from all-zero output
    here; two escalated it as P1 data loss while the host DB was healthy.

    It is a SIBLING key: ``data`` keeps its exact shape, and the CLI's
    stdout stays a bare array. The store deliberately does NOT travel on
    stderr — ``invoke_cli_json`` reads Click's merged ``result.output``,
    so a stderr line would make ``data`` unparseable and hand every
    caller ``None``.
    """
    argv = ["db", "query", "--json", "--limit", str(limit)]
    if table:
        argv += ["--table", table]
    if agent:
        argv += ["--agent", agent]
    if host:
        argv += ["--host", host]
    result = invoke_cli_json(argv)
    # Read through the MODULE: the constant binds from the environment at
    # import time, so a captured copy goes stale and would report a path
    # that is not the one just queried.
    from ..._state import state_db as _state_db

    result["store"] = str(_state_db.DEFAULT_DB_PATH)
    return result


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
