"""Tests for v2 config loading, auto-derivation, and mcp_servers."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import pytest
import yaml

from scitex_agent_container.config import AgentConfig, load_config, validate_config


def _write_config(data: dict) -> str:
    """Write a config dict to ``<tmp>/<name>/<name>.yaml`` and return its path.

    Dir-as-SSoT: the loader derives the agent name from the parent dir.
    The helper picks the dir name from ``data["metadata"]["name"]`` (a
    test-only convenience) and **strips** that field before writing so
    the validator (which now rejects metadata.name) doesn't complain.
    """
    import copy

    from tests.scitex_agent_container._helpers.explicit_spec import (
        deep_merge,
        explicit_spec_defaults,
    )

    data = copy.deepcopy(data)
    # Red-start ruling 2026-07-21: every spec field must be explicit —
    # merge the validator's own paste defaults beneath (fixture wins).
    data["spec"] = deep_merge(
        explicit_spec_defaults(data.get("kind", "Agent")),
        data.get("spec") or {},
    )
    metadata = data.get("metadata") or {}
    name = metadata.pop("name", None) or "test-agent"
    if metadata:
        data["metadata"] = metadata
    elif "metadata" in data:
        del data["metadata"]
    tmp_dir = Path(tempfile.mkdtemp()) / name
    tmp_dir.mkdir(parents=True)
    path = tmp_dir / f"{name}.yaml"
    path.write_text(yaml.safe_dump(data))
    return str(path)


# No-hidden-defaults (operator directive 2026-06-23): each inline fixture
# spec declares every now-required author field (runtime, host, workdir,
# claude.model, apptainer.{image,binds}, health.{enabled,interval},
# restart.{policy,max_retries}) so load_config validates clean.
MINIMAL_V1_CONFIG = {
    "apiVersion": "scitex-agent-container/v3",
    "kind": "Agent",
    "metadata": {"name": "test-agent"},
    "spec": {
        "runtime": "apptainer",
        "host": "${HOSTNAME}",
        "workdir": "~/.scitex/agent-container/runtime/agents/test-agent",
        "claude": {"model": "claude-opus-4-8[1m]"},
        "apptainer": {"image": "/opt/sac/scitex.sif", "binds": []},
        "health": {"enabled": True, "interval": 60},
        "restart": {"policy": "on-failure", "max_retries": 3},
    },
}

MINIMAL_V2_CONFIG = {
    "apiVersion": "scitex-agent-container/v3",
    "kind": "Agent",
    "metadata": {
        "name": "head-test",
        "labels": {"role": "head", "team": "fleet", "machine": "test-box"},
    },
    "spec": {
        "runtime": "apptainer",
        "host": "${HOSTNAME}",
        "workdir": "~/.scitex/agent-container/runtime/agents/head-test",
        # v3-realign: model moved to spec.claude.model.
        "claude": {"model": "opus[1m]"},
        "apptainer": {"image": "/opt/sac/scitex.sif", "binds": []},
        "health": {"enabled": True, "interval": 60},
        "restart": {"policy": "on-failure", "max_retries": 3},
    },
}

V2_WITH_MCP = {
    "apiVersion": "scitex-agent-container/v3",
    "kind": "Agent",
    "metadata": {
        "name": "head-test",
        "labels": {"role": "head"},
    },
    "spec": {
        "runtime": "apptainer",
        "host": "${HOSTNAME}",
        "workdir": "~/.scitex/agent-container/runtime/agents/head-test",
        # v3-realign: model moved to spec.claude.model.
        "claude": {"model": "sonnet"},
        "apptainer": {"image": "/opt/sac/scitex.sif", "binds": []},
        "health": {"enabled": True, "interval": 60},
        "restart": {"policy": "on-failure", "max_retries": 3},
        "mcp_servers": {
            "fleet-hub": {
                "type": "stdio",
                "command": "bun",
                "args": ["run", "~/proj/fleet-hub/ts/mcp_channel.ts"],
                "env": {
                    "SCITEX_HUB_URL": "wss://fleet-hub.example.com",
                    "SCITEX_HUB_AGENT": "${metadata.name}",
                    "SCITEX_HUB_TOKEN": "${SCITEX_HUB_TOKEN}",
                },
            }
        },
    },
}


@pytest.fixture
def v2_loaded_config(tmp_path):
    """Load MINIMAL_V2_CONFIG with HOME pointed at tmp_path."""
    import os

    saved = os.environ.get("HOME")
    os.environ["HOME"] = str(tmp_path)
    path = _write_config(MINIMAL_V2_CONFIG)
    try:
        cfg = load_config(path)
        yield cfg
    finally:
        Path(path).unlink(missing_ok=True)
        if saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved


@pytest.fixture
def v2_mcp_loaded_config():
    path = _write_config(V2_WITH_MCP)
    cfg = load_config(path)
    yield cfg
    Path(path).unlink(missing_ok=True)


@pytest.fixture
def v2_overrides_loaded_config():
    data = {
        **MINIMAL_V2_CONFIG,
        "spec": {
            **MINIMAL_V2_CONFIG["spec"],
            "workdir": "/custom/path",
            "screen": {"name": "custom-screen"},
            # v3-realign: env moved to spec.apptainer.env. Merge onto the
            # required image/binds rather than replacing the whole block.
            "apptainer": {
                **MINIMAL_V2_CONFIG["spec"]["apptainer"],
                "env": {"CLAUDE_AGENT_ID": "custom-id"},
            },
        },
    }
    path = _write_config(data)
    cfg = load_config(path)
    yield cfg
    Path(path).unlink(missing_ok=True)


@pytest.fixture
def v1_minimal_loaded_config():
    path = _write_config(MINIMAL_V1_CONFIG)
    cfg = load_config(path)
    yield cfg
    Path(path).unlink(missing_ok=True)


class TestV2AutoDerivedWorkdir:
    def test_v2_auto_derives_runtime_workdir(self):
        # Arrange — workdir is now REQUIRED in YAML (no hidden default), so the
        # runtime-root derivation is only reachable by constructing the config
        # object directly with an empty workdir (bypassing YAML validation).
        config = AgentConfig(name="head-test", workdir="")
        expected = str(Path.home() / ".scitex/agent-container/runtime/agents/head-test")
        # Act
        expanded = config.expanded_workdir
        # Assert
        assert expanded == expected


class TestV2ScreenName:
    def test_v2_screen_name_uses_bare_metadata_name(self, v2_loaded_config):
        """v2 screen_name is {name}, not cld-{name}."""
        # Arrange
        config = v2_loaded_config
        # Act
        screen_name = config.screen_name
        # Assert
        assert screen_name == "head-test"


class TestV2AutoDerivedEnv:
    def test_v2_env_sets_claude_agent_id(self, v2_loaded_config):
        # Arrange
        config = v2_loaded_config
        # Act
        value = config.env.get("CLAUDE_AGENT_ID")
        # Assert
        assert value == "head-test"

    def test_v2_env_sets_claude_agent_role(self, v2_loaded_config):
        # Arrange
        config = v2_loaded_config
        # Act
        value = config.env.get("CLAUDE_AGENT_ROLE")
        # Assert
        assert value == "head"

    def test_v2_env_sets_sac_agent(self, v2_loaded_config):
        # Arrange
        config = v2_loaded_config
        # Act
        value = config.env.get("SCITEX_AGENT_CONTAINER_AGENT")
        # Assert
        assert value == "head-test"

    def test_v2_env_sets_sac_model(self, v2_loaded_config):
        # Arrange
        config = v2_loaded_config
        # Act
        value = config.env.get("SCITEX_AGENT_CONTAINER_MODEL")
        # Assert
        assert value == "Claude Opus (1M)"

    def test_v2_env_omits_external_hub_agent(self, v2_loaded_config):
        """sac MUST NOT auto-inject external-consumer (hub etc.) env vars."""
        # Arrange
        config = v2_loaded_config
        # Act
        present = "SCITEX_HUB_AGENT" in config.env
        # Assert
        assert present is False

    def test_v2_env_omits_external_hub_model(self, v2_loaded_config):
        # Arrange
        config = v2_loaded_config
        # Act
        present = "SCITEX_HUB_MODEL" in config.env
        # Assert
        assert present is False


class TestV2AutoMkdirHook:
    def test_v2_pre_start_includes_mkdir_for_head_test(self, v2_loaded_config):
        # Arrange
        config = v2_loaded_config
        # Act
        pre_start = config.hooks.get("pre_start", [])
        has_mkdir = any("mkdir -p" in h and "head-test" in h for h in pre_start)
        # Assert
        assert has_mkdir is True


class TestV2UserOverrides:
    def test_v2_user_workdir_overrides_auto(self, v2_overrides_loaded_config):
        # Arrange
        config = v2_overrides_loaded_config
        # Act
        value = config.workdir
        # Assert
        assert value == "/custom/path"

    def test_v2_user_screen_name_overrides_auto(self, v2_overrides_loaded_config):
        # Arrange
        config = v2_overrides_loaded_config
        # Act
        value = config.screen_name
        # Assert
        assert value == "custom-screen"

    def test_v2_user_env_overrides_auto(self, v2_overrides_loaded_config):
        # Arrange
        config = v2_overrides_loaded_config
        # Act
        value = config.env.get("CLAUDE_AGENT_ID")
        # Assert
        assert value == "custom-id"

    def test_v2_auto_env_still_present_when_not_overridden(
        self, v2_overrides_loaded_config
    ):
        # Arrange
        config = v2_overrides_loaded_config
        # Act
        value = config.env.get("SCITEX_AGENT_CONTAINER_AGENT")
        # Assert
        assert value == "head-test"


class TestV2McpServersInterpolation:
    def test_v2_mcp_interpolates_metadata_name(self, v2_mcp_loaded_config):
        # Arrange
        config = v2_mcp_loaded_config
        # Act
        value = config.mcp_servers["fleet-hub"]["env"]["SCITEX_HUB_AGENT"]
        # Assert
        assert value == "head-test"

    def test_v2_mcp_leaves_non_metadata_envref_unchanged(self, v2_mcp_loaded_config):
        """${SCITEX_HUB_TOKEN} stays as-is (not a metadata ref)."""
        # Arrange
        config = v2_mcp_loaded_config
        # Act
        value = config.mcp_servers["fleet-hub"]["env"]["SCITEX_HUB_TOKEN"]
        # Assert
        assert value == "${SCITEX_HUB_TOKEN}"


class TestV2Validates:
    def test_minimal_v2_config_validates_clean(self):
        # Arrange
        path = _write_config(MINIMAL_V2_CONFIG)
        try:
            # Act
            errors = validate_config(path)
            # Assert
            assert errors == []
        finally:
            Path(path).unlink()


class TestMinimalV3RuntimeWorkspace:
    """v3 minimal config places workspace under sac's runtime root."""

    def test_v1_minimal_screen_name_is_agent_name(self, v1_minimal_loaded_config):
        # Arrange
        config = v1_minimal_loaded_config
        # Act
        value = config.screen_name
        # Assert
        assert value == "test-agent"

    def test_v1_minimal_workdir_uses_runtime_root(self):
        # Arrange — workdir is now REQUIRED in YAML (no hidden default), so the
        # runtime-root derivation is only reachable by constructing the config
        # object directly with an empty workdir (bypassing YAML validation).
        config = AgentConfig(name="test-agent", workdir="")
        expected = str(
            Path.home() / ".scitex/agent-container/runtime/agents/test-agent"
        )
        # Act
        expanded = config.expanded_workdir
        # Assert
        assert expanded == expected

    def test_v1_minimal_mcp_servers_has_builtin_sac(self, v1_minimal_loaded_config):
        """Builtin control plane (#415): a minimal spec with no MCP servers of
        its own still gets exactly the sac tools server injected by default."""
        # Arrange
        config = v1_minimal_loaded_config
        # Act
        value = config.mcp_servers
        # Assert
        assert value == {
            "scitex-agent-container": {
                "type": "stdio",
                "command": "/opt/venv-sac/bin/sac",
                "args": ["mcp", "start"],
            }
        }


@pytest.fixture
def mcp_setup_tmpdir():
    from scitex_agent_container.runtimes.mcp_config import setup_mcp_config

    with tempfile.TemporaryDirectory() as tmpdir:
        config = AgentConfig(
            name="test-agent",
            mcp_servers={
                "my-server": {
                    "type": "stdio",
                    "command": "echo",
                    "args": ["hello"],
                    "env": {"KEY": "value"},
                }
            },
        )
        setup_mcp_config(config, tmpdir)
        yield Path(tmpdir) / ".mcp.json"


class TestMcpServersToJson:
    def test_mcp_json_file_exists_after_setup(self, mcp_setup_tmpdir):
        # Arrange
        mcp_path = mcp_setup_tmpdir
        # Act
        exists = mcp_path.exists()
        # Assert
        assert exists is True

    def test_mcp_json_contains_my_server_key(self, mcp_setup_tmpdir):
        # Arrange
        mcp_path = mcp_setup_tmpdir
        # Act
        data = json.loads(mcp_path.read_text())
        # Assert
        assert "my-server" in data["mcpServers"]

    def test_mcp_json_records_my_server_command(self, mcp_setup_tmpdir):
        # Arrange
        mcp_path = mcp_setup_tmpdir
        # Act
        data = json.loads(mcp_path.read_text())
        # Assert
        assert data["mcpServers"]["my-server"]["command"] == "echo"


class TestMcpServersCleanup:
    def test_mcp_json_removed_after_cleanup(self):
        from scitex_agent_container.runtimes.mcp_config import (
            cleanup_mcp_config,
            setup_mcp_config,
        )

        # Arrange
        with tempfile.TemporaryDirectory() as tmpdir:
            config = AgentConfig(
                name="test-agent",
                mcp_servers={
                    "server-a": {"type": "stdio", "command": "a"},
                    "server-b": {"type": "stdio", "command": "b"},
                },
            )
            setup_mcp_config(config, tmpdir)
            # Act
            cleanup_mcp_config(config, tmpdir)
            mcp_path = Path(tmpdir) / ".mcp.json"
            # Assert — file removed when no servers left
            assert mcp_path.exists() is False


# ---------------------------------------------------------------------------
# spec.python-venv resolution.
# ---------------------------------------------------------------------------


def _make_venv(path):
    (path / "bin").mkdir(parents=True)
    (path / "bin" / "activate").write_text("# fake activate")


class TestPythonVenvResolutionEmpty:
    """Tests for ``spec.python-venv`` resolution.

    The chain is declared per-agent in the YAML as a list; first existing path
    wins; loud failure when none exists.
    """

    def test_empty_string_returns_empty(self):
        from scitex_agent_container.config._loaders import _resolve_python_venv

        # Arrange
        value = ""
        # Act
        result = _resolve_python_venv(value)
        # Assert
        assert result == ""

    def test_empty_list_returns_empty(self):
        from scitex_agent_container.config._loaders import _resolve_python_venv

        # Arrange
        value: list = []
        # Act
        result = _resolve_python_venv(value)
        # Assert
        assert result == ""

    def test_none_returns_empty(self):
        from scitex_agent_container.config._loaders import _resolve_python_venv

        # Arrange
        value = None
        # Act
        result = _resolve_python_venv(value)
        # Assert
        assert result == ""


class TestPythonVenvResolutionString:
    def test_string_existing_path_returns_input(self, tmp_path):
        from scitex_agent_container.config._loaders import _resolve_python_venv

        # Arrange
        v = tmp_path / "v"
        _make_venv(v)
        # Act
        result = _resolve_python_venv(str(v))
        # Assert
        assert result == str(v)

    def test_string_missing_path_raises_runtime_error(self, tmp_path):
        from scitex_agent_container.config._loaders import _resolve_python_venv

        # Arrange
        missing = str(tmp_path / "nope")
        # Act
        # Assert
        with pytest.raises(RuntimeError, match="no bin/activate"):
            _resolve_python_venv(missing)


class TestPythonVenvResolutionList:
    def test_list_first_existing_path_wins(self, tmp_path):
        from scitex_agent_container.config._loaders import _resolve_python_venv

        # Arrange
        first = tmp_path / "first"
        second = tmp_path / "second"
        _make_venv(first)
        _make_venv(second)
        # Act
        result = _resolve_python_venv([str(first), str(second)])
        # Assert
        assert result == str(first)

    def test_list_skips_missing_returns_existing(self, tmp_path):
        from scitex_agent_container.config._loaders import _resolve_python_venv

        # Arrange
        missing = tmp_path / "missing"
        present = tmp_path / "present"
        _make_venv(present)
        # Act
        result = _resolve_python_venv([str(missing), str(present)])
        # Assert
        assert result == str(present)

    def test_list_with_no_match_raises_runtime_error(self, tmp_path):
        from scitex_agent_container.config._loaders import _resolve_python_venv

        # Arrange
        a = tmp_path / "a"
        b = tmp_path / "b"
        # Act
        # Assert
        with pytest.raises(RuntimeError, match="matched no existing venv"):
            _resolve_python_venv([str(a), str(b)])

    def test_list_with_non_string_entry_raises_runtime_error(self):
        from scitex_agent_container.config._loaders import _resolve_python_venv

        # Arrange
        value = ["~/.venv", 123]
        # Act
        # Assert
        with pytest.raises(RuntimeError, match="must contain strings"):
            _resolve_python_venv(value)  # type: ignore[arg-type]

    def test_dict_input_raises_runtime_error(self):
        from scitex_agent_container.config._loaders import _resolve_python_venv

        # Arrange
        value = {"unexpected": "dict"}
        # Act
        # Assert
        with pytest.raises(RuntimeError, match="must be a string or list"):
            _resolve_python_venv(value)  # type: ignore[arg-type]


class TestPythonVenvViaConfigLoad:
    """End-to-end: YAML with ``python-venv`` list produces resolved config."""

    def test_v2_config_resolves_python_venv_from_list(self, tmp_path):
        from scitex_agent_container.config import load_config

        # Arrange
        v = tmp_path / "v"
        _make_venv(v)
        data = {
            "apiVersion": "scitex-agent-container/v3",
            "kind": "Agent",
            "metadata": {"name": "regression"},
            "spec": {
                "runtime": "apptainer",
                "host": "${HOSTNAME}",
                "workdir": "~/.scitex/agent-container/runtime/agents/regression",
                "claude": {"model": "claude-opus-4-8[1m]"},
                "apptainer": {"image": "/opt/sac/scitex.sif", "binds": []},
                "health": {"enabled": True, "interval": 60},
                "restart": {"policy": "on-failure", "max_retries": 3},
                "python-venv": [str(tmp_path / "missing"), str(v)],
            },
        }
        path = _write_config(data)
        try:
            # Act
            config = load_config(path)
            # Assert
            assert config.python_venv == str(v)
        finally:
            Path(path).unlink()
