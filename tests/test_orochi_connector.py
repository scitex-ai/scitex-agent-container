"""Tests for the Orochi auto-connect feature (OrochiSpec + start_orochi_sidecar)."""

from __future__ import annotations

import logging
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

import yaml

from scitex_agent_container.config import AgentConfig, OrochiSpec, load_config


MINIMAL_CONFIG = {
    "apiVersion": "cld-agent/v1",
    "kind": "Agent",
    "metadata": {"name": "test-agent"},
    "spec": {"runtime": "claude-code"},
}


def _write_config(data: dict) -> str:
    """Write a config dict to a temp file and return its path."""
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
    yaml.safe_dump(data, tmp)
    tmp.close()
    return tmp.name


# ---------------------------------------------------------------------------
# 1. OrochiSpec dataclass defaults
# ---------------------------------------------------------------------------
class TestOrochiSpecDefaults:
    def test_defaults(self):
        spec = OrochiSpec()
        assert spec.enabled is False
        assert spec.hosts == []
        assert spec.port == 8559
        assert spec.ws_path == "/ws/agent/"
        assert spec.token_env == "SCITEX_OROCHI_TOKEN"
        assert spec.channels == []
        assert spec.heartbeat_interval == 30
        assert spec.reconnect_interval == 10
        assert spec.reconnect_max_retries == 0


# ---------------------------------------------------------------------------
# 2. OrochiSpec.is_enabled property
# ---------------------------------------------------------------------------
class TestOrochiSpecIsEnabled:
    def test_enabled_with_hosts(self):
        """enabled=True and hosts set => is_enabled is True."""
        spec = OrochiSpec(enabled=True, hosts=["192.168.1.1"])
        assert spec.is_enabled is True

    def test_disabled(self):
        """enabled=False => is_enabled is False regardless of hosts."""
        spec = OrochiSpec(enabled=False, hosts=["192.168.1.1"])
        assert spec.is_enabled is False

    def test_enabled_empty_hosts(self):
        """enabled=True but hosts is empty => is_enabled is False."""
        spec = OrochiSpec(enabled=True, hosts=[])
        assert spec.is_enabled is False

    def test_default_is_not_enabled(self):
        """Default OrochiSpec is not enabled (enabled=False)."""
        spec = OrochiSpec()
        assert spec.is_enabled is False


# ---------------------------------------------------------------------------
# 3. Config loading WITH spec.orochi YAML section
# ---------------------------------------------------------------------------
class TestConfigLoadWithOrochi:
    def test_orochi_hosts_list(self):
        """hosts (list) is parsed correctly."""
        data = {
            "apiVersion": "cld-agent/v1",
            "kind": "Agent",
            "metadata": {"name": "orochi-agent"},
            "spec": {
                "runtime": "claude-code",
                "orochi": {
                    "enabled": True,
                    "hosts": ["10.0.0.5", "orochi.example.com"],
                    "port": 8888,
                    "token_env": "MY_TOKEN",
                    "channels": ["#ops", "#alerts"],
                    "heartbeat_interval": 60,
                    "reconnect_interval": 5,
                    "reconnect_max_retries": 10,
                },
            },
        }
        path = _write_config(data)
        try:
            config = load_config(path)
            assert config.orochi.enabled is True
            assert config.orochi.hosts == ["10.0.0.5", "orochi.example.com"]
            assert config.orochi.port == 8888
            assert config.orochi.token_env == "MY_TOKEN"
            assert config.orochi.channels == ["#ops", "#alerts"]
            assert config.orochi.heartbeat_interval == 60
            assert config.orochi.reconnect_interval == 5
            assert config.orochi.reconnect_max_retries == 10
            assert config.orochi.is_enabled is True
        finally:
            Path(path).unlink()

    def test_orochi_single_host_backward_compat(self):
        """Single 'host' string is converted to hosts list."""
        data = {
            "apiVersion": "cld-agent/v1",
            "kind": "Agent",
            "metadata": {"name": "compat-agent"},
            "spec": {
                "runtime": "claude-code",
                "orochi": {
                    "enabled": True,
                    "host": "hub.local",
                },
            },
        }
        path = _write_config(data)
        try:
            config = load_config(path)
            assert config.orochi.enabled is True
            assert config.orochi.hosts == ["hub.local"]
            assert config.orochi.port == 8559  # default
            assert config.orochi.is_enabled is True
        finally:
            Path(path).unlink()

    def test_orochi_partial_fields(self):
        """Only some orochi fields specified -- rest use defaults."""
        data = {
            "apiVersion": "cld-agent/v1",
            "kind": "Agent",
            "metadata": {"name": "partial-orochi"},
            "spec": {
                "runtime": "claude-code",
                "orochi": {
                    "enabled": True,
                    "hosts": ["hub.local"],
                },
            },
        }
        path = _write_config(data)
        try:
            config = load_config(path)
            assert config.orochi.enabled is True
            assert config.orochi.hosts == ["hub.local"]
            assert config.orochi.port == 8559  # default
            assert config.orochi.token_env == "SCITEX_OROCHI_TOKEN"  # default
            assert config.orochi.channels == []  # default
            assert config.orochi.heartbeat_interval == 30  # default
        finally:
            Path(path).unlink()


# ---------------------------------------------------------------------------
# 4. Config loading WITHOUT spec.orochi
# ---------------------------------------------------------------------------
class TestConfigLoadWithoutOrochi:
    def test_defaults_when_no_orochi_section(self):
        path = _write_config(MINIMAL_CONFIG)
        try:
            config = load_config(path)
            assert config.orochi.enabled is False
            assert config.orochi.hosts == []
            assert config.orochi.port == 8559
            assert config.orochi.token_env == "SCITEX_OROCHI_TOKEN"
            assert config.orochi.channels == []
            assert config.orochi.heartbeat_interval == 30
            assert config.orochi.reconnect_interval == 10
            assert config.orochi.reconnect_max_retries == 0
            assert config.orochi.is_enabled is False
        finally:
            Path(path).unlink()


# ---------------------------------------------------------------------------
# 5. start_orochi_sidecar returns None when orochi not enabled
# ---------------------------------------------------------------------------
class TestStartOrochiSidecarDisabled:
    def test_returns_none_when_not_enabled(self):
        from scitex_agent_container.orochi_connector import start_orochi_sidecar

        config = AgentConfig(
            name="disabled-agent",
            orochi=OrochiSpec(enabled=False),
        )
        result = start_orochi_sidecar(config)
        assert result is None

    def test_returns_none_when_enabled_but_empty_hosts(self):
        from scitex_agent_container.orochi_connector import start_orochi_sidecar

        config = AgentConfig(
            name="empty-hosts-agent",
            orochi=OrochiSpec(enabled=True, hosts=[]),
        )
        result = start_orochi_sidecar(config)
        assert result is None


# ---------------------------------------------------------------------------
# 6. start_orochi_sidecar returns None when token env var not set
# ---------------------------------------------------------------------------
class TestStartOrochiSidecarNoToken:
    def test_returns_none_and_warns_when_token_missing(self, caplog):
        from scitex_agent_container.orochi_connector import start_orochi_sidecar

        config = AgentConfig(
            name="no-token-agent",
            orochi=OrochiSpec(
                enabled=True, hosts=["10.0.0.1"], token_env="NONEXISTENT_TOKEN_VAR"
            ),
        )
        with patch.dict("os.environ", {}, clear=True):
            with caplog.at_level(logging.WARNING, logger="agent-container.orochi"):
                result = start_orochi_sidecar(config)

        assert result is None
        assert any("NONEXISTENT_TOKEN_VAR" in rec.message for rec in caplog.records)

    def test_uses_config_env_fallback(self):
        """When os.environ lacks the token but config.env has it, should proceed."""
        from scitex_agent_container.orochi_connector import start_orochi_sidecar

        config = AgentConfig(
            name="fallback-token-agent",
            env={"MY_OROCHI_TOKEN": "secret123"},
            orochi=OrochiSpec(
                enabled=True, hosts=["10.0.0.1"], token_env="MY_OROCHI_TOKEN"
            ),
        )
        with patch.dict("os.environ", {}, clear=True):
            with patch(
                "scitex_agent_container.orochi_connector._run_connector"
            ) as mock_run:
                result = start_orochi_sidecar(config)

        assert result is not None
        assert isinstance(result, threading.Thread)
        result.join(timeout=2)
        mock_run.assert_called_once_with(config, "secret123")


# ---------------------------------------------------------------------------
# 7. start_orochi_sidecar returns a Thread when properly configured
# ---------------------------------------------------------------------------
class TestStartOrochiSidecarSuccess:
    def test_returns_thread_when_configured(self):
        from scitex_agent_container.orochi_connector import start_orochi_sidecar

        config = AgentConfig(
            name="connected-agent",
            orochi=OrochiSpec(
                enabled=True,
                hosts=["127.0.0.1"],
                port=8559,
                token_env="TEST_TOKEN",
            ),
        )
        with patch.dict("os.environ", {"TEST_TOKEN": "my-secret-token"}, clear=False):
            with patch(
                "scitex_agent_container.orochi_connector._run_connector"
            ) as mock_run:
                result = start_orochi_sidecar(config)

        assert result is not None
        assert isinstance(result, threading.Thread)
        assert result.daemon is True
        assert result.name == "orochi-connected-agent"
        result.join(timeout=2)
        mock_run.assert_called_once_with(config, "my-secret-token")

    def test_thread_calls_run_connector_with_correct_args(self):
        from scitex_agent_container.orochi_connector import start_orochi_sidecar

        config = AgentConfig(
            name="arg-check-agent",
            orochi=OrochiSpec(
                enabled=True,
                hosts=["hub.example.com", "fallback.example.com"],
                port=7777,
                token_env="ARG_TOKEN",
            ),
        )
        with patch.dict("os.environ", {"ARG_TOKEN": "tok-abc"}, clear=False):
            with patch(
                "scitex_agent_container.orochi_connector._run_connector"
            ) as mock_run:
                thread = start_orochi_sidecar(config)
                thread.join(timeout=2)

        mock_run.assert_called_once_with(config, "tok-abc")
