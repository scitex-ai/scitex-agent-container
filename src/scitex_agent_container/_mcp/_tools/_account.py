"""``sac accounts`` tools (F-CS15) — Python API + MCP wrappers."""

from __future__ import annotations

from typing import Any

from ._helpers import invoke_cli_text


def account_show() -> dict[str, Any]:
    """Show the current Anthropic account binding (Pro/Max OAuth
    vs API key, quota snapshot, email + tier). Mirrors
    ``sac accounts status``."""
    return invoke_cli_text(["accounts", "status"])


def quota_watch(name: str | None = None) -> dict[str, Any]:
    """Watch per-agent token quota and auto-rotate on threshold.
    Mirrors ``sac accounts watch-quota``.

    ``watch-quota`` is interactive in the CLI; running through
    ``CliRunner`` returns the first poll's snapshot then exits.
    """
    argv = ["accounts", "watch-quota"]
    if name:
        argv.append(name)
    return invoke_cli_text(argv)


def register_account_tools(mcp) -> None:
    for fn in (account_show, quota_watch):
        mcp.tool()(fn)


__all__ = ["account_show", "quota_watch", "register_account_tools"]
