"""Inner-command argv builders for ``ApptainerContainerRuntime``.

Factored out of ``_apptainer_runtime.py`` so adding new ``kind``
dispatch branches doesn't push the parent module past the 512-line
cap. Each builder returns the full ``[tini, --, python3, -m, MODULE,
*args]`` list for one ``kind``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..config import AgentConfig

# Runner-module dispatch by ``config.kind``. Kept here so the parent
# orchestrator doesn't need to know either runner's module path.
RUNNER_MODULE_AGENT = "scitex_agent_container._runners.claude_session"
RUNNER_MODULE_PROXY = "scitex_agent_container._runners.a2a_proxy"

_TINI_PREFIX = ["/usr/bin/tini", "-s", "--", "python3", "-m"]


def build_inner_argv(config: "AgentConfig", *, one_shot: bool = False) -> list[str]:
    """Return ``[tini, --, python3, -m, RUNNER_MODULE, *runner_argv]``.

    Dispatches on ``config.kind``:
      * ``Agent`` (default)  → claude_session runner argv.
      * ``AgentProxy``       → a2a_proxy runner argv.
    """
    kind = getattr(config, "kind", "Agent")
    if kind == "AgentProxy":
        return _TINI_PREFIX + [RUNNER_MODULE_PROXY] + _proxy_runner_argv(config)
    return (
        _TINI_PREFIX
        + [RUNNER_MODULE_AGENT]
        + _agent_runner_argv(config, one_shot=one_shot)
    )


def _agent_runner_argv(config: "AgentConfig", *, one_shot: bool) -> list[str]:
    """Argv tail for ``kind: Agent`` (claude_session) — unchanged from v3 wiring."""
    runner_argv: list[str] = [
        "--name",
        config.name,
        "--state-root",
        "/state",
    ]
    # startup_prompts (v3) override startup_commands (legacy) for the
    # mission turn — the runner's --mission flag drives ONE SDK turn.
    mission = ""
    prompts = list(getattr(config, "startup_prompts", []) or [])
    if prompts:
        mission = str(prompts[0]).strip()
    else:
        cmds = list(getattr(config, "startup_commands", []) or [])
        if cmds and getattr(cmds[0], "command", ""):
            mission = cmds[0].command
    if mission:
        runner_argv += ["--mission", mission]
        if one_shot:
            # one-shot semantics → exit after the first SDK turn.
            runner_argv.append("--print-stream")
    # spec.a2a.port → --a2a-port (sidecar bind). Without this the
    # sidecar never binds and POST /v1/turn is unreachable.
    a2a_spec = getattr(config, "a2a", None)
    a2a_port = getattr(a2a_spec, "port", None) if a2a_spec else None
    if a2a_port:
        runner_argv += ["--a2a-port", str(a2a_port)]
        cfg_path = getattr(config, "config_path", "")
        if cfg_path:
            # Spec path is host-side; apptainer auto-binds /home so
            # the in-container path is the same string. Used to
            # publish /.well-known/agent-card.json.
            runner_argv += ["--a2a-card-yaml", str(cfg_path)]
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
    if a2a_port:
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
