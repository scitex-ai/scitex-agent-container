"""Tests for config loading and validation."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import yaml

from scitex_agent_container.config import AgentConfig, load_config, validate_config


MINIMAL_CONFIG = {
    "apiVersion": "cld-agent/v1",
    "kind": "Agent",
    "metadata": {"name": "test-agent"},
    "spec": {"runtime": "claude-code"},
}

FULL_CONFIG = {
    "apiVersion": "cld-agent/v1",
    "kind": "Agent",
    "metadata": {
        "name": "full-agent",
        "labels": {"role": "worker", "team": "dev"},
    },
    "spec": {
        "runtime": "claude-code",
        "model": "opus",
        "workdir": "/tmp/test-workdir",
        "claude": {
            "channels": ["plugin:telegram@claude-plugins-official"],
            "flags": ["--dangerously-skip-permissions"],
            "session": "continue",
        },
        "env": {"MY_VAR": "my_value"},
        "screen": {"name": "cld-full"},
        "container": {
            "runtime": "docker",
            "image": "my-image:latest",
            "volumes": ["/data:/data"],
            "network": "bridge",
        },
        "health": {
            "enabled": True,
            "interval": 45,
            "timeout": 10,
            "method": "screen-alive",
        },
        "restart": {
            "policy": "on-failure",
            "max_retries": 5,
            "backoff": {"initial": 15, "max": 120, "multiplier": 3},
        },
        "hooks": {
            "pre_start": ["echo pre"],
            "post_start": ["echo post"],
            "pre_stop": [],
            "post_stop": [],
        },
    },
}


def _write_config(data: dict) -> str:
    """Write a config dict to a temp file and return its path."""
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
    yaml.safe_dump(data, tmp)
    tmp.close()
    return tmp.name


class TestLoadConfig:
    def test_minimal_config(self):
        path = _write_config(MINIMAL_CONFIG)
        config = load_config(path)
        assert config.name == "test-agent"
        assert config.runtime == "claude-code"
        assert config.model == "sonnet"  # default
        assert config.screen_name == "cld-test-agent"  # auto-generated
        Path(path).unlink()

    def test_full_config(self):
        path = _write_config(FULL_CONFIG)
        config = load_config(path)
        assert config.name == "full-agent"
        assert config.model == "opus"
        assert config.labels == {"role": "worker", "team": "dev"}
        assert config.claude.channels == ["plugin:telegram@claude-plugins-official"]
        assert config.claude.session == "continue"
        assert config.container.runtime == "docker"
        assert config.container.image == "my-image:latest"
        assert config.container.network == "bridge"
        assert config.health.enabled is True
        assert config.health.interval == 45
        assert config.restart.policy == "on-failure"
        assert config.restart.max_retries == 5
        assert config.restart.backoff_initial == 15
        assert config.restart.backoff_multiplier == 3
        assert config.screen_name == "cld-full"
        assert config.env == {"MY_VAR": "my_value"}
        assert config.hooks["pre_start"] == ["echo pre"]
        Path(path).unlink()

    def test_expanded_workdir(self):
        path = _write_config(MINIMAL_CONFIG)
        config = load_config(path)
        expanded = config.expanded_workdir
        assert "~" not in expanded
        Path(path).unlink()

    def test_invalid_api_version(self):
        data = {**MINIMAL_CONFIG, "apiVersion": "wrong/v2"}
        path = _write_config(data)
        with pytest.raises(ValueError, match="apiVersion"):
            load_config(path)
        Path(path).unlink()

    def test_missing_name(self):
        data = {
            "apiVersion": "cld-agent/v1",
            "kind": "Agent",
            "metadata": {},
            "spec": {"runtime": "claude-code"},
        }
        path = _write_config(data)
        with pytest.raises(ValueError, match="name"):
            load_config(path)
        Path(path).unlink()

    def test_invalid_runtime(self):
        data = {
            "apiVersion": "cld-agent/v1",
            "kind": "Agent",
            "metadata": {"name": "test"},
            "spec": {"runtime": "invalid-runtime"},
        }
        path = _write_config(data)
        with pytest.raises(ValueError, match="runtime"):
            load_config(path)
        Path(path).unlink()


class TestValidateConfig:
    def test_valid_config(self):
        path = _write_config(MINIMAL_CONFIG)
        errors = validate_config(path)
        assert errors == []
        Path(path).unlink()

    def test_invalid_yaml(self):
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
        tmp.write(":::invalid yaml:::")
        tmp.close()
        errors = validate_config(tmp.name)
        assert len(errors) > 0
        Path(tmp.name).unlink()

    def test_missing_file(self):
        errors = validate_config("/nonexistent/path.yaml")
        assert any("not found" in e.lower() or "File not found" in e for e in errors)

    def test_invalid_container_runtime(self):
        data = {
            "apiVersion": "cld-agent/v1",
            "kind": "Agent",
            "metadata": {"name": "test"},
            "spec": {
                "runtime": "claude-code",
                "container": {"runtime": "podman"},
            },
        }
        path = _write_config(data)
        errors = validate_config(path)
        assert any("container.runtime" in e for e in errors)
        Path(path).unlink()

    def test_invalid_restart_policy(self):
        data = {
            "apiVersion": "cld-agent/v1",
            "kind": "Agent",
            "metadata": {"name": "test"},
            "spec": {
                "runtime": "claude-code",
                "restart": {"policy": "maybe"},
            },
        }
        path = _write_config(data)
        errors = validate_config(path)
        assert any("restart.policy" in e for e in errors)
        Path(path).unlink()
