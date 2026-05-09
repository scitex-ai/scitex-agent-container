"""Tests for config loading and validation."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import yaml

from scitex_agent_container.config import (
    AgentConfig,
    RemoteSpec,
    SkillsSpec,
    load_config,
    validate_config,
)

MINIMAL_CONFIG = {
    "apiVersion": "scitex-agent-container/v3",
    "kind": "Agent",
    "metadata": {"name": "test-agent"},
    "spec": {"runtime": "docker"},
}

FULL_CONFIG = {
    "apiVersion": "scitex-agent-container/v3",
    "kind": "Agent",
    "metadata": {
        "name": "full-agent",
        "labels": {"role": "worker", "team": "dev"},
    },
    "spec": {
        "runtime": "docker",
        "model": "opus",
        "workdir": "/tmp/test-workdir",
        "claude": {
            "channels": ["plugin:telegram@claude-plugins-official"],
            "flags": ["--dangerously-skip-permissions"],
            "session": "continue",
        },
        "env": {"MY_VAR": "my_value"},
        "screen": {"name": "full-agent"},
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
            "method": "sdk-alive",
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
    """Write a config dict to ``<tmp>/<name>/<name>.yaml`` and return its path.

    Dir-as-SSoT: the loader derives the agent name from the parent dir.
    The helper picks the dir name from ``data["metadata"]["name"]`` (a
    test-only convenience) and then **strips** that field before writing
    so the validator (which now rejects metadata.name) doesn't complain.
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


class TestLoadConfig:
    def test_minimal_config(self):
        path = _write_config(MINIMAL_CONFIG)
        config = load_config(path)
        assert config.name == "test-agent"
        assert config.runtime == "docker"
        assert config.model == "sonnet"  # default
        assert config.screen_name == "test-agent"  # auto-generated
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
        assert config.screen_name == "full-agent"
        # v3 auto-derives sac env vars on top of user env
        assert config.env["MY_VAR"] == "my_value"
        assert config.env["CLAUDE_AGENT_ID"] == "full-agent"
        # v3 auto-prepends mkdir -p {workdir}/.claude
        assert "echo pre" in config.hooks["pre_start"]
        assert any("mkdir -p" in h for h in config.hooks["pre_start"])
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

    def test_metadata_name_rejected(self):
        """Dir-as-SSoT: metadata.name in YAML is no longer accepted."""
        # Bypass _write_config (which strips metadata.name) — write raw.
        tmp_dir = Path(tempfile.mkdtemp()) / "rejected-agent"
        tmp_dir.mkdir(parents=True)
        path = tmp_dir / "rejected-agent.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "apiVersion": "scitex-agent-container/v3",
                    "kind": "Agent",
                    "metadata": {"name": "rejected-agent"},
                    "spec": {"runtime": "docker"},
                }
            )
        )
        with pytest.raises(ValueError, match="metadata.name is no longer accepted"):
            load_config(str(path))

    def test_invalid_runtime(self):
        data = {
            "apiVersion": "scitex-agent-container/v3",
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
            "apiVersion": "scitex-agent-container/v3",
            "kind": "Agent",
            "metadata": {"name": "test"},
            "spec": {
                "runtime": "docker",
                "container": {"runtime": "kubernetes"},
            },
        }
        path = _write_config(data)
        errors = validate_config(path)
        assert any("container.runtime" in e for e in errors)
        Path(path).unlink()

    def test_invalid_restart_policy(self):
        data = {
            "apiVersion": "scitex-agent-container/v3",
            "kind": "Agent",
            "metadata": {"name": "test"},
            "spec": {
                "runtime": "docker",
                "restart": {"policy": "maybe"},
            },
        }
        path = _write_config(data)
        errors = validate_config(path)
        assert any("restart.policy" in e for e in errors)
        Path(path).unlink()


class TestSkillsSpec:
    def test_default_skills(self):
        path = _write_config(MINIMAL_CONFIG)
        config = load_config(path)
        assert config.skills.required == []
        assert config.skills.available == []
        Path(path).unlink()

    def test_skills_from_yaml(self):
        data = {
            "apiVersion": "scitex-agent-container/v3",
            "kind": "Agent",
            "metadata": {"name": "skills-agent"},
            "spec": {
                "runtime": "docker",
                "skills": {
                    "required": ["quality-guards", "autonomous"],
                    "available": ["scitex", "code-review"],
                },
            },
        }
        path = _write_config(data)
        config = load_config(path)
        assert config.skills.required == ["quality-guards", "autonomous"]
        assert config.skills.available == ["scitex", "code-review"]
        Path(path).unlink()

    def test_skills_partial(self):
        data = {
            "apiVersion": "scitex-agent-container/v3",
            "kind": "Agent",
            "metadata": {"name": "partial-skills"},
            "spec": {
                "runtime": "docker",
                "skills": {
                    "required": ["speech"],
                },
            },
        }
        path = _write_config(data)
        config = load_config(path)
        assert config.skills.required == ["speech"]
        assert config.skills.available == []
        Path(path).unlink()


class TestClaudeMdGeneration:
    def test_setup_creates_claude_md(self):
        from scitex_agent_container.runtimes.claude_md import (
            setup_claude_md as _setup_claude_md,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            config = AgentConfig(
                name="test-agent",
                env={
                    "SCITEX_AGENT_CONTAINER_ROLE": "worker",
                    "SCITEX_AGENT_CONTAINER_ID": "test-agent",
                },
                skills=SkillsSpec(required=["quality-guards", "autonomous"]),
            )
            _setup_claude_md(config, tmpdir)

            claude_md = Path(tmpdir) / ".claude" / "CLAUDE.md"
            assert claude_md.exists()
            content = claude_md.read_text()
            assert '<!-- agent-container:start id="test-agent" -->' in content
            assert '<!-- agent-container:end id="test-agent" -->' in content
            assert "quality-guards" in content
            assert "autonomous" in content
            assert "Role: worker" in content

    def test_setup_preserves_existing_content(self):
        from scitex_agent_container.runtimes.claude_md import (
            setup_claude_md as _setup_claude_md,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            claude_dir = Path(tmpdir) / ".claude"
            claude_dir.mkdir(parents=True)
            claude_md = claude_dir / "CLAUDE.md"
            claude_md.write_text("# My Project\n\nSome existing content.\n")

            config = AgentConfig(name="test-agent")
            _setup_claude_md(config, tmpdir)

            content = claude_md.read_text()
            assert "# My Project" in content
            assert "Some existing content." in content
            assert '<!-- agent-container:start id="test-agent" -->' in content

    def test_setup_replaces_existing_section(self):
        from scitex_agent_container.runtimes.claude_md import (
            setup_claude_md as _setup_claude_md,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            claude_dir = Path(tmpdir) / ".claude"
            claude_dir.mkdir(parents=True)
            claude_md = claude_dir / "CLAUDE.md"
            claude_md.write_text(
                "# Header\n"
                '<!-- agent-container:start id="test-agent" -->\n'
                "old content\n"
                '<!-- agent-container:end id="test-agent" -->\n'
                "# Footer\n"
            )

            config = AgentConfig(
                name="test-agent",
                skills=SkillsSpec(required=["new-skill"]),
            )
            _setup_claude_md(config, tmpdir)

            content = claude_md.read_text()
            assert "old content" not in content
            assert "new-skill" in content
            assert "# Header" in content
            assert "# Footer" in content

    def test_cleanup_removes_section(self):
        from scitex_agent_container.runtimes.claude_md import (
            cleanup_claude_md as _cleanup_claude_md,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            claude_dir = Path(tmpdir) / ".claude"
            claude_dir.mkdir(parents=True)
            claude_md = claude_dir / "CLAUDE.md"
            claude_md.write_text(
                "# Header\n"
                '<!-- agent-container:start id="test-agent" -->\n'
                "agent section\n"
                '<!-- agent-container:end id="test-agent" -->\n'
                "# Footer\n"
            )

            config = AgentConfig(name="test-agent")
            _cleanup_claude_md(config, tmpdir)

            content = claude_md.read_text()
            assert "agent section" not in content
            assert "agent-container:start" not in content
            assert "# Header" in content
            assert "# Footer" in content

    def test_cleanup_noop_when_no_file(self):
        from scitex_agent_container.runtimes.claude_md import (
            cleanup_claude_md as _cleanup_claude_md,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            config = AgentConfig(name="test-agent")
            # Should not raise
            _cleanup_claude_md(config, tmpdir)


class TestRemoteSpec:
    def test_default_remote(self):
        """Remote spec defaults to empty (local execution)."""
        path = _write_config(MINIMAL_CONFIG)
        config = load_config(path)
        assert config.remote.host == ""
        assert config.remote.user == ""
        assert config.remote.key == ""
        assert config.remote.port == 22
        assert config.remote.is_remote is False
        Path(path).unlink()

    def test_remote_from_yaml(self):
        """Remote spec parsed from YAML config."""
        data = {
            "apiVersion": "scitex-agent-container/v3",
            "kind": "Agent",
            "metadata": {"name": "remote-agent"},
            "spec": {
                "runtime": "docker",
                "remote": {
                    "host": "mba",
                    "user": "testuser",
                },
            },
        }
        path = _write_config(data)
        config = load_config(path)
        assert config.remote.host == "mba"
        assert config.remote.user == "testuser"
        assert config.remote.port == 22
        assert config.remote.key == ""
        assert config.remote.is_remote is True
        Path(path).unlink()

    def test_remote_full_spec(self):
        """Remote spec with all fields specified."""
        data = {
            "apiVersion": "scitex-agent-container/v3",
            "kind": "Agent",
            "metadata": {"name": "remote-full"},
            "spec": {
                "runtime": "docker",
                "remote": {
                    "host": "192.168.1.100",
                    "user": "deploy",
                    "key": "/home/deploy/.ssh/id_ed25519",
                    "port": 2222,
                },
            },
        }
        path = _write_config(data)
        config = load_config(path)
        assert config.remote.host == "192.168.1.100"
        assert config.remote.user == "deploy"
        assert config.remote.key == "/home/deploy/.ssh/id_ed25519"
        assert config.remote.port == 2222
        assert config.remote.is_remote is True
        Path(path).unlink()

    def test_remote_spec_dataclass(self):
        """RemoteSpec dataclass works standalone."""
        r = RemoteSpec()
        assert r.is_remote is False

        r = RemoteSpec(host="myhost", user="me")
        assert r.is_remote is True

    def test_agent_config_with_remote(self):
        """AgentConfig accepts RemoteSpec."""
        config = AgentConfig(
            name="test",
            remote=RemoteSpec(host="server1", user="admin"),
        )
        assert config.remote.is_remote is True
        assert config.remote.host == "server1"

    def test_login_shell_default(self):
        """login_shell defaults to True."""
        path = _write_config(MINIMAL_CONFIG)
        config = load_config(path)
        assert config.remote.login_shell is True
        Path(path).unlink()

    def test_login_shell_from_yaml(self):
        """login_shell can be set to False in YAML."""
        data = {
            "apiVersion": "scitex-agent-container/v3",
            "kind": "Agent",
            "metadata": {"name": "test-login-shell"},
            "spec": {
                "runtime": "docker",
                "remote": {
                    "host": "fast-host",
                    "user": "deploy",
                    "login_shell": False,
                },
            },
        }
        path = _write_config(data)
        config = load_config(path)
        assert config.remote.login_shell is False
        assert config.remote.host == "fast-host"
        Path(path).unlink()
