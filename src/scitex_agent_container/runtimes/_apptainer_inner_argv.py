"""Inner-command argv builders for ``ApptainerContainerRuntime``.

Factored out of ``_apptainer_runtime.py`` so adding new ``kind``
dispatch branches doesn't push the parent module past the 512-line
cap. Each builder returns the full ``[tini, --, python3, -m, MODULE,
*args]`` list for one ``kind``.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import AgentConfig

# Runner-module dispatch by ``config.kind``. Kept here so the parent
# orchestrator doesn't need to know either runner's module path.
RUNNER_MODULE_AGENT = "scitex_agent_container._runners.claude_session"
RUNNER_MODULE_PROXY = "scitex_agent_container._runners.a2a_proxy"

_TINI_PREFIX = ["/usr/bin/tini", "-s", "--", "python3", "-m"]


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


def build_inner_argv(config: "AgentConfig", *, one_shot: bool = False) -> list[str]:
    """Return the apptainer-inner argv. Dispatches on ``config.kind``.

    When ``spec.startup_commands`` is non-empty, the argv is wrapped
    in ``[/bin/bash, -lc, "set -e; <cmd1>; sleep N; <cmd2>; exec <tini ...>"]``
    so the commands run as container-internal shell BEFORE the claude
    SDK process starts. ``exec`` replaces bash with tini, keeping PID 1
    clean. NOT a claude prompt — see ``spec.startup_prompts``.
    """
    kind = getattr(config, "kind", "Agent")
    if kind == "AgentProxy":
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


def _agent_runner_argv(config: "AgentConfig", *, one_shot: bool) -> list[str]:
    """Argv tail for ``kind: Agent`` (claude_session)."""
    runner_argv: list[str] = [
        "--name",
        config.name,
        "--state-root",
        "/state",
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
