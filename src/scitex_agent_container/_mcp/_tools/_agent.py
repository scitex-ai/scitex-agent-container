"""``sac agent ...`` MCP tools (F-CS15)."""

from __future__ import annotations

from typing import Any

from ._helpers import invoke_cli_json, invoke_cli_text


def register_agent_tools(mcp) -> None:
    """Register every ``sac_agent_*`` tool on the FastMCP server."""

    @mcp.tool()
    def agent_list(
        capability: str | None = None,
        machine: str | None = None,
    ) -> dict[str, Any]:
        """List every registered agent + liveness flags. Mirrors
        ``sac agent list --json``. Filter by ``capability`` (label
        substring match) or ``machine`` (label exact match)."""
        argv = ["agent", "list", "--json"]
        if capability:
            argv += ["--capability", capability]
        if machine:
            argv += ["--machine", machine]
        return invoke_cli_json(argv)

    @mcp.tool()
    def agent_status(name: str) -> dict[str, Any]:
        """Detailed status for one agent (heartbeat, session id, quota,
        snapshot, context-management %). Mirrors
        ``sac agent status <name> --json``."""
        return invoke_cli_json(["agent", "status", name, "--json"])

    @mcp.tool()
    def agent_logs(name: str, lines: int = 50) -> dict[str, Any]:
        """Tail the agent's session log. Mirrors
        ``sac agent logs <name> --lines <N>``."""
        return invoke_cli_text(["agent", "logs", name, "--lines", str(lines)])

    @mcp.tool()
    def agent_health(name: str) -> dict[str, Any]:
        """Health check for one agent (heartbeat freshness, restart
        policy, watchdog state). Mirrors ``sac agent health --json``."""
        return invoke_cli_json(["agent", "health", name, "--json"])

    @mcp.tool()
    def agent_find(name_or_pattern: str) -> dict[str, Any]:
        """Locate the YAML config for an agent name (or glob pattern).
        Mirrors ``sac agent find``."""
        return invoke_cli_text(["agent", "find", name_or_pattern])

    @mcp.tool()
    def agent_check(name: str) -> dict[str, Any]:
        """Validate an agent's YAML + runtime preflight. Mirrors
        ``sac agent check <name>``."""
        return invoke_cli_text(["agent", "check", name])

    @mcp.tool()
    def agent_validate(config_path: str) -> dict[str, Any]:
        """Validate a YAML config file's schema. Mirrors
        ``sac agent validate <path>``."""
        return invoke_cli_text(["agent", "validate", config_path])

    @mcp.tool()
    def agent_inspect(name: str) -> dict[str, Any]:
        """Inspect an agent's effective config (resolved + merged).
        Mirrors ``sac agent inspect <name>``."""
        return invoke_cli_text(["agent", "inspect", name])

    @mcp.tool()
    def agent_recall(name: str) -> dict[str, Any]:
        """Replay the recent recall events for an agent. Mirrors
        ``sac agent recall <name>``."""
        return invoke_cli_text(["agent", "recall", name])

    @mcp.tool()
    def agent_check_priority(name: str) -> dict[str, Any]:
        """Run the priority-failback check for an agent. Mirrors
        ``sac agent check-priority <name>``."""
        return invoke_cli_text(["agent", "check-priority", name])

    @mcp.tool()
    def agent_take_snapshot(name: str) -> dict[str, Any]:
        """Capture a state snapshot for an agent. Mirrors
        ``sac agent take-snapshot <name>``."""
        return invoke_cli_text(["agent", "take-snapshot", name])

    # ─── Mutation verbs (gated by the MCP host's own permission flow) ───

    @mcp.tool()
    def agent_start(name: str, foreground: bool = False) -> dict[str, Any]:
        """Start an agent by name. Mirrors ``sac agent start <name>``.
        ``foreground=True`` requires a TTY and blocks; the MCP path
        defaults to daemon mode."""
        argv = ["agent", "start", name]
        if foreground:
            argv.append("--foreground")
        return invoke_cli_text(argv)

    @mcp.tool()
    def agent_stop(name: str) -> dict[str, Any]:
        """Stop a running agent. Mirrors ``sac agent stop <name>``."""
        return invoke_cli_text(["agent", "stop", name])

    @mcp.tool()
    def agent_restart(name: str) -> dict[str, Any]:
        """Restart an agent (stop + start). Mirrors ``sac agent restart <name>``."""
        return invoke_cli_text(["agent", "restart", name])

    @mcp.tool()
    def agent_attach(name: str) -> dict[str, Any]:
        """Attach to a running agent's session (TTY-only operations
        return immediately when invoked from MCP — useful mainly for
        return-code introspection). Mirrors ``sac agent attach <name>``."""
        return invoke_cli_text(["agent", "attach", name])


__all__ = ["register_agent_tools"]
