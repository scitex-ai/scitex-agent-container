"""Inner-command argv builders for ``ApptainerContainerRuntime``.

Factored out of ``_apptainer_runtime.py`` so adding new ``kind``
dispatch branches doesn't push the parent module past the 512-line
cap. Each builder returns the full ``[tini, --, python3, -m, MODULE,
*args]`` list for one ``kind``.
"""

from __future__ import annotations

import json
import os as _os
import shlex
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import AgentConfig

# Runner-module dispatch by ``config.kind``. Kept here so the parent
# orchestrator doesn't need to know either runner's module path.
RUNNER_MODULE_AGENT = "scitex_agent_container._runners.claude_session"
RUNNER_MODULE_PROXY = "scitex_agent_container._runners.a2a_proxy"

_TINI_PREFIX = ["/usr/bin/tini", "-s", "--", "python3", "-m"]

# Unconditional supervisor-restart floor for ``kind: Agent`` runners.
# The session runner auto-resumes the persisted session_id; when that
# conversation no longer exists in the container's ~/.claude/projects/
# store (overlay redeployed on restart, or aged out after long idle),
# ``claude --resume <dead-id>`` exits 1 and EVERY turn crashes — a
# false-healthy "zombie" (heartbeat + /health keep answering). The
# runner's history-walk recovery (_resume_candidate steps to older ids,
# then falls back to a fresh session) plus the supervisor loop only run
# when ``--max-restarts > 0``; the CLI default is 0, so the recovery is
# dead code for the common ``restart.policy: never`` agent. We pass a
# floor of restarts so the recovery path is always live — re-resuming a
# provably-dead id can never succeed, and the history-walk is the only
# escape. An explicit on-failure/always policy with a higher
# ``max_retries`` raises the cap above this floor. Auth failures stay
# terminal regardless (the supervisor short-circuits them).
_SUPERVISOR_RESTART_FLOOR = 3


def _format_shell_steps(cmds: list) -> list[str]:
    # list[StartupCommand] -> shell statement list. `set -e` so any
    # failing step aborts launch loudly; delay=N becomes `sleep N`.
    steps: list[str] = []
    has_real = False
    for c in cmds:
        cmd = (getattr(c, "command", "") or "").strip()
        if not cmd:
            continue
        if not has_real:
            steps.append("set -e")
            has_real = True
        delay = int(getattr(c, "delay", 0) or 0)
        if delay > 0:
            steps.append(f"sleep {delay}")
        steps.append(cmd)
    return steps


def build_inner_argv(
    config: "AgentConfig",
    *,
    one_shot: bool = False,
    tui: bool = False,
    tui_mcp_config: str | None = None,
    tui_channel_mcp: str | None = None,
    tui_dev_channels: str | None = None,
) -> list[str]:
    """Return the apptainer-inner argv. Dispatches on ``config.kind``.

    When ``spec.startup_commands`` is non-empty, the argv is wrapped
    in ``[/bin/bash, -lc, "set -e; <cmd1>; sleep N; <cmd2>; exec <tini ...>"]``
    so the commands run as container-internal shell BEFORE the claude
    SDK process starts. ``exec`` replaces bash with tini, keeping PID 1
    clean. NOT a claude prompt — see ``spec.startup_prompts``.

    ``tui=True`` selects the interactive ``claude`` TUI as the inner
    process instead of the ``python -m`` SDK session runner (see
    :func:`_tui_runner_argv`). The startup_commands wrapper still
    applies, so container-internal bootstrap (uv venv, symlinks, ...)
    runs before ``exec claude`` identically to the SDK path.
    """
    kind = getattr(config, "kind", "Agent")
    if tui:
        runner_tail = _tui_runner_argv(
            config,
            mcp_config=tui_mcp_config,
            channel_mcp=tui_channel_mcp,
            dev_channels=tui_dev_channels,
        )
    elif kind == "AgentProxy":
        runner_tail = _TINI_PREFIX + [RUNNER_MODULE_PROXY] + _proxy_runner_argv(config)
    else:
        runner_tail = (
            _TINI_PREFIX
            + [RUNNER_MODULE_AGENT]
            + _agent_runner_argv(config, one_shot=one_shot)
        )

    startup_cmds = list(getattr(config, "startup_commands", []) or [])
    shell_steps = _format_shell_steps(startup_cmds)
    if not shell_steps:
        return runner_tail

    quoted_runner = " ".join(shlex.quote(p) for p in runner_tail)
    inline = "; ".join(shell_steps + [f"exec {quoted_runner}"])
    return ["/bin/bash", "-lc", inline]


def _resolve_max_restarts(config: "AgentConfig") -> int:
    """Supervisor restart cap for the session runner.

    Always at least :data:`_SUPERVISOR_RESTART_FLOOR` so the runner's
    resume-recovery (history-walk + fresh-session fallback) is live even
    for ``restart.policy: never`` agents. An explicit ``on-failure`` /
    ``always`` policy whose ``max_retries`` exceeds the floor raises the
    cap; ``never`` never lowers it below the floor (the floor is about
    *resume recovery*, not the operator's crash-restart policy — a dead
    session id can only be escaped by re-opening the client).
    """
    restart = getattr(config, "restart", None)
    policy = str(getattr(restart, "policy", "never") or "never")
    if policy in ("on-failure", "always"):
        max_retries = int(getattr(restart, "max_retries", 0) or 0)
        return max(max_retries, _SUPERVISOR_RESTART_FLOOR)
    return _SUPERVISOR_RESTART_FLOOR


def _resolve_restart_backoff_s(config: "AgentConfig") -> float:
    """Initial supervisor backoff seconds (doubles each retry).

    Uses ``restart.backoff_initial`` when an explicit on-failure/always
    policy sets one; otherwise the runner CLI default (1.0s) keeps the
    floor-driven resume recovery fast.
    """
    restart = getattr(config, "restart", None)
    policy = str(getattr(restart, "policy", "never") or "never")
    if policy in ("on-failure", "always"):
        backoff = getattr(restart, "backoff_initial", None)
        if isinstance(backoff, (int, float)) and backoff > 0:
            return float(backoff)
    return 1.0


def _agent_runner_argv(config: "AgentConfig", *, one_shot: bool) -> list[str]:
    """Argv tail for ``kind: Agent`` (claude_session)."""
    runner_argv: list[str] = [
        "--name",
        config.name,
        "--state-root",
        "/state",
        # Keep the supervisor / resume-recovery path live (see
        # _SUPERVISOR_RESTART_FLOOR). The runner CLI defaults to 0
        # (no restart), which silently disables recovery for every
        # restart.policy: never agent.
        "--max-restarts",
        str(_resolve_max_restarts(config)),
        "--restart-backoff-s",
        str(_resolve_restart_backoff_s(config)),
    ]
    # startup_prompts -> claude SDK mission via --mission. NO fallback
    # from startup_commands; that field is shell-exec only (see
    # build_inner_argv wrapper).
    prompts = list(getattr(config, "startup_prompts", []) or [])
    mission = str(prompts[0]).strip() if prompts else ""
    if mission:
        runner_argv += ["--mission", mission]
        if one_shot:
            # one-shot semantics → exit after the first SDK turn.
            runner_argv.append("--print-stream")
    # spec.a2a.port → --a2a-port (sidecar bind). Without this the
    # sidecar never binds and POST /v1/turn is unreachable.
    a2a_spec = getattr(config, "a2a", None)
    a2a_port = getattr(a2a_spec, "port", None) if a2a_spec else None
    # Resolved-int only: ``"auto"`` strings or None mean no sidecar
    # arg at this layer. The lifecycle resolves ``"auto"`` → int via
    # port_allocator BEFORE we get here; if a string slipped through,
    # it's a config that bypassed agent_start (e.g. dry-run inspection)
    # and the sidecar simply won't be wired up.
    if isinstance(a2a_port, int) and a2a_port > 0:
        runner_argv += ["--a2a-port", str(a2a_port)]
        cfg_path = getattr(config, "config_path", "")
        if cfg_path:
            # Spec path is host-side; apptainer auto-binds /home so
            # the in-container path is the same string. Used to
            # publish /.well-known/agent-card.json.
            runner_argv += ["--a2a-card-yaml", str(cfg_path)]
    # spec.claude.channels → one --channels arg per entry. When the set
    # contains 'server:sac', the daemon runner threads it into
    # build_sdk_options, which auto-registers the 'sac mcp channel' stdio
    # MCP so the long-lived SDK session subscribes to its inbox SSE and
    # a2a_send pushes are delivered. Mirrors the legacy stateless path in
    # a2a/_handlers.py + a2a/executors/_claude_session.py.
    claude_spec = getattr(config, "claude", None)
    channels = list(getattr(claude_spec, "channels", []) or []) if claude_spec else []
    for channel in channels:
        channel = str(channel).strip()
        if channel:
            runner_argv += ["--channels", channel]
    auto = getattr(config, "autonomous", None)
    if auto is not None and getattr(auto, "enabled", False):
        runner_argv += [
            "--autonomous-enabled",
            "--autonomous-drive-until",
            auto.drive_until,
            "--autonomous-max-turns",
            str(auto.max_turns),
            "--autonomous-kick-text",
            auto.kick_text,
        ]
    return runner_argv


_CLAUDE_TUI_BIN = "claude"


def _tui_runner_argv(
    config: "AgentConfig",
    *,
    mcp_config: str | None = None,
    channel_mcp: str | None = None,
    dev_channels: str | None = None,
) -> list[str]:
    """Argv for the interactive ``claude`` TUI (``spec.runtime: tui``).

    The inner process is the bundled ``claude`` binary running its
    interactive Ink TUI — the tmux PTY the caller wraps this argv in is
    what gives it a terminal. Threads the declarative spec surface:

      * ``spec.claude.model``  → ``--model <name>`` (when set).
      * ``spec.claude.flags``  → appended verbatim (e.g.
        ``--dangerously-skip-permissions``).

    MCP servers: the interactive ``claude`` auto-discovers ``.mcp.json``
    from the PROJECT ROOT (its cwd, ``--pwd``), NOT from ``$HOME`` like
    the SDK runner. So when to_home materialised a ``$HOME/.mcp.json``,
    the caller passes its in-container path as ``mcp_config`` and we add
    ``--mcp-config <path>`` so the TUI loads those servers (figrecipe /
    scitex-agent-container / scitex-todo …). Without this the TUI shows
    "No MCP servers configured".

    Channels (SDK parity — see ``runtimes._sdk_channels.apply_channels``):
    ``spec.claude.channels`` drives two flags. ``dev_channels`` →
    ``--dangerously-load-development-channels <set>`` (any channel).
    ``channel_mcp`` → an inline ``--mcp-config`` JSON registering the
    ``sac mcp channel`` stdio subscriber (``server:sac`` only) so the TUI
    actually receives a2a-bus pushes — the interactive ``claude`` has no
    ``--channels`` flag, so the subscriber must be injected as an MCP
    server (the bus-auth env from ``listen_env_flags`` lets it connect).
    Each mcp config rides on its OWN ``--mcp-config`` flag (P0 fix
    2026-06-15): claude's ``<configs...>`` syntax claims it accepts
    multiple space-separated values after one flag, but the binary
    silently drops everything past the first — the operator-facing
    symptom was the TUI complaining "no MCP server configured with that
    name" even though the values were passed. Repeated flags match the
    SDK runtime's pattern and are observably loaded.

    No tini wrapper: the TUI is the foreground interactive process in the
    tmux pane; apptainer + tmux own signal delivery. ``startup_commands``
    wrapping (``build_inner_argv``) still ``exec``s this as the tail.
    """
    argv: list[str] = [_CLAUDE_TUI_BIN]
    claude_spec = getattr(config, "claude", None)
    model = str(getattr(claude_spec, "model", "") or "").strip()
    if not model:
        model = str(getattr(config, "model", "") or "").strip()
    if model:
        argv += ["--model", model]
    # One ``--mcp-config`` per value (P0 fix 2026-06-15, operator-reported):
    # ``claude --help`` documents ``--mcp-config <configs...>`` as accepting
    # multiple space-separated values after a single flag, but the real
    # binary silently drops every value past the first. Symptom on the
    # failing fleet (figrecipe / todo / neurovista): the TUI pane showed
    # ``server:claude-code-telegrammer,server:sac · no MCP server
    # configured with that name`` even though the workspace ``.mcp.json``
    # path AND the inline ``sac mcp channel`` JSON were both passed.
    # Emitting one flag per value matches the SDK runtime's repeated-
    # flag pattern and is observably loaded by the TUI.
    if mcp_config:
        argv += ["--mcp-config", mcp_config]
    if channel_mcp:
        argv += ["--mcp-config", channel_mcp]
    if dev_channels:
        argv += ["--dangerously-load-development-channels", dev_channels]
    for flag in list(getattr(claude_spec, "flags", []) or []):
        flag = str(flag).strip()
        if flag:
            argv.append(flag)
    return argv


# In-SIF resolution of the bundled ``sac`` console-script for the
# channel-MCP subscriber. The bundled ``claude`` TUI spawns it as an
# stdio MCP subprocess INSIDE the SIF, so the command must point at
# sac's REAL in-SIF location — which varies by image build:
#
#   * sac-base.sif (current, verified 2026-06-17): /opt/venv-sac/bin/sac
#   * a prior build:                               /opt/venv-agent/bin/sac
#
# A single hardcoded path silently broke the channel the moment the SIF
# layout flipped: claude reported ``server:sac · no MCP server configured
# with that name`` because the subprocess ``command`` did not exist
# (2026-06-17 figrecipe/neurovista/todo — the constant had been flipped
# to ``/opt/venv-agent/bin/sac``, absent from sac-base.sif). The HOST
# cannot probe the in-SIF filesystem while building the apptainer-exec
# argv, so resolution is DEFERRED to spawn time: the MCP ``command`` is
# ``/bin/sh -c <resolver>`` that tries the known venvs + PATH inside the
# SIF and fails loud if none is executable (see :func:`_sac_channel_mcp_server`).
# Absolute candidates make it PATH-independent under ``--containall`` /
# ``--cleanenv`` where the venv bin dir may not be exported.

# Ordered absolute in-SIF candidates (current build first), mirroring the
# SDK runner's ``_sdk_channels._resolve_sac_binary`` candidate philosophy.
_SAC_BIN_IN_SIF_CANDIDATES: tuple[str, ...] = (
    "/opt/venv-sac/bin/sac",
    "/opt/venv-agent/bin/sac",
    "/usr/local/bin/sac",
)

# Back-compat single-path constant + helper. The channel MCP no longer
# consumes these (it uses the spawn-time resolver below); kept for
# external imports / single-path callers. Default corrected to the
# verified current sac-base.sif location.
_SAC_BIN_IN_SIF_DEFAULT = "/opt/venv-sac/bin/sac"


def resolve_sac_bin_in_sif() -> str:
    """Return a best-effort single in-SIF path to the ``sac`` script.

    Honours the ``SAC_BIN_IN_SIF`` env override, else returns
    :data:`_SAC_BIN_IN_SIF_DEFAULT`. NOTE: the channel-MCP subscriber no
    longer uses this — it resolves sac at SPAWN time across
    :data:`_SAC_BIN_IN_SIF_CANDIDATES` (robust to SIF venv layout); see
    :func:`_sac_channel_mcp_server`. This helper remains for
    backward-compatible imports and single-path callers.
    """
    override = _os.environ.get("SAC_BIN_IN_SIF", "").strip()
    if override:
        return override
    return _SAC_BIN_IN_SIF_DEFAULT


# Backward-compat alias — keep imports of ``_SAC_BIN_IN_SIF`` working
# (e.g. external skill-doc examples that referenced the legacy constant
# name). New call sites should call :func:`resolve_sac_bin_in_sif`.
_SAC_BIN_IN_SIF = _SAC_BIN_IN_SIF_DEFAULT


def _sac_channel_mcp_server(channel_args: list[str]) -> dict:
    """Build the stdio MCP-server spec for the ``sac mcp channel`` sub.

    The ``command`` resolves sac's absolute path INSIDE the SIF at spawn
    time — trying ``$SAC_BIN`` / ``$SAC_BIN_IN_SIF`` (operator override
    via ``spec.env``), the known venv locations
    (:data:`_SAC_BIN_IN_SIF_CANDIDATES`), then ``command -v sac`` — and
    ``exec``s the first executable, passing ``channel_args`` through
    unchanged via ``"$@"``. Fails loud (a stderr diagnostic + ``exit
    127``) when sac is absent, so a missing binary surfaces as a named
    error rather than the silent ``server:sac · no MCP server configured
    with that name`` (2026-06-17). Absolute candidates make resolution
    PATH-independent under ``--containall`` / ``--cleanenv``.

    ``/bin/sh -c <resolver> sac <channel_args...>``: ``"sac"`` becomes
    ``$0`` and ``channel_args`` become ``"$@"``, so the resolved binary
    runs ``sac mcp channel --name <agent> [...]`` exactly.
    """
    candidates = (
        '"$SAC_BIN" "$SAC_BIN_IN_SIF" '
        + " ".join(_SAC_BIN_IN_SIF_CANDIDATES)
        + ' "$(command -v sac 2>/dev/null)"'
    )
    resolver = (
        f"for c in {candidates}; do "
        'if [ -n "$c" ] && [ -x "$c" ]; then exec "$c" "$@"; fi; '
        "done; "
        ">&2 echo 'sac: binary not found in SIF; channel MCP cannot "
        "start (set SAC_BIN_IN_SIF in spec.env or rebuild the image "
        "with sac installed)'; exit 127"
    )
    return {
        "type": "stdio",
        "command": "/bin/sh",
        "args": ["-c", resolver, "sac", *channel_args],
    }


def tui_channel_config(config: "AgentConfig") -> tuple[str | None, str | None]:
    """Resolve ``spec.claude.channels`` into TUI channel flags.

    Returns ``(dev_channels, channel_mcp_json)`` — SDK parity with
    :func:`runtimes._sdk_channels.apply_channels`:

      * ``dev_channels`` — comma-joined channel set for
        ``--dangerously-load-development-channels`` (fires for ANY
        channel entry), or ``None`` when no channels are declared.
      * ``channel_mcp_json`` — inline ``--mcp-config`` JSON registering
        the ``sac mcp channel --name <agent>`` stdio subscriber under
        ``mcpServers.sac`` (``server:sac`` ONLY), or ``None``. The
        subscriber's ``--listen-url`` defaults to ``$SAC_LISTEN_BASE_URL``
        (already forwarded by ``listen_env_flags``); when the a2a port is
        resolved it also gets ``--turn-url`` for the WAKE path.
    """
    claude_spec = getattr(config, "claude", None)
    channels = [
        str(c).strip()
        for c in (getattr(claude_spec, "channels", []) or [])
        if str(c).strip()
    ]
    if not channels:
        return None, None
    dev_channels = ",".join(sorted(set(channels)))
    channel_mcp: str | None = None
    if any(c == "server:sac" for c in channels):
        args = ["mcp", "channel", "--name", config.name]
        a2a_spec = getattr(config, "a2a", None)
        port = getattr(a2a_spec, "port", None) if a2a_spec else None
        if isinstance(port, int) and port > 0:
            args += ["--turn-url", f"http://127.0.0.1:{port}/v1/turn"]
        channel_mcp = json.dumps({"mcpServers": {"sac": _sac_channel_mcp_server(args)}})
    return dev_channels, channel_mcp


def _proxy_runner_argv(config: "AgentConfig") -> list[str]:
    """Argv tail for ``kind: AgentProxy`` (a2a_proxy).

    Reads spec.proxy.* (upstream / trust / redact / timeout_s) and
    spec.a2a.port (sidecar bind). No --mission / autonomous — the
    proxy has no SDK conversation.
    """
    proxy = getattr(config, "proxy", None)
    upstream = getattr(proxy, "upstream", "") if proxy else ""
    trust = getattr(proxy, "trust", "untrusted") if proxy else "untrusted"
    redact = list(getattr(proxy, "redact", []) or []) if proxy else []
    timeout_s = getattr(proxy, "timeout_s", 30.0) if proxy else 30.0

    runner_argv: list[str] = [
        "--name",
        config.name,
        "--state-root",
        "/state",
        "--upstream",
        str(upstream),
        "--trust",
        str(trust),
        "--redact",
        ",".join(redact),
        "--timeout-s",
        str(timeout_s),
    ]
    a2a_spec = getattr(config, "a2a", None)
    a2a_port = getattr(a2a_spec, "port", None) if a2a_spec else None
    # See _agent_runner_argv for resolved-int rationale.
    if isinstance(a2a_port, int) and a2a_port > 0:
        runner_argv += ["--a2a-port", str(a2a_port)]
        cfg_path = getattr(config, "config_path", "")
        if cfg_path:
            runner_argv += ["--a2a-card-yaml", str(cfg_path)]
    return runner_argv


__all__ = [
    "RUNNER_MODULE_AGENT",
    "RUNNER_MODULE_PROXY",
    "build_inner_argv",
]
