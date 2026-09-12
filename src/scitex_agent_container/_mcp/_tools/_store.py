"""``sac store ...`` tools (F-CS15) — Python API + MCP wrappers."""

from __future__ import annotations

from typing import Any

from ._helpers import invoke_cli_json, invoke_cli_text


def store_clean(heartbeat_stale_seconds: int = 600) -> dict[str, Any]:
    """Sweep dead instances. Mirrors ``sac store clean --json``."""
    return invoke_cli_json(
        [
            "store",
            "clean",
            "--heartbeat-stale-seconds",
            str(heartbeat_stale_seconds),
            "--json",
        ]
    )


def store_tick(heartbeat_stale_seconds: int = 600) -> dict[str, Any]:
    """One round of background reconciliation — the ``store clean`` sweep,
    silent. Mirrors ``sac store tick``.
    """
    return invoke_cli_text(
        ["store", "tick", "--heartbeat-stale-seconds", str(heartbeat_stale_seconds)]
    )


def store_migrate() -> dict[str, Any]:
    """Pull legacy ``registry/*.json`` rows into ``instances``.
    Idempotent. Mirrors ``sac store migrate``.

    Took a ``force`` flag until 2026-08-29 that appended ``--force`` to the
    argv. ``sac store migrate`` has never defined that option, so passing
    ``force=True`` did not force anything — Click refused the whole
    invocation with "no such option".
    """
    return invoke_cli_text(["store", "migrate"])


def register_store_tools(mcp) -> None:
    for fn in (
        store_clean,
        store_tick,
        store_migrate,
    ):
        mcp.tool()(fn)


__all__ = [
    "store_clean",
    "store_tick",
    "store_migrate",
    "register_store_tools",
]
