from __future__ import annotations

import os

import pytest

from scitex_agent_container._lifecycle import _codex_gateway as gateway
from scitex_agent_container.config import AgentConfig, ClaudeSpec, ProviderSpec


def _config(*, codex: bool = True) -> AgentConfig:
    provider = (
        ProviderSpec(
            base_url="http://127.0.0.1:18765",
            auth_token_env="SCITEX_GENAI_GATEWAY_API_KEY",
        )
        if codex
        else None
    )
    return AgentConfig(
        name="test-agent",
        workdir="/tmp/work",
        claude=ClaudeSpec(model="gpt-test", provider=provider),
    )


def test_non_codex_backend_is_noop(monkeypatch):
    monkeypatch.setattr(
        gateway,
        "_runtime_dir",
        lambda: pytest.fail("non-Codex config must not touch gateway state"),
    )

    gateway.ensure_codex_gateway(_config(codex=False))


def test_reuses_authenticated_running_gateway(tmp_path, monkeypatch):
    key_path = tmp_path / "api-key"
    gateway._write_private(key_path, "persisted-key")
    monkeypatch.delenv("SCITEX_GENAI_GATEWAY_API_KEY", raising=False)
    monkeypatch.setattr(gateway, "_runtime_dir", lambda: tmp_path)
    monkeypatch.setattr(gateway, "_configured_key", lambda: "")
    monkeypatch.setattr(gateway, "_health", lambda _url: True)
    monkeypatch.setattr(
        gateway, "_accepts_key", lambda _url, key: key == "persisted-key"
    )
    monkeypatch.setattr(
        gateway.shutil,
        "which",
        lambda _name: pytest.fail("running gateway must not spawn another"),
    )

    gateway.ensure_codex_gateway(_config())

    assert os.environ["SCITEX_GENAI_GATEWAY_API_KEY"] == "persisted-key"


def test_adopted_shell_key_is_persisted_for_later_starts(tmp_path, monkeypatch):
    monkeypatch.setattr(gateway, "_runtime_dir", lambda: tmp_path)
    monkeypatch.setattr(gateway, "_configured_key", lambda: "shell-only-key")
    monkeypatch.setattr(gateway, "_health", lambda _url: True)
    monkeypatch.setattr(gateway, "_accepts_key", lambda _url, _key: True)

    gateway.ensure_codex_gateway(_config())

    key_path = tmp_path / "api-key"
    assert key_path.read_text().strip() == "shell-only-key"
    assert key_path.stat().st_mode & 0o777 == 0o600


def test_running_gateway_without_recoverable_key_fails_loud(tmp_path, monkeypatch):
    monkeypatch.delenv("SCITEX_GENAI_GATEWAY_API_KEY", raising=False)
    monkeypatch.setattr(gateway, "_runtime_dir", lambda: tmp_path)
    monkeypatch.setattr(gateway, "_configured_key", lambda: "")
    monkeypatch.setattr(gateway, "_health", lambda _url: True)

    with pytest.raises(gateway.CodexGatewayError, match="cannot recover"):
        gateway.ensure_codex_gateway(_config())


def test_first_start_generates_private_key_and_launches(tmp_path, monkeypatch):
    calls: list[list[str]] = []
    health_checks = iter([False, True])
    monkeypatch.delenv("SCITEX_GENAI_GATEWAY_API_KEY", raising=False)
    monkeypatch.setattr(gateway, "_runtime_dir", lambda: tmp_path)
    monkeypatch.setattr(gateway, "_configured_key", lambda: "")
    monkeypatch.setattr(gateway, "_health", lambda _url: next(health_checks))
    monkeypatch.setattr(gateway, "_accepts_key", lambda _url, _key: True)
    monkeypatch.setattr(
        gateway.shutil, "which", lambda _name: "/venv/bin/scitex-genai-gateway"
    )

    class FakeProcess:
        pid = 4321

        def poll(self):
            return None

    def fake_popen(argv, **kwargs):
        calls.append(argv)
        assert kwargs["env"]["SCITEX_GENAI_GATEWAY_API_KEY"]
        assert kwargs["start_new_session"] is True
        return FakeProcess()

    monkeypatch.setattr(gateway.subprocess, "Popen", fake_popen)

    gateway.ensure_codex_gateway(_config())

    key_path = tmp_path / "api-key"
    assert len(key_path.read_text().strip()) == 64
    assert key_path.stat().st_mode & 0o777 == 0o600
    assert (tmp_path / "gateway.pid").read_text().strip() == "4321"
    assert calls == [
        [
            "/venv/bin/scitex-genai-gateway",
            "--host",
            "127.0.0.1",
            "--port",
            "18765",
            "--log-level",
            "warning",
        ]
    ]


def test_first_start_persists_configured_key(tmp_path, monkeypatch):
    health_checks = iter([False, True])
    monkeypatch.setattr(gateway, "_runtime_dir", lambda: tmp_path)
    monkeypatch.setattr(gateway, "_configured_key", lambda: "configured-key")
    monkeypatch.setattr(gateway, "_health", lambda _url: next(health_checks))
    monkeypatch.setattr(gateway, "_accepts_key", lambda _url, _key: True)
    monkeypatch.setattr(
        gateway.shutil, "which", lambda _name: "/venv/bin/scitex-genai-gateway"
    )

    class FakeProcess:
        pid = 4321

        def poll(self):
            return None

    monkeypatch.setattr(
        gateway.subprocess, "Popen", lambda _argv, **_kwargs: FakeProcess()
    )

    gateway.ensure_codex_gateway(_config())

    key_path = tmp_path / "api-key"
    assert key_path.read_text().strip() == "configured-key"
    assert key_path.stat().st_mode & 0o777 == 0o600
