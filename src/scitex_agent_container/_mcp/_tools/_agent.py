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


def agent_start(
    name: str, foreground: bool = False, session: str | None = None
) -> dict[str, Any]:
    """Start an agent by name. Mirrors ``sac agents start <name>``.

    JSON-friendly wrapper around the CLI. For programmatic callers
    that need to share a Registry instance, use
    :func:`scitex_agent_container._lifecycle.lifecycle.agent_start`
    directly.

    Args:
        name: The agent to start.
        foreground: Attach to the caller's terminal (claude-session only).
        session: Optional session-continuity override for THIS start,
            mirroring the CLI ``--session`` / ``--continue`` / ``--fresh``.
            One of ``"fresh"`` (independent session — the default for
            experiment trials), ``"continue"`` (resume the latest session —
            for long-lived coordinators), or ``"resume"``. ``None`` leaves
            the spec's resolved ``claude.session`` (which role-defaults to
            ``continue`` for coordinators, ``fresh`` otherwise) untouched.
            ``"new-session"`` is accepted as a back-compat alias for
            ``"fresh"``.

    .. note::
       In-SIF dispatch: as of the SAC-from-SAC broker (operator-
       mandated 2026-06-01), an ``agent_start`` call from inside an
       apptainer SIF (``APPTAINER_CONTAINER`` / ``SINGULARITY_CONTAINER``
       set) auto-redirects to the host-side ``sac listen`` control
       plane via :mod:`_lifecycle._in_sif_broker`. The host re-runs
       ``check_spawn``, records the parent→child lineage edge, and
       shells the real ``sac agent start`` against the bare host's
       apptainer. On the bare host the local lifecycle is unchanged.
       Use :func:`agent_spawn` when you need to pass an *inline*
       spec dict (the host materialises it) or to be explicit about
       broker semantics in a hybrid script that might also run on
       the bare host.
    """
    argv = ["agents", "start", name]
    if foreground:
        argv.append("--foreground")
    if session is not None:
        mode = str(session).strip().lower()
        # Map to the friendly shorthand flags where they exist so the MCP
        # surface mirrors the CLI exactly; fall through to --session for
        # ``resume`` and the ``new-session`` back-compat alias.
        if mode == "continue":
            argv.append("--continue")
        elif mode == "fresh":
            argv.append("--fresh")
        elif mode in ("resume", "new-session"):
            argv += ["--session", mode]
        else:
            return {
                "status": "error",
                "reason": (
                    f"invalid session {session!r}; expected one of "
                    "fresh|continue|resume (or the alias new-session)."
                ),
            }
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
        result = request_spawn(name, caller=caller, spec=spec, overwrite=overwrite)
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


def agent_restart(name: str, fresh: bool = False) -> dict[str, Any]:
    """Restart an agent (stop + start). Mirrors ``sac agents restart <name>``.

    Passes ``--yes`` unconditionally: the CLI refuses an unconfirmed
    restart (``exit 2``), but an MCP tool call has no TTY to prompt on,
    so the call itself IS the confirmation. Without this the tool always
    failed with "Refusing to restart ... without --yes/-y." (no-surprise:
    the documented MCP surface must actually work, not dead-end on an
    interactive guard that can never be satisfied over MCP).

    Host bypass (operator 2026-06-29 "agents manage agents"): when this
    tool runs INSIDE a container, the target peer's registry row lives on
    the bare host and is unresolvable locally — the underlying CLI then
    falls back to brokering the restart to the HOST listen
    (``POST {SAC_LISTEN_BASE_URL}/agents/<name>/restart``, manage-gated by
    ``check_lineage_acl``), exactly like ``agent_spawn`` brokers a spawn.
    The fallback is transparent here: the CLI runs it internally so this
    tool needs no extra wiring; on a bare host (row resolvable) the local
    path runs unchanged.

    ``fresh=True`` brokers a NEW Claude session (``start --force --fresh``)
    instead of a resuming restart — the deterministic recovery for an agent
    wedged on a boot prompt whose queued input keeps returning on a plain
    restart.
    """
    argv = ["agents", "restart", name, "--yes"]
    if fresh:
        argv.append("--fresh")
    return invoke_cli_text(argv)


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


def agent_create(
    name: str,
    template: str = "developer",
    workdir: str | None = None,
    telegram_token: str | None = None,
    group: str | None = None,
    start: bool = False,
) -> dict[str, Any]:
    """Create a proven-shape agent spec from a template. Mirrors
    ``sac agents create <name> --template developer|scientist``.

    Writes ``<name>/spec.yaml`` from the developer/scientist skeleton,
    filling identity (name -> project / workdir / overlay / state-db /
    SCITEX_TODO_AGENT) and auto-detecting the editable-install block
    (workdir ships a package) and the per-agent Telegram bot
    (``telegram_token`` file present). ``start=True`` launches the agent
    afterwards. The developer group is authorized to CRUD agents."""
    argv = ["agents", "create", name, "--template", template]
    if workdir:
        argv += ["--workdir", workdir]
    if telegram_token:
        argv += ["--telegram-token", telegram_token]
    if group:
        argv += ["--group", group]
    if start:
        argv += ["--start"]
    return invoke_cli_text(argv)


def host_exec_local(
    argv: list[str],
    cwd: str | None = None,
    timeout_s: float | None = None,
    env: dict[str, str] | None = None,
    caller: str | None = None,
) -> dict[str, Any]:
    """Run an arbitrary command on the HOST via the ``sac listen`` bypass.

    Operator directive 2026-07-01: developer + researcher agents run any host
    command through the listen daemon. Unblocks in-container image builds
    (``sac image build``), cron/systemd apply, and other host-only ops that
    otherwise require the operator's shell.

    POSTs to ``{SAC_LISTEN_BASE_URL}/v1/host_exec``. The listen daemon
    enforces the group gate (403 unless caller is in developer/researcher)
    and appends one JSONL audit line per invocation.

    Args:
        argv: The command to run — a non-empty list of strings. No shell form;
            no expansion. E.g. ``["sac", "image", "build", "base", "-y"]``.
        cwd: Optional working directory for the child process.
        timeout_s: Optional per-command timeout on the server side. Bounded to
            ``(0, 3600]``; defaults to 300s server-side when omitted.
        env: Optional extra environment vars (merged onto the daemon's env on
            the host).
        caller: Override the auto-resolved caller identity (defaults to
            ``SAC_NAME`` from the container). Only consulted on the host-wide
            bearer path.

    Returns:
        On success: ``{"status": "ok", "result": {"exit_code": int, "stdout":
        str, "stderr": str, "duration_s": float, "timed_out": bool}}``. The MCP
        host can branch on ``result.exit_code`` and ``result.timed_out``.

        On failure (transport / 401 / 403 / 400 / 500 / missing env):
        ``{"status": "error", "reason": "...", "http_status": <int|null>,
        "body": <server body or null>}`` — fail loud, never silently swallowed.
    """
    from ..._lifecycle._host_exec_client import (
        HostExecRequestError,
        request_host_exec,
    )

    try:
        result = request_host_exec(
            argv,
            cwd=cwd,
            timeout_s=timeout_s,
            env=env,
            caller=caller,
        )
    except HostExecRequestError as exc:
        return {
            "status": "error",
            "reason": str(exc),
            "http_status": exc.status,
            "body": exc.body,
        }
    return {"status": "ok", "result": result}


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
        agent_create,
        agent_start,
        agent_spawn,
        agent_stop,
        agent_restart,
        agent_send,
        host_exec_local,
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
    "agent_create",
    "agent_start",
    "agent_spawn",
    "agent_stop",
    "agent_restart",
    "agent_send",
    "host_exec_local",
    "register_agent_tools",
]
