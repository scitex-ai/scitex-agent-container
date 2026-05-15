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

    data = copy.deepcopy(data)
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


MINIMAL_V1_CONFIG = {
    "apiVersion": "scitex-agent-container/v3",
    "kind": "Agent",
    "metadata": {"name": "test-agent"},
    "spec": {"runtime": "apptainer"},
}

MINIMAL_V2_CONFIG = {
    "apiVersion": "scitex-agent-container/v3",
    "kind": "Agent",
    "metadata": {
        "name": "head-test",
        "labels": {"role": "head", "team": "orochi", "machine": "test-box"},
    },
    "spec": {
        "runtime": "apptainer",
        # v3-realign: model moved to spec.claude.model.
        "claude": {"model": "opus[1m]"},
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
        # v3-realign: model moved to spec.claude.model.
        "claude": {"model": "sonnet"},
        "mcp_servers": {
            "scitex-orochi": {
                "type": "stdio",
                "command": "bun",
                "args": ["run", "~/proj/scitex-orochi/ts/mcp_channel.ts"],
                "env": {
                    "SCITEX_OROCHI_URL": "wss://scitex-orochi.com",
                    "SCITEX_OROCHI_AGENT": "${metadata.name}",
                    "SCITEX_OROCHI_TOKEN": "${SCITEX_OROCHI_TOKEN}",
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
            # v3-realign: env moved to spec.apptainer.env.
            "apptainer": {"env": {"CLAUDE_AGENT_ID": "custom-id"}},
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
    def test_v2_auto_derives_runtime_workdir(self, v2_loaded_config):
        # Arrange
        config = v2_loaded_config
        # Act
        workdir = config.workdir
        # Assert
        assert workdir == "~/.scitex/agent-container/runtime/agents/head-test"


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

    def test_v2_env_omits_external_orochi_agent(self, v2_loaded_config):
        """sac MUST NOT auto-inject external-consumer (orochi etc.) env vars."""
        # Arrange
        config = v2_loaded_config
        # Act
        present = "SCITEX_OROCHI_AGENT" in config.env
        # Assert
        assert present is False

    def test_v2_env_omits_external_orochi_model(self, v2_loaded_config):
        # Arrange
        config = v2_loaded_config
        # Act
        present = "SCITEX_OROCHI_MODEL" in config.env
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
        value = config.mcp_servers["scitex-orochi"]["env"]["SCITEX_OROCHI_AGENT"]
        # Assert
        assert value == "head-test"

    def test_v2_mcp_leaves_non_metadata_envref_unchanged(self, v2_mcp_loaded_config):
        """${SCITEX_OROCHI_TOKEN} stays as-is (not a metadata ref)."""
        # Arrange
        config = v2_mcp_loaded_config
        # Act
        value = config.mcp_servers["scitex-orochi"]["env"]["SCITEX_OROCHI_TOKEN"]
        # Assert
        assert value == "${SCITEX_OROCHI_TOKEN}"


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

    def test_v1_minimal_workdir_uses_runtime_root(self, v1_minimal_loaded_config):
        # Arrange
        config = v1_minimal_loaded_config
        # Act
        value = config.workdir
        # Assert
        assert value == "~/.scitex/agent-container/runtime/agents/test-agent"

    def test_v1_minimal_mcp_servers_empty(self, v1_minimal_loaded_config):
        # Arrange
        config = v1_minimal_loaded_config
        # Act
        value = config.mcp_servers
        # Assert
        assert value == {}


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


def _dot_dir(defdir: str) -> Path:
    d = Path(defdir) / "dot_claude"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def deploy_claude_md_dest():
    """Deploy a templated CLAUDE.md and yield the rendered destination path."""
    from scitex_agent_container.runtimes._dot_claude import deploy_dot_claude

    with (
        tempfile.TemporaryDirectory() as defdir,
        tempfile.TemporaryDirectory() as workdir,
    ):
        (_dot_dir(defdir) / "CLAUDE.md").write_text(
            "## Agent: ${metadata.name}\n- Role: ${metadata.labels.role}\n"
        )
        config = AgentConfig(
            name="my-agent",
            labels={"role": "head"},
            config_path=str(Path(defdir) / "spec.yaml"),
        )
        deploy_dot_claude(config, workdir)
        yield Path(workdir) / "CLAUDE.md"


class TestDeployClaudeMd:
    """Integration coverage of dot_claude/ deploy against a real AgentConfig."""

    def test_deploy_creates_dest_file(self, deploy_claude_md_dest):
        # Arrange
        dest = deploy_claude_md_dest
        # Act
        exists = dest.exists()
        # Assert
        assert exists is True

    def test_deploy_interpolates_agent_name(self, deploy_claude_md_dest):
        # Arrange
        dest = deploy_claude_md_dest
        # Act
        content = dest.read_text()
        # Assert
        assert "my-agent" in content

    def test_deploy_interpolates_role_label(self, deploy_claude_md_dest):
        # Arrange
        dest = deploy_claude_md_dest
        # Act
        content = dest.read_text()
        # Assert
        assert "head" in content

    def test_deploy_wraps_in_managed_marker(self, deploy_claude_md_dest):
        # Arrange
        dest = deploy_claude_md_dest
        # Act
        content = dest.read_text()
        # Assert
        assert "Start of scitex-agent-container generated section" in content


@pytest.fixture
def deploy_preserves_tail_dest():
    from scitex_agent_container.runtimes._dot_claude import deploy_dot_claude

    with (
        tempfile.TemporaryDirectory() as defdir,
        tempfile.TemporaryDirectory() as workdir,
    ):
        dest = Path(workdir) / "CLAUDE.md"
        dest.write_text(
            "<!-- Start of scitex-agent-container generated section (old) -->\n"
            "## Old managed\n"
            "<!-- End of scitex-agent-container generated section -->\n"
            "# My notes\nAgent wrote this.\n"
        )
        (_dot_dir(defdir) / "CLAUDE.md").write_text("## Managed section\n")
        config = AgentConfig(
            name="my-agent",
            config_path=str(Path(defdir) / "spec.yaml"),
        )
        deploy_dot_claude(config, workdir)
        yield dest


class TestDeployPreservesUserTail:
    def test_deploy_keeps_user_notes_heading(self, deploy_preserves_tail_dest):
        # Arrange
        dest = deploy_preserves_tail_dest
        # Act
        content = dest.read_text()
        # Assert
        assert "My notes" in content

    def test_deploy_keeps_user_body_line(self, deploy_preserves_tail_dest):
        # Arrange
        dest = deploy_preserves_tail_dest
        # Act
        content = dest.read_text()
        # Assert
        assert "Agent wrote this." in content

    def test_deploy_writes_new_managed_section(self, deploy_preserves_tail_dest):
        # Arrange
        dest = deploy_preserves_tail_dest
        # Act
        content = dest.read_text()
        # Assert
        assert "Managed section" in content

    def test_deploy_overwrites_old_managed_body(self, deploy_preserves_tail_dest):
        # Arrange
        dest = deploy_preserves_tail_dest
        # Act
        content = dest.read_text()
        # Assert
        assert "Old managed" not in content


@pytest.fixture
def deploy_mcp_json_server():
    """Deploy a templated .mcp.json via real env var, yield the test-server dict."""
    import os

    from scitex_agent_container.runtimes._dot_claude import deploy_dot_claude

    with (
        tempfile.TemporaryDirectory() as defdir,
        tempfile.TemporaryDirectory() as workdir,
    ):
        (_dot_dir(defdir) / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "test-server": {
                            "type": "stdio",
                            "command": "echo",
                            "env": {
                                "AGENT": "${metadata.name}",
                                "TOKEN": "${TEST_TOKEN_VAR}",
                            },
                        }
                    }
                }
            )
        )
        saved = os.environ.get("TEST_TOKEN_VAR")
        os.environ["TEST_TOKEN_VAR"] = "secret123"
        try:
            config = AgentConfig(
                name="my-agent",
                config_path=str(Path(defdir) / "spec.yaml"),
            )
            deploy_dot_claude(config, workdir)
            dest = Path(workdir) / ".mcp.json"
            data = json.loads(dest.read_text())
            yield dest, data["mcpServers"]["test-server"]
        finally:
            if saved is None:
                os.environ.pop("TEST_TOKEN_VAR", None)
            else:
                os.environ["TEST_TOKEN_VAR"] = saved


class TestDeployMcpJson:
    def test_deploy_creates_mcp_json_file(self, deploy_mcp_json_server):
        # Arrange
        dest, _server = deploy_mcp_json_server
        # Act
        exists = dest.exists()
        # Assert
        assert exists is True

    def test_deploy_interpolates_agent_name_into_env(self, deploy_mcp_json_server):
        # Arrange
        _dest, server = deploy_mcp_json_server
        # Act
        value = server["env"]["AGENT"]
        # Assert
        assert value == "my-agent"

    def test_deploy_interpolates_real_env_var(self, deploy_mcp_json_server):
        # Arrange
        _dest, server = deploy_mcp_json_server
        # Act
        value = server["env"]["TOKEN"]
        # Assert
        assert value == "secret123"


@pytest.fixture
def deploy_state_md_dest():
    from scitex_agent_container.runtimes._dot_claude import deploy_dot_claude

    with (
        tempfile.TemporaryDirectory() as defdir,
        tempfile.TemporaryDirectory() as workdir,
    ):
        (_dot_dir(defdir) / "state.md").write_text(
            "# Handover for ${metadata.name}\n- inflight: scholar\n"
        )
        config = AgentConfig(
            name="orchestrator",
            config_path=str(Path(defdir) / "spec.yaml"),
        )
        deploy_dot_claude(config, workdir)
        yield Path(workdir) / "state.md"


class TestDeployStateMd:
    def test_deploy_creates_state_md_file(self, deploy_state_md_dest):
        # Arrange
        dest = deploy_state_md_dest
        # Act
        exists = dest.exists()
        # Assert
        assert exists is True

    def test_deploy_state_md_interpolates_name(self, deploy_state_md_dest):
        # Arrange
        dest = deploy_state_md_dest
        # Act
        content = dest.read_text()
        # Assert
        assert "Handover for orchestrator" in content

    def test_deploy_state_md_preserves_static_lines(self, deploy_state_md_dest):
        # Arrange
        dest = deploy_state_md_dest
        # Act
        content = dest.read_text()
        # Assert
        assert "inflight: scholar" in content


@pytest.fixture
def deploy_noop_workdir():
    """defdir has no dot_claude/ — deploy_dot_claude must silently no-op."""
    from scitex_agent_container.runtimes._dot_claude import deploy_dot_claude

    with (
        tempfile.TemporaryDirectory() as defdir,
        tempfile.TemporaryDirectory() as workdir,
    ):
        config = AgentConfig(
            name="orchestrator",
            config_path=str(Path(defdir) / "spec.yaml"),
        )
        deploy_dot_claude(config, workdir)
        yield Path(workdir)


class TestDeployNoopWithoutDotClaudeDir:
    def test_noop_does_not_create_state_md(self, deploy_noop_workdir):
        # Arrange
        workdir = deploy_noop_workdir
        # Act
        exists = (workdir / "state.md").exists()
        # Assert
        assert exists is False

    def test_noop_does_not_create_claude_md(self, deploy_noop_workdir):
        # Arrange
        workdir = deploy_noop_workdir
        # Act
        exists = (workdir / "CLAUDE.md").exists()
        # Assert
        assert exists is False


class TestCleanupStateMd:
    def test_state_md_present_after_deploy(self):
        from scitex_agent_container.runtimes._dot_claude import deploy_dot_claude

        # Arrange
        with (
            tempfile.TemporaryDirectory() as defdir,
            tempfile.TemporaryDirectory() as workdir,
        ):
            (_dot_dir(defdir) / "state.md").write_text("snapshot\n")
            config = AgentConfig(
                name="orchestrator",
                config_path=str(Path(defdir) / "spec.yaml"),
            )
            # Act
            deploy_dot_claude(config, workdir)
            # Assert
            assert (Path(workdir) / "state.md").exists() is True

    def test_state_md_removed_after_cleanup(self):
        from scitex_agent_container.runtimes._dot_claude import (
            cleanup_dot_claude,
            deploy_dot_claude,
        )

        # Arrange
        with (
            tempfile.TemporaryDirectory() as defdir,
            tempfile.TemporaryDirectory() as workdir,
        ):
            (_dot_dir(defdir) / "state.md").write_text("snapshot\n")
            config = AgentConfig(
                name="orchestrator",
                config_path=str(Path(defdir) / "spec.yaml"),
            )
            deploy_dot_claude(config, workdir)
            # Act
            cleanup_dot_claude(config, workdir)
            # Assert
            assert (Path(workdir) / "state.md").exists() is False


@pytest.fixture
def cleanup_claude_md_dest():
    """Deploy managed CLAUDE.md, append user content, then cleanup; yield dest."""
    from scitex_agent_container.runtimes._dot_claude import (
        cleanup_dot_claude,
        deploy_dot_claude,
    )

    with (
        tempfile.TemporaryDirectory() as defdir,
        tempfile.TemporaryDirectory() as workdir,
    ):
        (_dot_dir(defdir) / "CLAUDE.md").write_text("## Managed\n")
        config = AgentConfig(
            name="my-agent",
            config_path=str(Path(defdir) / "spec.yaml"),
        )
        dest = Path(workdir) / "CLAUDE.md"
        deploy_dot_claude(config, workdir)
        dest.write_text(dest.read_text() + "# User content\n")
        cleanup_dot_claude(config, workdir)
        yield dest


class TestCleanupClaudeMd:
    def test_cleanup_keeps_user_content(self, cleanup_claude_md_dest):
        # Arrange
        dest = cleanup_claude_md_dest
        # Act
        content = dest.read_text()
        # Assert
        assert "User content" in content

    def test_cleanup_strips_managed_heading(self, cleanup_claude_md_dest):
        # Arrange
        dest = cleanup_claude_md_dest
        # Act
        content = dest.read_text()
        # Assert
        assert "Managed" not in content


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
