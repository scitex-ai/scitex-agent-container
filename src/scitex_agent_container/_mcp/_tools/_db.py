"""``sac db ...`` tools (F-CS15) — Python API + MCP wrappers."""

from __future__ import annotations

from typing import Any

from ._helpers import invoke_cli_json, invoke_cli_text


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
    """One round of background reconciliation — the ``db clean`` sweep,
    silent. Mirrors ``sac db tick``.
    """
    return invoke_cli_text(
        ["db", "tick", "--heartbeat-stale-seconds", str(heartbeat_stale_seconds)]
    )


def db_migrate() -> dict[str, Any]:
    """Pull legacy ``registry/*.json`` rows into ``instances``.
    Idempotent. Mirrors ``sac db migrate``.

    Took a ``force`` flag until 2026-08-29 that appended ``--force`` to the
    argv. ``sac db migrate`` has never defined that option, so passing
    ``force=True`` did not force anything — Click refused the whole
    invocation with "no such option".
    """
    return invoke_cli_text(["db", "migrate"])


def register_db_tools(mcp) -> None:
    for fn in (
        db_clean,
        db_tick,
        db_migrate,
    ):
        mcp.tool()(fn)


__all__ = [
    "db_clean",
    "db_tick",
    "db_migrate",
    "register_db_tools",
]
