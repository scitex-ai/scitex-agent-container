"""Tests for v2 config loading, auto-derivation, and mcp_servers."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import yaml

from scitex_agent_container.config import AgentConfig, load_config, validate_config


def _write_config(data: dict) -> str:
    """Write a config dict to a temp file and return its path."""
    tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
    yaml.safe_dump(data, tmp)
    tmp.close()
    return tmp.name


MINIMAL_V1_CONFIG = {
    "apiVersion": "cld-agent/v1",
    "kind": "Agent",
    "metadata": {"name": "test-agent"},
    "spec": {"runtime": "claude-code"},
}

MINIMAL_V2_CONFIG = {
    "apiVersion": "scitex-agent-container/v2",
    "kind": "Agent",
    "metadata": {
        "name": "head-test",
        "labels": {"role": "head", "team": "orochi", "machine": "test-box"},
    },
    "spec": {
        "runtime": "claude-code",
        "model": "opus[1m]",
    },
}

V2_WITH_MCP = {
    "apiVersion": "scitex-agent-container/v2",
    "kind": "Agent",
    "metadata": {
        "name": "head-test",
        "labels": {"role": "head"},
    },
    "spec": {
        "runtime": "claude-code",
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
    def test_v2_auto_derived_workdir(self):
        path = _write_config(MINIMAL_V2_CONFIG)
        config = load_config(path)
        assert config.workdir == "~/.scitex/orochi/workspaces/head-test"
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
        assert config.env["SCITEX_OROCHI_AGENT"] == "head-test"
        assert config.env["SCITEX_OROCHI_MODEL"] == "Claude Opus (1M)"
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
        assert config.env["SCITEX_OROCHI_AGENT"] == "head-test"
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

    def test_v1_still_works(self):
        """Ensure v1 configs are unaffected by v2 changes."""
        path = _write_config(MINIMAL_V1_CONFIG)
        config = load_config(path)
        assert config.screen_name == "cld-test-agent"
        assert config.workdir == "~/proj"
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

    def test_deploy_preserves_existing_content(self):
        from scitex_agent_container.runtimes.src_files import deploy_src_claude_md

        with (
            tempfile.TemporaryDirectory() as defdir,
            tempfile.TemporaryDirectory() as workdir,
        ):
            # Pre-existing CLAUDE.md with agent content
            dest = Path(workdir) / "CLAUDE.md"
            dest.write_text("# My notes\nAgent wrote this.\n")

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
            # Pre-existing content + deploy
            dest = Path(workdir) / "CLAUDE.md"
            dest.write_text("# User content\n")
            deploy_src_claude_md(config, workdir)
            assert "Managed" in dest.read_text()

            # Cleanup removes managed section, keeps user content
            cleanup_src_claude_md(config, workdir)
            content = dest.read_text()
            assert "User content" in content
            assert "Managed" not in content
