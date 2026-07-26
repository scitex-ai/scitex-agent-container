"""Real-seam tests for automatic Codex gateway preparation."""

from __future__ import annotations

import os

import pytest

from scitex_agent_container._lifecycle import _codex_gateway as gateway
from scitex_agent_container.config import AgentConfig, ClaudeSpec, ProviderSpec


def _config(*, codex: bool = True, auth_token: str = "") -> AgentConfig:
    provider = (
        ProviderSpec(
            name="codex",
            base_url="http://127.0.0.1:18765",
            auth_token_env="SCITEX_GENAI_GATEWAY_API_KEY",
            auth_token=auth_token,
        )
        if codex
        else None
    )
    return AgentConfig(
        name="test-agent",
        workdir="/tmp/work",
        claude=ClaudeSpec(model="gpt-test", provider=provider),
    )


class _Process:
    pid = 4321

    def poll(self):
        return None


def test_non_codex_backend_touches_no_gateway_state(tmp_path):
    # Arrange
    runtime_dir = tmp_path / "gateway"
    # Act
    gateway.ensure_codex_gateway(_config(codex=False), runtime_dir=runtime_dir)
    # Assert
    assert not runtime_dir.exists()


def test_reuses_authenticated_running_gateway(tmp_path, env_save_restore):
    # Arrange
    gateway._write_private(tmp_path / "api-key", "persisted-key")
    env_save_restore.delete("SCITEX_GENAI_GATEWAY_API_KEY")
    # Act
    gateway.ensure_codex_gateway(
        _config(),
        runtime_dir=tmp_path,
        configured_key_fn=lambda _config: "",
        health_fn=lambda _url: True,
        accepts_key_fn=lambda _url, key: key == "persisted-key",
    )
    # Assert
    assert os.environ["SCITEX_GENAI_GATEWAY_API_KEY"] == "persisted-key"


def test_adopted_key_is_persisted_private(tmp_path):
    # Arrange
    def configured(_config):
        return "shell-only-key"

    # Act
    gateway.ensure_codex_gateway(
        _config(),
        runtime_dir=tmp_path,
        configured_key_fn=configured,
        health_fn=lambda _url: True,
        accepts_key_fn=lambda _url, _key: True,
    )
    key_path = tmp_path / "api-key"
    # Assert
    assert (
        key_path.read_text().strip(),
        key_path.stat().st_mode & 0o777,
    ) == ("shell-only-key", 0o600)


def test_running_gateway_without_recoverable_key_fails_loud(tmp_path):
    # Arrange
    def action():
        gateway.ensure_codex_gateway(
            _config(),
            runtime_dir=tmp_path,
            configured_key_fn=lambda _config: "",
            health_fn=lambda _url: True,
        )

    # Act
    ctx = pytest.raises(gateway.CodexGatewayError, match="cannot recover")
    # Assert
    with ctx:
        action()


def test_first_start_generates_private_key_and_launches(tmp_path):
    # Arrange
    calls: list[list[str]] = []
    health_checks = iter([False, True])

    def record_popen(argv, **_kwargs):
        calls.append(argv)
        return _Process()

    # Act
    gateway.ensure_codex_gateway(
        _config(auth_token="auto"),
        runtime_dir=tmp_path,
        configured_key_fn=lambda _config: "",
        health_fn=lambda _url: next(health_checks),
        accepts_key_fn=lambda _url, _key: True,
        executable_resolver=lambda _name: "/venv/bin/scitex-genai-gateway",
        popen_fn=record_popen,
    )
    key_path = tmp_path / "api-key"
    # Assert
    assert (
        len(key_path.read_text().strip()),
        key_path.stat().st_mode & 0o777,
        (tmp_path / "gateway.pid").read_text().strip(),
        calls,
    ) == (
        64,
        0o600,
        "4321",
        [
            [
                "/venv/bin/scitex-genai-gateway",
                "--host",
                "127.0.0.1",
                "--port",
                "18765",
                "--log-level",
                "warning",
            ]
        ],
    )


def test_first_start_persists_direct_spec_key(tmp_path):
    # Arrange
    health_checks = iter([False, True])
    # Act
    gateway.ensure_codex_gateway(
        _config(auth_token="configured-key"),
        runtime_dir=tmp_path,
        health_fn=lambda _url: next(health_checks),
        accepts_key_fn=lambda _url, _key: True,
        executable_resolver=lambda _name: "/venv/bin/scitex-genai-gateway",
        popen_fn=lambda _argv, **_kwargs: _Process(),
    )
    key_path = tmp_path / "api-key"
    # Assert
    assert (
        key_path.read_text().strip(),
        key_path.stat().st_mode & 0o777,
    ) == ("configured-key", 0o600)
