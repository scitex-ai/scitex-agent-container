"""``sac account / sac quota`` MCP tools (F-CS15)."""

from __future__ import annotations

from typing import Any

from ._helpers import invoke_cli_text


def register_account_tools(mcp) -> None:
    @mcp.tool()
    def account_show() -> dict[str, Any]:
        """Show the current Anthropic account binding (Pro/Max OAuth
        vs API key, balance hints if available). Mirrors ``sac account``."""
        return invoke_cli_text(["account"])

    @mcp.tool()
    def quota_watch(name: str | None = None) -> dict[str, Any]:
        """Watch per-agent token quota. Mirrors ``sac quota watch``."""
        argv = ["quota", "watch"]
        if name:
            argv.append(name)
        # quota watch is interactive; running through CliRunner returns
        # the first poll's snapshot then exits.
        return invoke_cli_text(argv)


__all__ = ["register_account_tools"]
