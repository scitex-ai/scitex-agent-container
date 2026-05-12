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
    "spec": {"runtime": "docker"},
}

MINIMAL_V2_CONFIG = {
    "apiVersion": "scitex-agent-container/v3",
    "kind": "Agent",
    "metadata": {
        "name": "head-test",
        "labels": {"role": "head", "team": "orochi", "machine": "test-box"},
    },
    "spec": {
        "runtime": "docker",
        "model": "opus[1m]",
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
        "runtime": "docker",
        "model": "sonnet",
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


class TestV2Config:
    def test_v2_auto_derived_workdir(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        path = _write_config(MINIMAL_V2_CONFIG)
        config = load_config(path)
        assert config.workdir == "~/.scitex/agent-container/runtime/workspaces/head-test"
        Path(path).unlink()

    def test_v2_screen_name(self):
        """v2 screen_name is {name}, not cld-{name}."""
        path = _write_config(MINIMAL_V2_CONFIG)
        config = load_config(path)
        assert config.screen_name == "head-test"
        Path(path).unlink()

    def test_v2_auto_derived_env(self):
        path = _write_config(MINIMAL_V2_CONFIG)
        config = load_config(path)
        assert config.env["CLAUDE_AGENT_ID"] == "head-test"
        assert config.env["CLAUDE_AGENT_ROLE"] == "head"
        assert config.env["SCITEX_AGENT_CONTAINER_AGENT"] == "head-test"
        assert config.env["SCITEX_AGENT_CONTAINER_MODEL"] == "Claude Opus (1M)"
        # sac MUST NOT auto-inject external-consumer (orochi etc.) env vars.
        assert "SCITEX_OROCHI_AGENT" not in config.env
        assert "SCITEX_OROCHI_MODEL" not in config.env
        Path(path).unlink()

    def test_v2_auto_mkdir_hook(self):
        path = _write_config(MINIMAL_V2_CONFIG)
        config = load_config(path)
        pre_start = config.hooks.get("pre_start", [])
        assert any("mkdir -p" in h and "head-test" in h for h in pre_start)
        Path(path).unlink()

    def test_v2_user_overrides(self):
        data = {
            **MINIMAL_V2_CONFIG,
            "spec": {
                **MINIMAL_V2_CONFIG["spec"],
                "workdir": "/custom/path",
                "screen": {"name": "custom-screen"},
                "env": {"CLAUDE_AGENT_ID": "custom-id"},
            },
        }
        path = _write_config(data)
        config = load_config(path)
        assert config.workdir == "/custom/path"
        assert config.screen_name == "custom-screen"
        assert config.env["CLAUDE_AGENT_ID"] == "custom-id"
        # Auto-derived values still present where not overridden
        assert config.env["SCITEX_AGENT_CONTAINER_AGENT"] == "head-test"
        Path(path).unlink()

    def test_v2_mcp_servers_interpolation(self):
        path = _write_config(V2_WITH_MCP)
        config = load_config(path)
        orochi = config.mcp_servers["scitex-orochi"]
        assert orochi["env"]["SCITEX_OROCHI_AGENT"] == "head-test"
        # ${SCITEX_OROCHI_TOKEN} stays as-is (not a metadata ref)
        assert orochi["env"]["SCITEX_OROCHI_TOKEN"] == "${SCITEX_OROCHI_TOKEN}"
        Path(path).unlink()

    def test_v2_validates(self):
        path = _write_config(MINIMAL_V2_CONFIG)
        errors = validate_config(path)
        assert errors == []
        Path(path).unlink()

    def test_minimal_v3_uses_runtime_workspace(self):
        """v3 minimal config places workspace under sac's runtime root."""
        path = _write_config(MINIMAL_V1_CONFIG)
        config = load_config(path)
        assert config.screen_name == "test-agent"
        assert config.workdir == "~/.scitex/agent-container/runtime/workspaces/test-agent"
        assert config.mcp_servers == {}
        Path(path).unlink()


class TestV2McpConfig:
    def test_mcp_servers_to_json(self):
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
            mcp_path = Path(tmpdir) / ".mcp.json"
            assert mcp_path.exists()
            data = json.loads(mcp_path.read_text())
            assert "my-server" in data["mcpServers"]
            assert data["mcpServers"]["my-server"]["command"] == "echo"

    def test_mcp_servers_cleanup(self):
        from scitex_agent_container.runtimes.mcp_config import (
            cleanup_mcp_config,
            setup_mcp_config,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            config = AgentConfig(
                name="test-agent",
                mcp_servers={
                    "server-a": {"type": "stdio", "command": "a"},
                    "server-b": {"type": "stdio", "command": "b"},
                },
            )
            setup_mcp_config(config, tmpdir)
            cleanup_mcp_config(config, tmpdir)
            mcp_path = Path(tmpdir) / ".mcp.json"
            # File removed when no servers left
            assert not mcp_path.exists()


class TestSrcFiles:
    def test_deploy_src_claude_md(self):
        from scitex_agent_container.runtimes.src_files import deploy_src_claude_md

        with (
            tempfile.TemporaryDirectory() as defdir,
            tempfile.TemporaryDirectory() as workdir,
        ):
            # Write src_CLAUDE.md in definition dir
            src = Path(defdir) / "src_CLAUDE.md"
            src.write_text(
                "## Agent: ${metadata.name}\n- Role: ${metadata.labels.role}\n"
            )

            config = AgentConfig(
                name="my-agent",
                labels={"role": "head"},
                config_path=str(Path(defdir) / "agent.yaml"),
            )
            deploy_src_claude_md(config, workdir)

            dest = Path(workdir) / "CLAUDE.md"
            assert dest.exists()
            content = dest.read_text()
            assert "my-agent" in content
            assert "head" in content
            assert "Start of scitex-agent-container generated section" in content

    def test_deploy_preserves_user_tail(self):
        from scitex_agent_container.runtimes.src_files import deploy_src_claude_md

        with (
            tempfile.TemporaryDirectory() as defdir,
            tempfile.TemporaryDirectory() as workdir,
        ):
            # Pre-existing CLAUDE.md with markers + user tail after End
            dest = Path(workdir) / "CLAUDE.md"
            dest.write_text(
                "<!-- Start of scitex-agent-container generated section (old) -->\n"
                "## Old managed\n"
                "<!-- End of scitex-agent-container generated section -->\n"
                "# My notes\nAgent wrote this.\n"
            )

            src = Path(defdir) / "src_CLAUDE.md"
            src.write_text("## Managed section\n")

            config = AgentConfig(
                name="my-agent",
                config_path=str(Path(defdir) / "agent.yaml"),
            )
            deploy_src_claude_md(config, workdir)

            content = dest.read_text()
            assert "My notes" in content
            assert "Agent wrote this." in content
            assert "Managed section" in content
            assert "Old managed" not in content

    def test_deploy_src_mcp_json(self):
        import os

        from scitex_agent_container.runtimes.src_files import deploy_src_mcp_json

        with (
            tempfile.TemporaryDirectory() as defdir,
            tempfile.TemporaryDirectory() as workdir,
        ):
            src = Path(defdir) / "src_mcp.json"
            src.write_text(
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

            os.environ["TEST_TOKEN_VAR"] = "secret123"
            try:
                config = AgentConfig(
                    name="my-agent",
                    config_path=str(Path(defdir) / "agent.yaml"),
                )
                deploy_src_mcp_json(config, workdir)
            finally:
                del os.environ["TEST_TOKEN_VAR"]

            dest = Path(workdir) / ".mcp.json"
            assert dest.exists()
            data = json.loads(dest.read_text())
            server = data["mcpServers"]["test-server"]
            assert server["env"]["AGENT"] == "my-agent"
            assert server["env"]["TOKEN"] == "secret123"

    def test_deploy_src_state_md(self):
        from scitex_agent_container.runtimes.src_files import deploy_src_state_md

        with (
            tempfile.TemporaryDirectory() as defdir,
            tempfile.TemporaryDirectory() as workdir,
        ):
            src = Path(defdir) / "src_state.md"
            src.write_text("# Handover for ${metadata.name}\n- inflight: scholar\n")

            config = AgentConfig(
                name="orchestrator",
                config_path=str(Path(defdir) / "agent.yaml"),
            )
            deploy_src_state_md(config, workdir)

            dest = Path(workdir) / "state.md"
            assert dest.exists()
            content = dest.read_text()
            assert "Handover for orchestrator" in content
            assert "inflight: scholar" in content

    def test_deploy_src_state_md_noop_without_source(self):
        from scitex_agent_container.runtimes.src_files import deploy_src_state_md

        with (
            tempfile.TemporaryDirectory() as defdir,
            tempfile.TemporaryDirectory() as workdir,
        ):
            config = AgentConfig(
                name="orchestrator",
                config_path=str(Path(defdir) / "agent.yaml"),
            )
            # No src_state.md in defdir — must be a silent no-op.
            deploy_src_state_md(config, workdir)
            assert not (Path(workdir) / "state.md").exists()

    def test_cleanup_src_state_md_removes_workspace_file(self):
        from scitex_agent_container.runtimes.src_files import (
            cleanup_src_state_md,
            deploy_src_state_md,
        )

        with (
            tempfile.TemporaryDirectory() as defdir,
            tempfile.TemporaryDirectory() as workdir,
        ):
            (Path(defdir) / "src_state.md").write_text("snapshot\n")
            config = AgentConfig(
                name="orchestrator",
                config_path=str(Path(defdir) / "agent.yaml"),
            )
            deploy_src_state_md(config, workdir)
            assert (Path(workdir) / "state.md").exists()

            cleanup_src_state_md(config, workdir)
            assert not (Path(workdir) / "state.md").exists()

    def test_cleanup_src_claude_md(self):
        from scitex_agent_container.runtimes.src_files import (
            cleanup_src_claude_md,
            deploy_src_claude_md,
        )

        with (
            tempfile.TemporaryDirectory() as defdir,
            tempfile.TemporaryDirectory() as workdir,
        ):
            src = Path(defdir) / "src_CLAUDE.md"
            src.write_text("## Managed\n")

            config = AgentConfig(
                name="my-agent",
                config_path=str(Path(defdir) / "agent.yaml"),
            )
            # Deploy first (creates markers), then add user content after End
            dest = Path(workdir) / "CLAUDE.md"
            deploy_src_claude_md(config, workdir)
            # Append user content after the guide comment
            content = dest.read_text()
            dest.write_text(content + "# User content\n")
            assert "Managed" in dest.read_text()

            # Cleanup removes managed section + guide, keeps user content
            cleanup_src_claude_md(config, workdir)
            content = dest.read_text()
            assert "User content" in content
            assert "Managed" not in content
            assert "guide" not in content.lower() or "custom content" not in content


class TestPythonVenvResolution:
    """Tests for ``spec.python-venv`` resolution (replaces the old
    ``venv: auto`` magic). The chain is now declared per-agent in the
    YAML as a list; first existing path wins; loud failure when none
    exists.
    """

    @staticmethod
    def _make_venv(path):
        (path / "bin").mkdir(parents=True)
        (path / "bin" / "activate").write_text("# fake activate")

    def test_empty_returns_empty(self):
        from scitex_agent_container.config._loaders import _resolve_python_venv

        assert _resolve_python_venv("") == ""
        assert _resolve_python_venv([]) == ""
        assert _resolve_python_venv(None) == ""

    def test_string_existing_returns_path(self, tmp_path):
        from scitex_agent_container.config._loaders import _resolve_python_venv

        v = tmp_path / "v"
        self._make_venv(v)
        assert _resolve_python_venv(str(v)) == str(v)

    def test_string_missing_raises(self, tmp_path):
        from scitex_agent_container.config._loaders import _resolve_python_venv

        with pytest.raises(RuntimeError, match="no bin/activate"):
            _resolve_python_venv(str(tmp_path / "nope"))

    def test_list_first_existing_wins(self, tmp_path):
        from scitex_agent_container.config._loaders import _resolve_python_venv

        first = tmp_path / "first"
        second = tmp_path / "second"
        self._make_venv(first)
        self._make_venv(second)
        assert _resolve_python_venv([str(first), str(second)]) == str(first)

    def test_list_skips_missing_picks_existing(self, tmp_path):
        from scitex_agent_container.config._loaders import _resolve_python_venv

        missing = tmp_path / "missing"
        present = tmp_path / "present"
        self._make_venv(present)
        assert _resolve_python_venv([str(missing), str(present)]) == str(present)

    def test_list_none_match_raises(self, tmp_path):
        from scitex_agent_container.config._loaders import _resolve_python_venv

        a = tmp_path / "a"
        b = tmp_path / "b"
        with pytest.raises(RuntimeError, match="matched no existing venv"):
            _resolve_python_venv([str(a), str(b)])

    def test_list_non_string_raises(self):
        from scitex_agent_container.config._loaders import _resolve_python_venv

        with pytest.raises(RuntimeError, match="must contain strings"):
            _resolve_python_venv(["~/.venv", 123])  # type: ignore[list-item]

    def test_invalid_type_raises(self):
        from scitex_agent_container.config._loaders import _resolve_python_venv

        with pytest.raises(RuntimeError, match="must be a string or list"):
            _resolve_python_venv({"unexpected": "dict"})  # type: ignore[arg-type]

    def test_via_v2_config_load(self, tmp_path):
        """End-to-end: YAML with ``python-venv`` list produces resolved config."""
        from scitex_agent_container.config import load_config

        v = tmp_path / "v"
        self._make_venv(v)

        data = {
            "apiVersion": "scitex-agent-container/v3",
            "kind": "Agent",
            "metadata": {"name": "regression"},
            "spec": {
                "runtime": "docker",
                "python-venv": [str(tmp_path / "missing"), str(v)],
            },
        }
        path = _write_config(data)
        try:
            config = load_config(path)
            assert config.python_venv == str(v)
        finally:
            Path(path).unlink()
