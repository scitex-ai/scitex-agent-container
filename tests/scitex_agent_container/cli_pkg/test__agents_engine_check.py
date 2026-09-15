"""The documented ``sac agents engine-check`` command is wired and safe."""

from __future__ import annotations

import json

from click.testing import CliRunner

from scitex_agent_container.cli_pkg import _agents_engine_check as command
from scitex_agent_container.cli_pkg.agent_group import agent_group
from scitex_agent_container.config import AgentConfig, EngineSpec, ProviderSpec
from scitex_agent_container.config._engine_tool_probe import ToolProbeResult


def _config() -> AgentConfig:
    config = AgentConfig(name="probe-agent", harness="codex", runtime="headless")
    engine = EngineSpec(
        key="qwen",
        model="qwen38-27b",
        provider=ProviderSpec(
            base_url="http://gateway",
            auth_token_env="QWEN_KEY",
        ),
        provider_declared={
            "base_url": "http://gateway",
            "auth_token_env": "QWEN_KEY",
        },
        is_default=True,
    )
    config.engines = {"qwen": engine}
    config.engine_key = "qwen"
    return config


def test_engine_check_is_registered_under_agents() -> None:
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(agent_group, ["engine-check", "--help"])
    # Assert
    assert result.exit_code == 0
    assert "Check exact tool calls" in result.output


def test_engine_check_reports_all_attempts_without_secret(
    monkeypatch, tmp_path
) -> None:
    # Arrange
    spec = tmp_path / "spec.yaml"
    spec.write_text("placeholder: true\n")
    config = _config()
    monkeypatch.setenv("QWEN_KEY", "secret")
    monkeypatch.setattr(command, "load_config", lambda _: config)
    monkeypatch.setattr(command, "resolve_provider_api_key", lambda _: "secret")
    monkeypatch.setattr(
        command,
        "probe_engine_tools",
        lambda *args, **kwargs: ToolProbeResult(
            True,
            "openai-responses",
            "exact tool call received",
            "sac_engine_probe",
            {"nonce": "result-nonce"},
        ),
    )
    # Act
    result = CliRunner().invoke(
        command.engine_check, [str(spec), "--count", "2", "--json"]
    )
    # Assert
    payload = json.loads(result.output)
    assert result.exit_code == 0
    assert payload["passed"] == payload["attempted"] == 2
    assert payload["harness"] == "codex"
    assert "secret" not in result.output


def test_engine_check_fails_when_one_call_is_inexact(monkeypatch, tmp_path) -> None:
    # Arrange
    spec = tmp_path / "spec.yaml"
    spec.write_text("placeholder: true\n")
    config = _config()
    monkeypatch.setenv("QWEN_KEY", "secret")
    monkeypatch.setattr(command, "load_config", lambda _: config)
    monkeypatch.setattr(command, "resolve_provider_api_key", lambda _: "secret")
    monkeypatch.setattr(
        command,
        "probe_engine_tools",
        lambda *args, **kwargs: ToolProbeResult(
            False, "openai-responses", "tool name does not match"
        ),
    )
    # Act
    result = CliRunner().invoke(command.engine_check, [str(spec), "--count", "1"])
    # Assert
    assert result.exit_code == 1
    assert "0/1 exact tool calls" in result.output
    assert "engine tool-call conformance failed" in result.output
