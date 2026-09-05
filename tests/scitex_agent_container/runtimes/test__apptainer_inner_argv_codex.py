"""Tests for ``runtimes/_apptainer_inner_argv_codex`` — the codex TUI argv.

Real seams only: real ``AgentConfig`` / ``ClaudeSpec`` / ``ProviderSpec``
objects through the real builder. Each test pins one observable fact.
"""

from __future__ import annotations

from scitex_agent_container.config import AgentConfig, ClaudeSpec, ProviderSpec
from scitex_agent_container.runtimes._apptainer_inner_argv_codex import (
    CODEX_EXEC_MODULE,
    CODEX_KEY_ENV,
    CODEX_PROVIDER_ID,
    codex_config_overrides,
    codex_tui_argv,
)
from scitex_agent_container.runtimes._apptainer_provider import ProviderEnvError


def _config(**claude_kw) -> AgentConfig:
    claude = ClaudeSpec(
        model=claude_kw.pop("model", "qwen38-27b"),
        provider=claude_kw.pop(
            "provider",
            ProviderSpec(
                base_url="http://100.64.0.1:18772/",
                auth_token_env="FLEET_GATEWAY_KEY",
            ),
        ),
        **claude_kw,
    )
    return AgentConfig(
        name="hm", runtime="tui", workdir="/tmp/hm-wd", harness="codex", claude=claude
    )


def _overrides(flags: list[str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for i, a in enumerate(flags):
        if a == "-c" and i + 1 < len(flags):
            k, _, v = flags[i + 1].partition("=")
            out[k] = v
    return out


def test_overrides_select_the_sac_provider():
    # Arrange
    config = _config()
    # Act
    seen = _overrides(codex_config_overrides(config))
    # Assert
    assert seen["model_provider"] == f'"{CODEX_PROVIDER_ID}"'


def test_overrides_point_the_provider_at_the_gateway_v1_root():
    # Arrange -- the spec's base_url is the Anthropic-style root; Codex wants /v1.
    config = _config()
    # Act
    seen = _overrides(codex_config_overrides(config))
    # Assert
    assert (
        seen[f"model_providers.{CODEX_PROVIDER_ID}.base_url"]
        == '"http://100.64.0.1:18772/v1"'
    )


def test_overrides_speak_the_responses_api_only():
    # Arrange -- measured: "responses" is the only wire_api the binary accepts.
    config = _config()
    # Act
    seen = _overrides(codex_config_overrides(config))
    # Assert
    assert seen[f"model_providers.{CODEX_PROVIDER_ID}.wire_api"] == '"responses"'


def test_overrides_name_the_key_env_sac_fills():
    # Arrange
    config = _config()
    # Act
    seen = _overrides(codex_config_overrides(config))
    # Assert
    assert seen[f"model_providers.{CODEX_PROVIDER_ID}.env_key"] == f'"{CODEX_KEY_ENV}"'


def test_overrides_carry_the_engine_model():
    # Arrange
    config = _config(model="qwen38-27b")
    # Act
    seen = _overrides(codex_config_overrides(config))
    # Assert
    assert seen["model"] == '"qwen38-27b"'


def test_overrides_disable_the_nested_sandbox():
    # Arrange -- bubblewrap cannot nest inside apptainer; the container is the boundary.
    config = _config()
    # Act
    seen = _overrides(codex_config_overrides(config))
    # Assert
    assert seen["sandbox_mode"] == '"danger-full-access"'


def test_overrides_carry_the_declared_context_window():
    # Arrange -- the engine fold leaves max_context_tokens on the config.
    config = _config()
    config.max_context_tokens = 1_048_576
    # Act
    seen = _overrides(codex_config_overrides(config))
    # Assert
    assert seen["model_context_window"] == "1048576"


def test_overrides_omit_the_context_window_when_none_is_declared():
    # Arrange
    config = _config()
    # Act
    seen = _overrides(codex_config_overrides(config))
    # Assert
    assert "model_context_window" not in seen


def test_overrides_refuse_a_config_without_a_provider():
    # Arrange -- silence would mean Codex's OpenAI-hosted default.
    config = _config(provider=None)
    # Act
    try:
        codex_config_overrides(config)
        message = ""
    except ProviderEnvError as exc:
        message = str(exc)
    # Assert
    assert "needs an inference provider" in message


def test_overrides_refuse_a_config_without_a_model():
    # Arrange
    config = _config(model="")
    config.model = ""
    # Act
    try:
        codex_config_overrides(config)
        message = ""
    except ProviderEnvError as exc:
        message = str(exc)
    # Assert
    assert "names no model" in message


def test_argv_runs_the_in_container_exec_shim():
    # Arrange -- the binary is resolved inside the container, not on the host.
    config = _config()
    # Act
    argv = codex_tui_argv(config)
    # Assert
    assert argv[:3] == ["python3", "-m", CODEX_EXEC_MODULE]


def test_argv_hands_the_mcp_files_to_the_shim():
    # Arrange
    config = _config()
    # Act
    argv = codex_tui_argv(
        config, mcp_config="/home/agent/.mcp.json", channel_mcp='{"a":1}'
    )
    # Assert
    assert argv[3:8] == [
        "--mcp-config",
        "/home/agent/.mcp.json",
        "--mcp-json",
        '{"a":1}',
        "--",
    ]


def test_argv_resumes_a_pinned_session_by_id():
    # Arrange -- spec.claude.session: resume + resume_id, like the Claude TUI.
    config = _config(session="resume", resume_id="0f4c1e6a-2b6d-4d0e-9c5b-7a1b2c3d4e5f")
    # Act
    argv = codex_tui_argv(config)
    # Assert
    assert argv[argv.index("--") + 1 : argv.index("--") + 3] == [
        "resume",
        "0f4c1e6a-2b6d-4d0e-9c5b-7a1b2c3d4e5f",
    ]


def test_argv_continues_the_latest_session_for_continue_mode():
    # Arrange
    config = _config(session="continue")
    # Act
    argv = codex_tui_argv(config)
    # Assert
    assert argv[argv.index("--") + 1 : argv.index("--") + 3] == ["resume", "--last"]


def test_argv_starts_fresh_without_a_session_directive():
    # Arrange
    config = _config()
    # Act
    argv = codex_tui_argv(config)
    # Assert -- the first thing after the separator is an override, not `resume`.
    assert argv[argv.index("--") + 1] == "-c"


def test_argv_hands_the_settings_to_the_shim_for_hooks():
    # Arrange -- the same settings path the Claude TUI launches with.
    config = _config()
    # Act
    argv = codex_tui_argv(config, settings="/home/agent/.claude/settings.json")
    # Assert
    assert argv[3:5] == ["--hooks-from", "/home/agent/.claude/settings.json"]


def test_overrides_trust_the_workdir_up_front():
    # Arrange -- otherwise Codex parks on its directory-trust picker at boot.
    config = _config()
    # Act
    seen = _overrides(codex_config_overrides(config))
    # Assert
    assert seen['projects."/tmp/hm-wd".trust_level'] == '"trusted"'

