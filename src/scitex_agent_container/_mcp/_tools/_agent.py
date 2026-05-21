"""``sac agents ...`` tools (F-CS15) — Python API + MCP wrappers.

Each tool is defined as a public Python function. The
:func:`register_agent_tools` shim attaches ``@mcp.tool()`` to each so
the same source of truth feeds both surfaces (per scitex MCP §6
parity).

The lifecycle verbs (``agent_start``/``stop``/``restart``) are
re-implemented here as JSON-friendly thin wrappers rather than
re-using ``_lifecycle.lifecycle.agent_start`` — the lifecycle
function takes a ``Registry | None`` parameter that fastmcp's pydantic
schema generator can't introspect. The wrappers go through
``invoke_cli_text`` so they share the same ``sac agents <verb>``
codepath the CLI runs.

The CLI group was renamed ``agent`` → ``agents`` (plural); every argv
prefix below targets the current ``agents`` group. Tools whose
subcommands were removed in that restructure (``validate``,
``inspect``, ``check-priority``, ``take-snapshot``, ``attach``,
``logs``) are gone here too — except ``logs``, whose replacement
``tail`` is wired under the kept public name ``agent_logs``.
"""

from __future__ import annotations

from typing import Any

from ._helpers import invoke_cli_json, invoke_cli_text


def agent_list(
    capability: str | None = None,
    machine: str | None = None,
) -> dict[str, Any]:
    """List every registered agent + liveness flags. Mirrors
    ``sac agents list --json``. Filter by ``capability`` (label
    substring match) or ``machine`` (label exact match)."""
    argv = ["agents", "list", "--json"]
    if capability:
        argv += ["--capability", capability]
    if machine:
        argv += ["--machine", machine]
    return invoke_cli_json(argv)


def agent_status(name: str) -> dict[str, Any]:
    """Detailed status for one agent (heartbeat, session id, quota,
    snapshot, context-management %). Mirrors
    ``sac agents list <name> --json`` — the ``list`` leaf renders a
    single-agent status view when given a NAME (the old ``status``
    subcommand was folded into ``list`` in the group rename)."""
    return invoke_cli_json(["agents", "list", name, "--json"])


def agent_logs(name: str, lines: int = 50) -> dict[str, Any]:
    """Tail the agent's SDK runner session transcript. Mirrors
    ``sac agents tail <name> --lines <N>``."""
    return invoke_cli_text(["agents", "tail", name, "--lines", str(lines)])


def agent_health(name: str) -> dict[str, Any]:
    """Health check for one agent (heartbeat freshness, restart
    policy, watchdog state). Mirrors ``sac agents health --json``."""
    return invoke_cli_json(["agents", "health", name, "--json"])


def agent_find(name_or_pattern: str) -> dict[str, Any]:
    """Locate the YAML config for an agent name (or glob pattern).
    Mirrors ``sac agents find``."""
    return invoke_cli_text(["agents", "find", name_or_pattern])


def agent_check(name: str) -> dict[str, Any]:
    """Validate an agent's YAML + runtime preflight. Mirrors
    ``sac agents check <name>``."""
    return invoke_cli_text(["agents", "check", name])


def agent_recall(name: str) -> dict[str, Any]:
    """Replay the recent recall events for an agent. Mirrors
    ``sac agents recall <name>``."""
    return invoke_cli_text(["agents", "recall", name])


# ─── Mutation verbs (the MCP host's permission flow gates these) ───


def agent_start(name: str, foreground: bool = False) -> dict[str, Any]:
    """Start an agent by name. Mirrors ``sac agents start <name>``.

    JSON-friendly wrapper around the CLI. For programmatic callers
    that need to share a Registry instance, use
    :func:`scitex_agent_container._lifecycle.lifecycle.agent_start`
    directly.
    """
    argv = ["agents", "start", name]
    if foreground:
        argv.append("--foreground")
    return invoke_cli_text(argv)


def agent_stop(name: str) -> dict[str, Any]:
    """Stop a running agent. Mirrors ``sac agents stop <name>``."""
    return invoke_cli_text(["agents", "stop", name])


def agent_restart(name: str) -> dict[str, Any]:
    """Restart an agent (stop + start). Mirrors ``sac agents restart <name>``."""
    return invoke_cli_text(["agents", "restart", name])


def agent_send(
    name: str,
    prompt: str | None = None,
    timeout_seconds: int = 120,
    key: str | None = None,
    model: str | None = None,
    max_turns: int | None = None,
    wait: bool = False,
) -> dict[str, Any]:
    """Dispatch one prompt (or control key) to ``name``'s live session.

    NON-BLOCKING by default. An MCP tool call cannot be backgrounded by
    the caller, so a synchronous send would hang the lead's whole turn
    until the target agent finishes processing. By default this tool
    therefore validates that the agent is reachable and returns PROMPTLY
    with ``status="dispatched"`` plus a backgroundable ``track_command``
    — the equivalent ``sac agents send ...`` CLI the caller runs in a
    background shell to deliver the prompt and stream the reply. Pass
    ``wait=True`` to block inline and get the reply in ``response_text``.

    Library-grade dispatch to the agent's A2A sidecar. Unlike the
    other ``agent_*`` tools this does NOT go through the CLI runner
    surface — it calls
    :func:`scitex_agent_container.cli_pkg._send.send_to_agent` directly
    so the structured payload survives MCP transport intact (the CLI
    prints the reply as free text, which would lose the metadata).

    Returns the helper's dict verbatim. ``status`` is one of:

      * ``"dispatched"`` — (default, ``wait=False``) reachability
        validated; ``track_command`` carries the backgroundable
        ``sac agents send ...`` CLI to deliver + await the reply
      * ``"ok"`` — (``wait=True``) reply received; ``response_text``
        populated
      * ``"error"`` — agent not running / no a2a_port / sidecar
        unreachable / HTTP failure
      * ``"creds-expired"`` — lead/peer OAuth token expired
      * ``"timeout"`` — (``wait=True``) no response in ``timeout_seconds``

    ``prompt`` and ``key`` are mutually exclusive; passing both raises
    ``ValueError`` (surfaced to the MCP host as a tool-input error).
    """
    from ...cli_pkg._send import send_to_agent

    return send_to_agent(
        name,
        prompt=prompt,
        key=key,
        timeout_seconds=timeout_seconds,
        model=model,
        max_turns=max_turns,
        wait=wait,
    )


def register_agent_tools(mcp) -> None:
    """Attach ``@mcp.tool()`` to every public function in this module."""
    for fn in (
        agent_list,
        agent_status,
        agent_logs,
        agent_health,
        agent_find,
        agent_check,
        agent_recall,
        agent_start,
        agent_stop,
        agent_restart,
        agent_send,
    ):
        mcp.tool()(fn)


__all__ = [
    "agent_list",
    "agent_status",
    "agent_logs",
    "agent_health",
    "agent_find",
    "agent_check",
    "agent_recall",
    "agent_start",
    "agent_stop",
    "agent_restart",
    "agent_send",
    "register_agent_tools",
]
