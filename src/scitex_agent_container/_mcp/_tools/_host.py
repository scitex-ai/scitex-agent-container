"""``sac host ...`` MCP tools (F-CS15)."""

from __future__ import annotations

from typing import Any

from ._helpers import invoke_cli_json, invoke_cli_text


def register_host_tools(mcp) -> None:
    @mcp.tool()
    def host_show() -> dict[str, Any]:
        """Print the current host's identity (hostname, interfaces,
        Tailscale state). Mirrors ``sac host show --json``."""
        return invoke_cli_json(["host", "show", "--json"])

    @mcp.tool()
    def host_list() -> dict[str, Any]:
        """List configured peers (~/.scitex/agent-container/sac.yaml).
        Mirrors ``sac host list --json``."""
        return invoke_cli_json(["host", "list", "--json"])

    @mcp.tool()
    def host_validate() -> dict[str, Any]:
        """Validate the sac.yaml peer config (chain integrity,
        unknown via, schema). Mirrors ``sac host validate --json``."""
        return invoke_cli_json(["host", "validate", "--json"])

    @mcp.tool()
    def host_probe(peer: str, timeout: int = 5) -> dict[str, Any]:
        """SSH-probe a peer for liveness + sac availability. Mirrors
        ``sac host probe <peer> --json``."""
        return invoke_cli_json(
            ["host", "probe", peer, "--timeout", str(timeout), "--json"]
        )

    @mcp.tool()
    def host_exec(peer: str, command: list[str]) -> dict[str, Any]:
        """Run a sac sub-command on ``peer`` over multi-hop SSH.
        Mirrors ``sac host exec <peer> -- <command...>``."""
        return invoke_cli_text(["host", "exec", peer, "--", *command])


__all__ = ["register_host_tools"]
