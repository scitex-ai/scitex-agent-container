"""Inner-command argv builders for ``ApptainerContainerRuntime``.

Factored out of ``_apptainer_runtime.py`` so adding new ``kind``
dispatch branches doesn't push the parent module past the 512-line
cap. Each builder returns the full ``[tini, --, python3, -m, MODULE,
*args]`` list for one ``kind``.

The interactive-TUI argv/channel-plan helpers (``_tui_runner_argv``,
``tui_channel_plan``, ``tui_channel_config``, ``resolve_sac_bin_in_sif``,
``_home_has_resumable_conversation``, ...) live in
``_apptainer_inner_argv_tui`` (split out 2026-07-05 to stay under the
512-line cap) and are re-imported below so every existing
``from scitex_agent_container.runtimes._apptainer_inner_argv import X``
keeps working unchanged.
"""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from ..config._harness_registry import (
    CLAUDE_AGENT_SDK,
    CLAUDE_CODE_TUI,
    HARNESS_DESCRIPTORS,
    OPENAI_AGENTS,
)
from ..config._harness_types import ensure_harness_matches_claude_launch
from ..config._residency_types import DEFAULT_AGENT_RESIDENCY
from ._apptainer_inner_argv_tui import (  # noqa: F401 (re-export)
    _home_has_resumable_conversation,
    _tui_runner_argv,
    resolve_sac_bin_in_sif,
    tui_channel_config,
    tui_channel_plan,
)

if TYPE_CHECKING:
    from ..config import AgentConfig

# Runner-module names, DERIVED from the harness registry (v4 step 4,
# ``config._harness_registry``) — the registry entry is the single
# source for each module path; these constants remain because they are
# imported by tests and sibling modules.
RUNNER_MODULE_AGENT = HARNESS_DESCRIPTORS[CLAUDE_AGENT_SDK].runner_module

# OpenAI harness runner module (scitex-todo card ``openai-compat-2``).
# NOT DISPATCHED from here YET: the v4 step-2 refusal
# (``ensure_harness_matches_claude_launch``) guards every branch below,
# so a non-Anthropic harness raises instead of dispatching. The
# registry's ``openai-agents`` entry carries the REAL argv builder;
# key-based launch is migration step 7
# (card ``sac-v4-layering-refactor-harness-runtime-inference-20260813``).
RUNNER_MODULE_OPENAI = HARNESS_DESCRIPTORS[OPENAI_AGENTS].runner_module

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


# Generic, values-agnostic env alias — always prepended as the FIRST
# shell step (see build_inner_argv), regardless of whether the agent
# declares any spec.startup_commands. This does not know or care where
# SAC_GIT_* came from: a project's own ``.envrc`` (direnv ``source_up`` to
# e.g. ``~/proj/.envrc``) is the actual current source, entirely outside
# this module's concern. It only mirrors whichever of the 5 SAC_GIT_* vars
# are ALREADY present in the shell env (by whatever means, once direnv has
# fired via the login shell's /etc/bash.bashrc hook) onto the literal
# ``GIT_*`` names git itself reads — because git has no concept of a
# ``SAC_`` prefix, while the operator wants the canonical/introspectable
# source-of-truth names to carry it (``printenv | grep SAC_`` shows every
# sac-relevant var in one shot). ``${VAR:-}`` + ``-n`` guards make an unset
# SAC_GIT_* a silent no-op — it never overwrites an already-correct GIT_*
# with an empty string. These lines run BEFORE any ``set -e`` that
# _format_shell_steps below may emit, so a false ``[ -n ... ]`` (unset var)
# never aborts the launch.
_GIT_ENV_ALIAS_STEPS: list[str] = [
    '[ -n "${SAC_GIT_AUTHOR_NAME:-}" ] && export GIT_AUTHOR_NAME="$SAC_GIT_AUTHOR_NAME"',
    '[ -n "${SAC_GIT_AUTHOR_EMAIL:-}" ] && export GIT_AUTHOR_EMAIL="$SAC_GIT_AUTHOR_EMAIL"',
    '[ -n "${SAC_GIT_COMMITTER_NAME:-}" ] && export GIT_COMMITTER_NAME="$SAC_GIT_COMMITTER_NAME"',
    '[ -n "${SAC_GIT_COMMITTER_EMAIL:-}" ] && export GIT_COMMITTER_EMAIL="$SAC_GIT_COMMITTER_EMAIL"',
    '[ -n "${SAC_GIT_SSH_COMMAND:-}" ] && export GIT_SSH_COMMAND="$SAC_GIT_SSH_COMMAND"',
]


def hook_floor_gate_steps(config: "AgentConfig") -> "list[str]":
    """In-container step that REFUSES the launch when a declared hook floor is unmet.

    Empty for a spec that declares no ``required_claude_hooks`` — most specs
    today. No step, no refusal, no line to scroll past: the declaration IS the
    opt-in.

    Why the check lives HERE and not in ``agent_start``: an agent's effective
    hook set is the union of two stacked mounts over ``/home/agent``, and every
    host-side proxy for that union has UNDERCOUNTED. Measured 2026-08-10 for
    ``scitex-agent-container``, the layer-1 read reported 67 pre-tool-use hooks
    and called ``log_post_tool_use.sh`` missing; the same listing INSIDE that
    container returned 71 and the hook was there. ``agent_start`` runs on the
    bare host, where the only ``$HOME`` it can read is the operator's — so a
    gate placed there would answer confidently about the wrong machine. This
    step runs after the mounts exist and before ``exec``, which is the only
    place the guarantee is measurable.

    A non-zero exit aborts the ``bash -lc`` chain BEFORE ``exec``, so the
    container dies, ``runtime.start`` returns False, and the existing
    ``_start_failure_diag`` path carries the refusal text (which the command
    prints to stderr) back to the operator. ``SAC_ALLOW_MISSING_HOOKS=1`` in
    ``spec.env`` is the named override; it is honoured by the command itself,
    so nothing about this step needs to know the policy.

    ``|| exit 1`` is explicit rather than relying on ``set -e``: the git-alias
    steps run BEFORE any ``set -e`` (see ``_GIT_ENV_ALIAS_STEPS``), and a spec
    with no ``startup_commands`` never emits one at all.
    """
    if getattr(config, "required_claude_hooks", None) is None:
        return []
    sac_bin = resolve_sac_bin_in_sif()
    name = str(getattr(config, "name", "") or "")
    cmd = " ".join(shlex.quote(p) for p in [sac_bin, "agents", "hooks", name])
    return [f"{cmd} || exit 1"]


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
    tui_settings: str | None = None,
) -> list[str]:
    """Return the apptainer-inner argv. Dispatches on ``config.kind``.

    The argv is now ALWAYS wrapped in
    ``[/bin/bash, -lc, "<git-env-alias>; [set -e; <cmd1>; sleep N; <cmd2>;]
    exec <tini ...>"]`` — never returned bare. The first steps are the
    fixed, unconditional :data:`_GIT_ENV_ALIAS_STEPS` (see there); when
    ``spec.startup_commands`` is also non-empty those follow next, run as
    container-internal shell BEFORE the SDK process starts. ``exec``
    replaces bash with tini, keeping PID 1 clean. NOT a claude prompt — see
    ``spec.startup_prompts``.

    ``tui=True`` selects the interactive ``claude`` TUI as the inner
    process instead of the ``python -m`` SDK session runner (see
    :func:`_tui_runner_argv`). The startup_commands wrapper still
    applies, so container-internal bootstrap (uv venv, symlinks, ...)
    runs before ``exec claude`` identically to the SDK path.

    ``tui_settings`` is the in-container path of the materialised
    ``$HOME/.claude/settings.json`` (skip-permissions + SAC channel hooks +
    the ``_shared`` baseline honest-grounding Stop gate / lint PostToolUse,
    deep-merged by ``settings_json.setup_settings_json(..., filename=
    "settings.json")``). The interactive TUI reads it at USER scope by
    discovery — no flag needed; we thread it to ``--settings`` only as
    belt-and-suspenders (the flag is a no-op for the interactive TUI, which
    is why the file MUST be ``settings.json``, not ``settings.local.json``:
    there is no ``.local.json`` at user scope).

    Harness guard (v4 step 2, card ``sac-v4-layering-refactor-harness-
    runtime-inference-20260813``): both the TUI and SDK branches launch
    the CLAUDE harness, so a non-Anthropic ``config.harness`` REFUSES
    loudly instead of dispatching. The old ``getattr(config, "provider",
    None)`` selector here was DEAD (the harness rename removed the
    field), so ``harness: openai`` specs silently got the Claude runner;
    dispatching ``RUNNER_MODULE_OPENAI`` for real is step 4 (the
    descriptor registry). ``kind: AgentProxy`` is exempt — the a2a proxy
    runner is vendor-neutral.
    """
    kind = getattr(config, "kind", "Agent")
    if tui:
        # Same v4 step-2 guard as the SDK branch below: the interactive
        # claude TUI is just as wrong a vendor for a non-Anthropic
        # harness (and this branch never had even the dead check).
        ensure_harness_matches_claude_launch(
            config, launching="the interactive claude TUI"
        )
        # v4 step 4: the registry entry owns the argv shape. The entry is
        # keyed by the caller's already-decided launch mode (``tui=True``
        # came from TuiSessionRuntime), never re-derived from the config —
        # direct/dry-run callers pass configs whose ``runtime`` field this
        # builder must not second-guess.
        runner_tail = HARNESS_DESCRIPTORS[CLAUDE_CODE_TUI].inner_argv(
            config,
            {
                "tui_mcp_config": tui_mcp_config,
                "tui_channel_mcp": tui_channel_mcp,
                "tui_dev_channels": tui_dev_channels,
                "tui_settings": tui_settings,
            },
        )
    elif kind == "AgentProxy":
        runner_tail = _TINI_PREFIX + [RUNNER_MODULE_PROXY] + _proxy_runner_argv(config)
    else:
        # v4 step-2 loudness: refuse a wrong-vendor launch on the REAL
        # field (the dead ``config.provider`` read used to sit here and
        # silently fell through to the Claude runner). Post-guard the
        # harness is Anthropic-family, so the SDK entry is the only
        # runner-hosted candidate; key-based launch of other entries is
        # migration step 7.
        ensure_harness_matches_claude_launch(
            config, launching=f"runner module {RUNNER_MODULE_AGENT!r}"
        )
        runner_tail = HARNESS_DESCRIPTORS[CLAUDE_AGENT_SDK].inner_argv(
            config, {"one_shot": one_shot}
        )

    startup_cmds = list(getattr(config, "startup_commands", []) or [])
    # Alias step is unconditional (every agent gets it); the hook-floor gate
    # follows for a spec that declares one; startup_cmds steps follow those
    # when present. This means shell_steps is NEVER empty, so the bash -lc wrap
    # now ALWAYS happens — see docstring.
    #
    # The gate is placed BEFORE startup_commands deliberately: an agent whose
    # guard hooks are missing must not run the spec's own bootstrap either.
    shell_steps = (
        _GIT_ENV_ALIAS_STEPS
        + hook_floor_gate_steps(config)
        + _format_shell_steps(startup_cmds)
    )

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


def _a2a_argv(config: "AgentConfig") -> list[str]:
    """``spec.a2a`` -> the session runner's ``--a2a-*`` flags.

    Shared by both ``kind`` branches so the two can never drift apart.

    ``--a2a-port`` is the port the runner's inbound-turn server listens on;
    without it the sidecar never binds and POST /v1/turn is unreachable.
    ``--a2a-host`` is that server's BIND ADDRESS, threaded from
    ``spec.a2a.host``. Until this was added the builder emitted only the port,
    so the runner fell back to its own ``--a2a-host`` default and the address
    the spec declared reached exactly one of the three bind paths
    (``runtimes/a2a_sidecar.py``). A spec asking for a reachable address
    therefore produced an agent whose spec and runtime disagreed, with nothing
    reporting the disagreement. The value flows
    ``--a2a-host`` -> ``_session_cli.main`` -> ``claude_session.run(a2a_host=)``
    -> ``_session_http.serve_inbound(host=)`` -> ``uvicorn.Config(host=)``.

    Resolved-int port only: ``"auto"`` strings or None mean no sidecar arg at
    this layer. The lifecycle resolves ``"auto"`` -> int via port_allocator
    BEFORE we get here; if a string slipped through, it's a config that
    bypassed agent_start (e.g. dry-run inspection) and the sidecar simply
    won't be wired up. The host rides WITH the port for that same reason: a
    bind address without a port binds nothing.

    A blank / missing / non-string host emits NO ``--a2a-host`` at all, leaving
    the runner's own flag default in charge — so a spec that declares nothing
    binds exactly where it bound before.
    """
    a2a_spec = getattr(config, "a2a", None)
    a2a_port = getattr(a2a_spec, "port", None) if a2a_spec else None
    if not (isinstance(a2a_port, int) and a2a_port > 0):
        return []
    argv = ["--a2a-port", str(a2a_port)]
    a2a_host = getattr(a2a_spec, "host", None)
    if isinstance(a2a_host, str) and a2a_host.strip():
        argv += ["--a2a-host", a2a_host.strip()]
    cfg_path = getattr(config, "config_path", "")
    if cfg_path:
        # Spec path is host-side; apptainer auto-binds /home so the
        # in-container path is the same string. Used to publish
        # /.well-known/agent-card.json.
        argv += ["--a2a-card-yaml", str(cfg_path)]
    return argv


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
    # spec.residency (v4 step 6) → --residency. Emitted only when the
    # compiled spec declares the NON-default ("one-shot"): an agent that
    # declares nothing keeps a byte-identical argv, so a container whose
    # installed runner predates the flag still boots — an opt-in one-shot
    # agent needs the new runner anyway for the behaviour to exist, and
    # the runner's argparse refuses the flag loudly on an old build
    # instead of silently staying resident.
    residency = str(getattr(config, "residency", "") or "").strip()
    if residency and residency != DEFAULT_AGENT_RESIDENCY:
        runner_argv += ["--residency", residency]
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
    # spec.a2a.{port,host} → --a2a-port / --a2a-host (the sidecar bind).
    runner_argv += _a2a_argv(config)
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
    spec.a2a.{port,host} (the sidecar bind). No --mission / autonomous —
    the proxy has no SDK conversation.
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
    runner_argv += _a2a_argv(config)
    return runner_argv


__all__ = [
    "RUNNER_MODULE_AGENT",
    "RUNNER_MODULE_OPENAI",
    "RUNNER_MODULE_PROXY",
    "build_inner_argv",
    "hook_floor_gate_steps",
]
