"""``sac agent ...`` tools (F-CS15) — Python API + MCP wrappers.

Each tool is defined as a public Python function. The
:func:`register_agent_tools` shim attaches ``@mcp.tool()`` to each so
the same source of truth feeds both surfaces (per scitex MCP §6
parity).

The lifecycle verbs (``agent_start``/``stop``/``restart``) are
re-implemented here as JSON-friendly thin wrappers rather than
re-using ``_lifecycle.lifecycle.agent_start`` — the lifecycle
function takes a ``Registry | None`` parameter that fastmcp's pydantic
schema generator can't introspect. The wrappers go through
``invoke_cli_text`` so they share the same ``sac agent <verb>``
codepath the CLI runs.
"""

from __future__ import annotations

from typing import Any

from ._helpers import invoke_cli_json, invoke_cli_text


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


def agent_status(name: str) -> dict[str, Any]:
    """Detailed status for one agent (heartbeat, session id, quota,
    snapshot, context-management %). Mirrors
    ``sac agent status <name> --json``."""
    return invoke_cli_json(["agent", "status", name, "--json"])


def agent_logs(name: str, lines: int = 50) -> dict[str, Any]:
    """Tail the agent's session log. Mirrors
    ``sac agent logs <name> --lines <N>``."""
    return invoke_cli_text(["agent", "logs", name, "--lines", str(lines)])


def agent_health(name: str) -> dict[str, Any]:
    """Health check for one agent (heartbeat freshness, restart
    policy, watchdog state). Mirrors ``sac agent health --json``."""
    return invoke_cli_json(["agent", "health", name, "--json"])


def agent_find(name_or_pattern: str) -> dict[str, Any]:
    """Locate the YAML config for an agent name (or glob pattern).
    Mirrors ``sac agent find``."""
    return invoke_cli_text(["agent", "find", name_or_pattern])


def agent_check(name: str) -> dict[str, Any]:
    """Validate an agent's YAML + runtime preflight. Mirrors
    ``sac agent check <name>``."""
    return invoke_cli_text(["agent", "check", name])


def agent_validate(config_path: str) -> dict[str, Any]:
    """Validate a YAML config file's schema. Mirrors
    ``sac agent validate <path>``."""
    return invoke_cli_text(["agent", "validate", config_path])


def agent_inspect(name: str) -> dict[str, Any]:
    """Inspect an agent's effective config (resolved + merged).
    Mirrors ``sac agent inspect <name>``."""
    return invoke_cli_text(["agent", "inspect", name])


def agent_recall(name: str) -> dict[str, Any]:
    """Replay the recent recall events for an agent. Mirrors
    ``sac agent recall <name>``."""
    return invoke_cli_text(["agent", "recall", name])


def agent_check_priority(name: str) -> dict[str, Any]:
    """Run the priority-failback check for an agent. Mirrors
    ``sac agent check-priority <name>``."""
    return invoke_cli_text(["agent", "check-priority", name])


def agent_take_snapshot(name: str) -> dict[str, Any]:
    """Capture a state snapshot for an agent. Mirrors
    ``sac agent take-snapshot <name>``."""
    return invoke_cli_text(["agent", "take-snapshot", name])


# ─── Mutation verbs (the MCP host's permission flow gates these) ───


def agent_attach(name: str) -> dict[str, Any]:
    """Attach to a running agent's session (TTY-only operations
    return immediately when invoked from MCP — useful mainly for
    return-code introspection). Mirrors ``sac agent attach <name>``."""
    return invoke_cli_text(["agent", "attach", name])


def agent_start(name: str, foreground: bool = False) -> dict[str, Any]:
    """Start an agent by name. Mirrors ``sac agent start <name>``.

    JSON-friendly wrapper around the CLI. For programmatic callers
    that need to share a Registry instance, use
    :func:`scitex_agent_container._lifecycle.lifecycle.agent_start`
    directly.
    """
    argv = ["agent", "start", name]
    if foreground:
        argv.append("--foreground")
    return invoke_cli_text(argv)


def agent_stop(name: str) -> dict[str, Any]:
    """Stop a running agent. Mirrors ``sac agent stop <name>``."""
    return invoke_cli_text(["agent", "stop", name])


def agent_restart(name: str) -> dict[str, Any]:
    """Restart an agent (stop + start). Mirrors ``sac agent restart <name>``."""
    return invoke_cli_text(["agent", "restart", name])


def register_agent_tools(mcp) -> None:
    """Attach ``@mcp.tool()`` to every public function in this module."""
    for fn in (
        agent_list,
        agent_status,
        agent_logs,
        agent_health,
        agent_find,
        agent_check,
        agent_validate,
        agent_inspect,
        agent_recall,
        agent_check_priority,
        agent_take_snapshot,
        agent_attach,
        agent_start,
        agent_stop,
        agent_restart,
    ):
        mcp.tool()(fn)


__all__ = [
    "agent_list",
    "agent_status",
    "agent_logs",
    "agent_health",
    "agent_find",
    "agent_check",
    "agent_validate",
    "agent_inspect",
    "agent_recall",
    "agent_check_priority",
    "agent_take_snapshot",
    "agent_attach",
    "agent_start",
    "agent_stop",
    "agent_restart",
    "register_agent_tools",
]
