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

    .. note::
       Runs the CLI **inside the current container**. Inside an
       apptainer-isolated agent that means apptainer-in-apptainer
       (blocked on most HPCs) or a bare-runner fallback. Use
       :func:`agent_spawn` instead to ask the bare HOST to start the
       child via the sac-listen control plane — that path goes through
       the server-side ACL gate and records the parent→child lineage
       edge automatically (ADR-0010 mechanism #3).
    """
    argv = ["agents", "start", name]
    if foreground:
        argv.append("--foreground")
    return invoke_cli_text(argv)


def agent_spawn(
    name: str,
    spec: dict | None = None,
    overwrite: bool = False,
    caller: str | None = None,
) -> dict[str, Any]:
    """Ask the HOST sac-listen to spawn ``name`` on the bare host.

    This is the ADR-0010 mechanism-#3 spawn path — the only sanctioned
    agent-driven spawn surface. Every accepted request runs through
    the server-side ``check_spawn`` ACL gate and is recorded in the
    ``lineage`` table BEFORE any runtime work happens. The child is
    booted on the bare host (no apptainer-in-apptainer).

    Compared to :func:`agent_start` (which shells ``sac agents start``
    in the CURRENT container), ``agent_spawn`` POSTs to the host's
    ``/agents`` endpoint via :mod:`_lifecycle._spawn_client`. The
    container needs ``SAC_LISTEN_BASE_URL`` (+ optional
    ``SAC_LISTEN_BEARER``) injected by the apptainer runtime; both
    are standard on sac-managed agents.

    Args:
        name: The child agent to start (must be registered on the
            host OR provided inline via ``spec``).
        spec: Optional inline spec dict ``{apiVersion, kind, spec}``.
            When provided, the server materialises it under
            ``~/.scitex/agent-container/agents/<name>/spec.yaml`` and
            then starts it. Use for ephemeral / per-turn children.
        overwrite: Only meaningful with ``spec`` — overwrite an
            existing on-disk spec instead of returning 409.
        caller: Override the auto-resolved caller identity. Defaults
            to ``SAC_NAME`` from the container env (resolved inside
            :mod:`_lifecycle._spawn_client`), which is what the
            server-side ``check_spawn`` gate keys off.

    Returns:
        On success: ``{"status": "ok", "result": {...server body...}}``
        where the inner body is the agents_start response
        ``{name, returncode, stdout, stderr}``. The MCP host can branch
        on ``result.returncode``.

        On failure (transport / 4xx-5xx / missing env): ``{"status":
        "error", "reason": "...", "http_status": <int|null>,
        "body": <server body or null>}`` — fail loud, never silently
        swallowed (ADR-0010 / handoff §0).
    """
    from ..._lifecycle._spawn_client import SpawnRequestError, request_spawn

    try:
        result = request_spawn(
            name, caller=caller, spec=spec, overwrite=overwrite
        )
    except SpawnRequestError as exc:
        return {
            "status": "error",
            "reason": str(exc),
            "http_status": exc.status,
            "body": exc.body,
        }
    return {"status": "ok", "result": result}


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
        agent_spawn,
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
    "agent_spawn",
    "agent_stop",
    "agent_restart",
    "agent_send",
    "register_agent_tools",
]
