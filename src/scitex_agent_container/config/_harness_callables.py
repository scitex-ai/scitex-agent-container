"""Per-entry behaviour hooks for :mod:`config._harness_registry`.

Extracted from the registry module when the FOURTH harness (``codex-sdk``,
card ``sac-codex-python-sdk-harness-20260814``) pushed it past the repo's
512-line cap. The split is the cap's own instruction — one cohesive
responsibility per file — and it separates two things that were only
ever co-located:

* the REGISTRY (``_harness_registry``): the keys, the frozen
  :class:`~._harness_registry.HarnessDescriptor`, the
  ``HARNESS_DESCRIPTORS`` table, resolution and the derivation helpers;
* the CALLABLES (this module): the ``inner_argv`` / ``env_and_binds`` /
  ``prepare_home`` functions the entries point at, plus the
  runner-module path constants they are built from.

WHAT THIS MODULE DOES NOT CHANGE: every ``runtimes`` import inside these
functions stays CALL-TIME only. The package's one-directional import
rule is that ``runtimes`` imports ``config``, never the reverse at module
level; the functions moved here verbatim and keep that property. They are
module-level ``def``s (not lambdas) for the same reason as before — so
tests and tracebacks can name them.

The functions are private by convention (leading underscore) because the
registry entries are their only intended callers; they are imported by
name rather than re-exported wholesale so an unused hook shows up as a
lint error rather than dead weight.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover — typing only, no runtime import cycle
    from pathlib import Path

    from ._types import AgentConfig

__all__ = [
    "CLAUDE_SESSION_RUNNER",
    "CODEX_SESSION_RUNNER",
    "OPENAI_SESSION_RUNNER",
]


# ---------------------------------------------------------------------------
# Runner-module paths (the ``python -m`` targets). Fed into the registry
# entries and derived FROM them by ``runtimes/_apptainer_inner_argv`` /
# ``_apptainer_build_argv`` (their ``RUNNER_MODULE*`` re-exports).
# ---------------------------------------------------------------------------

CLAUDE_SESSION_RUNNER = "scitex_agent_container._runners.claude_session"
OPENAI_SESSION_RUNNER = "scitex_agent_container._runners.openai_session"
CODEX_SESSION_RUNNER = "scitex_agent_container._runners.codex_session"


# ---------------------------------------------------------------------------
# prepare_home
# ---------------------------------------------------------------------------


def _noop_prepare_home(config: "AgentConfig") -> None:
    """Default ``prepare_home`` — nothing beyond the shared machinery.

    Home materialisation (``to_home`` deployment, CLAUDE.md management)
    still lives in the runtime adapters (``ClaudeSessionRuntime`` /
    ``OpenAISessionRuntime`` / ``TuiSessionRuntime`` ``_setup_workspace``)
    today; harnesses hook per-harness extras here in a later step.
    """


# ---------------------------------------------------------------------------
# inner_argv
# ---------------------------------------------------------------------------


def _claude_tui_inner_argv(
    config: "AgentConfig", options: "Mapping[str, object] | None" = None
) -> list[str]:
    """Inner argv for the interactive Claude Code TUI (pre-shell-wrap)."""
    options = options or {}
    from ..runtimes._apptainer_inner_argv_tui import _tui_runner_argv

    return _tui_runner_argv(
        config,
        mcp_config=options.get("tui_mcp_config"),  # type: ignore[arg-type]
        channel_mcp=options.get("tui_channel_mcp"),  # type: ignore[arg-type]
        dev_channels=options.get("tui_dev_channels"),  # type: ignore[arg-type]
        settings=options.get("tui_settings"),  # type: ignore[arg-type]
    )


def _session_runner_inner_argv(
    config: "AgentConfig", module: str, options: "Mapping[str, object] | None"
) -> list[str]:
    """Shared ``tini → python -m <module>`` tail for runner-hosted entries.

    All THREE runner-hosted SDK families take the SAME argv surface — the
    shared session-daemon flags (``--name`` / ``--state-root`` /
    supervisor caps / ``--mission`` / ``--a2a-*`` / ``--channels`` /
    ``--residency`` / autonomous) that every one of their CLIs threads
    into ``run_session_daemon`` (v4 step 7) — so the builder is shared
    and only the module differs.
    """
    options = options or {}
    from ..runtimes._apptainer_inner_argv import _TINI_PREFIX, _agent_runner_argv

    return (
        list(_TINI_PREFIX)
        + [module]
        + _agent_runner_argv(config, one_shot=bool(options.get("one_shot", False)))
    )


def _claude_sdk_inner_argv(
    config: "AgentConfig", options: "Mapping[str, object] | None" = None
) -> list[str]:
    """Inner argv for the headless ``claude-agent-sdk`` session runner."""
    return _session_runner_inner_argv(config, CLAUDE_SESSION_RUNNER, options)


def _openai_agents_inner_argv(
    config: "AgentConfig", options: "Mapping[str, object] | None" = None
) -> list[str]:
    """Inner argv for the ``openai-agents`` session runner.

    The runner itself is daemon-hosted since v4 step 7 (its CLI hands
    the process to ``run_session_daemon`` with the OpenAI turn driver),
    but the step-2 refusal (``ensure_harness_matches_claude_launch``)
    still guards every LAUNCH path — nothing dispatches this argv until
    the canary step lifts that guard.
    """
    return _session_runner_inner_argv(config, OPENAI_SESSION_RUNNER, options)


def _codex_sdk_inner_argv(
    config: "AgentConfig", options: "Mapping[str, object] | None" = None
) -> list[str]:
    """Inner argv for the ``openai-codex`` (Codex SDK) session runner.

    Takes the SAME shared runner argv as the other two runner-hosted
    entries — the codex session CLI accepts every flag for real (it
    hands the process to ``run_session_daemon`` exactly like the
    claude/openai runners do). ``harness: codex`` with ``runtime: headless``
    selects this argv through the lifecycle registry.
    """
    return _session_runner_inner_argv(config, CODEX_SESSION_RUNNER, options)


def _codex_tui_inner_argv(
    config: "AgentConfig", options: "Mapping[str, object] | None" = None
) -> list[str]:
    """Inner argv for the interactive ``codex`` TUI (pre-shell-wrap).

    Takes the SAME option keys as the Claude TUI entry (the workspace
    ``.mcp.json`` path and the inline channel-subscriber JSON) so the
    TUI branch of ``build_inner_argv`` dispatches both entries alike.
    """
    options = options or {}
    from ..runtimes._apptainer_inner_argv_codex import codex_tui_argv

    return codex_tui_argv(
        config,
        mcp_config=options.get("tui_mcp_config"),  # type: ignore[arg-type]
        channel_mcp=options.get("tui_channel_mcp"),  # type: ignore[arg-type]
        settings=options.get("tui_settings"),  # type: ignore[arg-type]
    )


def _hermes_tui_inner_argv(
    config: "AgentConfig", options: "Mapping[str, object] | None" = None
) -> list[str]:
    """Inner argv for Hermes' official Ink TUI as the session owner.

    Hermes does not reliably select the profile's default backend before a
    session exists.  Pin the same resolved model/provider pair materialized in
    ``~/.hermes/config.yaml`` so fresh sessions do not fall into Setup Required.
    """
    del options
    from ..runtimes._hermes_context_gc import DEFAULT_MAX_SESSION_AGE_MINUTES

    configured_max_age = config.claude.continue_max_age_minutes
    max_age = min(
        configured_max_age or DEFAULT_MAX_SESSION_AGE_MINUTES,
        DEFAULT_MAX_SESSION_AGE_MINUTES,
    )
    argv = [
        "/usr/bin/tini",
        "-s",
        "--",
        "python3",
        "-m",
        "scitex_agent_container.runtimes._hermes_tui_owner",
        "--state-dir",
        f"/state/{config.name}",
        "--max-session-age-minutes",
        str(max_age),
        "--",
        "hermes",
        "chat",
        "--tui",
        "--in",
        str(config.workdir),
        "--pass-session-id",
    ]
    model = str(config.model or "").strip()
    engine_key = str(config.engine_key or "").strip()
    if not model or not engine_key:
        raise ValueError(
            "Hermes TUI requires a resolved engine model and key; refusing "
            "to launch without a resolved engine"
        )
    # NOTE: no explicit --model/--provider flags. The generated hermes
    # config.yaml already selects this model as default, and the explicit
    # override path is what trips Hermes' data-training-tier guard in
    # non-interactive runs (config-default needs no confirmation).
    session_mode = str(config.claude.session or "").strip().lower()
    if session_mode == "continue":
        from ._hermes_session import hermes_session_key

        argv += [
            "--continue",
            hermes_session_key(config.name, engine_key),
            "--create-if-missing",
        ]
    elif session_mode == "resume":
        resume_id = str(config.claude.resume_id or "").strip()
        if not resume_id:
            raise ValueError(
                "Hermes session mode 'resume' requires spec.claude.resume_id "
                "or the CLI --resume <session-id>; refusing to degrade to a "
                "fresh session"
            )
        argv += ["--resume", resume_id]
    prompts = [str(value) for value in config.startup_prompts if str(value).strip()]
    if prompts:
        argv += ["--query", "\n\n".join(prompts)]
    return argv


# ---------------------------------------------------------------------------
# env_and_binds
# ---------------------------------------------------------------------------


def _claude_env_and_binds(config: "AgentConfig", state_dir: "Path") -> list[str]:
    """Auth env + creds-bind flags for the Claude-family entries.

    Delegates to :func:`runtimes._apptainer_auth.auth_argv` — the real
    builder both the TUI and SDK launches already flow through (OAuth
    creds bind, or ``spec.claude.provider`` API-key backend).
    """
    from ..runtimes._apptainer_auth import auth_argv

    return auth_argv(config, state_dir)


def _openai_env_and_binds(config: "AgentConfig", state_dir: "Path") -> list[str]:
    """Auth env flags for the ``openai-agents`` entry (no creds bind).

    Delegates to :func:`runtimes._apptainer_provider.openai_env_flags` —
    OPENAI_* key injection + routing pass-throughs; declines (returns
    ``[]``) when the launch does not resolve to the openai harness.
    """
    del state_dir  # the openai path mounts no credentials file
    from ..runtimes._apptainer_provider import openai_env_flags

    return openai_env_flags(config)


def _codex_env_and_binds(config: "AgentConfig", state_dir: "Path") -> list[str]:
    """Auth env + ``CODEX_HOME`` bind flags for the ``codex-sdk`` entry.

    Delegates to :func:`runtimes._apptainer_codex_env.codex_env_flags`.
    Unlike the openai entry this one DOES mount credentials: the Codex
    SDK reuses the ``codex`` CLI's own auth (``$CODEX_HOME/auth.json``,
    default ``~/.codex``) alongside its ``config.toml`` model-provider
    routing, so the directory is bind-mounted rather than reduced to an
    API-key env var. Declines (returns ``[]``) when the launch does not
    resolve to the codex harness.
    """
    from ..runtimes._apptainer_codex_env import codex_env_flags

    return codex_env_flags(config, state_dir)


def _hermes_profile_env_argv(state_dir: "Path") -> list[str]:
    """Expose the materialized owner-only Hermes env without argv secrets."""
    profile_env = state_dir / "home" / ".hermes" / ".env"
    if not profile_env.is_file():
        raise RuntimeError(
            "Hermes profile env is absent; materialize the Hermes workspace "
            f"before building its container argv: {profile_env}"
        )
    mode = profile_env.stat().st_mode & 0o777
    if mode & 0o077:
        raise RuntimeError(
            f"Hermes profile env {profile_env} has unsafe mode {mode:#o}; "
            "expected no group/world permissions"
        )
    return ["--env-file", str(profile_env)]


def _hermes_env_and_binds(config: "AgentConfig", state_dir: "Path") -> list[str]:
    """Expose the isolated Hermes profile, env, and engine provenance.

    Hermes imports config-reading modules before its CLI-level dotenv loader.
    A generated MCP entry containing ``${env:NAME}`` can therefore be parsed
    while NAME is still absent, leaving the literal placeholder in the child
    server environment for the entire session. Apptainer must load the same
    owner-only profile env before the Hermes process starts. ``--env-file``
    carries only a path in argv, so provider keys and the SAC listen bearer
    remain absent from the world-readable process command line.
    """
    from ..runtimes._apptainer_provider import engine_env_flags

    return (
        ["--env", "HERMES_HOME=/home/agent/.hermes"]
        + _hermes_profile_env_argv(state_dir)
        + engine_env_flags(config)
    )
