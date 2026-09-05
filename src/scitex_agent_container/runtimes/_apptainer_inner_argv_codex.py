"""Inner argv for the interactive ``codex`` TUI (``spec.harness: codex``).

WHY THIS EXISTS (2026-09-05, operator ruling): the fleet prepares to leave
Claude Code for the Codex CLI, gradually, over the OpenAI protocol that the
Qwen server speaks natively. 107 of 119 fleet specs run their harness as an
interactive TUI in a tmux pane, so the codex harness needs the same shape —
this module renders what that pane runs.

TWO HALVES, and the split is deliberate:

* HOST side (this module): the static Codex config overrides, rendered from
  the spec — the model provider (``spec.engines.<key>.provider`` /
  ``spec.claude.provider``: ``base_url`` + the key's env NAME), the model,
  the sandbox, the context window and the session pin. Every override is a
  ``-c key=value`` flag — Codex reads its config from ``config.toml`` and
  from ``-c`` overrides ONLY (``OPENAI_BASE_URL`` is silently ignored;
  measured against the 0.153 binary), so no file has to be written into
  the agent's home and nothing is left behind on a harness switch.
* CONTAINER side (:mod:`._apptainer_codex_exec`): the shim the pane
  actually execs. It resolves the ``codex`` binary bundled in the image's
  own venv (``codex_cli_bin.bundled_codex_path``) — resolving it on the
  host would bake a host path into the argv — and translates the MCP
  server files sac materialises for the Claude TUI (``~/.mcp.json`` plus
  the inline channel-subscriber JSON) into ``-c mcp_servers.*`` overrides,
  because those files exist only inside the container.

MEASURED FACTS THE FLAGS REST ON (codex-cli 0.147.0 in the image, 0.153.4
from npm; the fleet card carries the evidence):

* ``wire_api``'s only accepted value is ``"responses"``; ``"chat"`` fails
  config load. vLLM 0.22.0 serves ``/v1/responses`` natively, and the
  scitex-genai gateway relays it since #42.
* ``env_key`` is enough for a custom provider: no ChatGPT OAuth, no
  ``auth.json``, no ``codex login``.
* The sandbox is bubblewrap, which cannot nest inside apptainer: with the
  default sandbox every tool call exits 1 WHILE THE MODEL REPORTS SUCCESS.
  The container is already the boundary, so the sandbox is off here.
* ``model_context_window`` and ``model_reasoning_effort`` are real config
  keys in this version (27 and 26 occurrences in the binary);
  ``model_max_output_tokens`` is not.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

from ..config._session_continuity import SESSION_RESUME, wants_continue
from ._apptainer_provider import ProviderEnvError, _provider_spec

if TYPE_CHECKING:
    from ..config import AgentConfig

__all__ = [
    "CODEX_EXEC_MODULE",
    "CODEX_KEY_ENV",
    "CODEX_PROVIDER_ID",
    "codex_config_overrides",
    "codex_tui_argv",
]

#: The in-container shim that resolves the binary and the MCP overrides.
CODEX_EXEC_MODULE = "scitex_agent_container.runtimes._apptainer_codex_exec"

#: The ``[model_providers.<id>]`` entry the overrides declare and select.
CODEX_PROVIDER_ID = "sac"

#: The env var Codex reads the provider key from (``env_key``). sac puts the
#: engine's resolved key under this name — see
#: :func:`._apptainer_codex_env.codex_provider_key_flags`.
CODEX_KEY_ENV = "SAC_CODEX_API_KEY"

#: The container is the boundary; Codex's own bubblewrap sandbox cannot nest
#: inside apptainer (measured: tool calls exit 1, the model reports success).
_SANDBOX = "danger-full-access"


def _toml(value: object) -> str:
    """A TOML literal for a string / int / bool — JSON's are valid TOML here."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    return json.dumps(str(value))


def _override(key: str, value: object) -> list[str]:
    return ["-c", f"{key}={_toml(value)}"]


def codex_config_overrides(config: AgentConfig) -> list[str]:
    """The static ``-c`` overrides for one agent, rendered from its spec."""
    provider = _provider_spec(config)
    base_url = str(getattr(provider, "base_url", "") or "").strip().rstrip("/")
    if not base_url:
        raise ProviderEnvError(
            f"spec.harness: codex on agent {config.name!r} needs an inference "
            "provider (spec.engines.<key>.provider with base_url + "
            "auth_token_env, or spec.claude.provider): Codex reads its model "
            "provider from config only, and sac refuses to point it at the "
            "OpenAI-hosted default by silence."
        )
    claude = getattr(config, "claude", None)
    model = str(
        getattr(claude, "model", "") or getattr(config, "model", "") or ""
    ).strip()
    if not model:
        raise ProviderEnvError(
            f"spec.harness: codex on agent {config.name!r} names no model "
            "(spec.engines.<key>.model / spec.claude.model) — Codex would fall "
            "back to its OpenAI-hosted default silently."
        )
    pid = CODEX_PROVIDER_ID
    flags: list[str] = []
    flags += _override("model_provider", pid)
    flags += _override(f"model_providers.{pid}.name", "scitex-genai gateway")
    flags += _override(f"model_providers.{pid}.base_url", f"{base_url}/v1")
    flags += _override(f"model_providers.{pid}.wire_api", "responses")
    flags += _override(f"model_providers.{pid}.env_key", CODEX_KEY_ENV)
    flags += _override(f"model_providers.{pid}.requires_openai_auth", False)
    flags += _override("model", model)
    flags += _override("sandbox_mode", _SANDBOX)
    flags += _override("approval_policy", "never")
    max_ctx = getattr(config, "max_context_tokens", None)
    if max_ctx:
        flags += _override("model_context_window", int(max_ctx))
    effort = str(getattr(config, "reasoning_effort", "") or "").strip()
    if effort:
        flags += _override("model_reasoning_effort", effort)
    return flags


def _session_args(config: AgentConfig) -> list[str]:
    """``resume <id>`` / ``resume --last`` from the spec's session mode."""
    claude = getattr(config, "claude", None)
    mode = str(getattr(claude, "session", "") or "").strip().lower()
    if wants_continue(getattr(claude, "session", None)):
        return ["resume", "--last"]
    if mode == SESSION_RESUME:
        resume_id = str(getattr(claude, "resume_id", "") or "").strip()
        if resume_id:
            return ["resume", resume_id]
    return []


def codex_tui_argv(
    config: AgentConfig,
    *,
    mcp_config: str | None = None,
    channel_mcp: str | None = None,
    settings: str | None = None,
) -> list[str]:
    """Argv for the interactive ``codex`` TUI (pre-shell-wrap).

    ``mcp_config`` is the in-container path of the workspace ``.mcp.json``
    sac materialised; ``channel_mcp`` the inline JSON registering the
    ``sac mcp channel`` subscriber. Both are handed to the in-container shim,
    which turns them into ``-c mcp_servers.*`` overrides the way the Claude
    TUI receives them as ``--mcp-config`` flags. ``settings`` is the
    in-container path of the Claude ``settings.json`` the TUI would launch
    with: its ``hooks`` block is copied into ``$CODEX_HOME/hooks.json`` by
    the shim — Codex's hooks engine reads the same event -> matcher ->
    command shape (measured 2026-09-05: its diagnostics name `matcher`,
    "empty hook command", and skip prompt/agent/async hook TYPES by their
    Claude names), so the fleet's hooks port by copy, not by rewrite.
    """
    argv: list[str] = ["python3", "-m", CODEX_EXEC_MODULE]
    if mcp_config:
        argv += ["--mcp-config", mcp_config]
    if channel_mcp:
        argv += ["--mcp-json", channel_mcp]
    if settings:
        argv += ["--hooks-from", settings]
    argv.append("--")
    argv += _session_args(config)
    argv += codex_config_overrides(config)
    return argv
