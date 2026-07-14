"""Tests for config loading and validation."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import yaml

from scitex_agent_container.config import (
    AgentConfig,
    SkillsSpec,
    load_config,
    validate_config,
)

# No-hidden-defaults (operator directive 2026-06-23): every applicable
# author field is REQUIRED at the YAML/load layer, so each inline fixture
# spec must declare runtime, workdir, host, apptainer.{image,binds},
# health.{enabled,interval}, restart.{policy,max_retries} (+ claude.model
# for kind: Agent) or load_config raises ValueError.
MINIMAL_CONFIG = {
    "apiVersion": "scitex-agent-container/v3",
    "kind": "Agent",
    "metadata": {"name": "test-agent"},
    "spec": {
        "runtime": "apptainer",
        "host": "${HOSTNAME}",
        "workdir": "/tmp/test-agent-workdir",
        "claude": {"model": "claude-opus-4-8[1m]"},
        "apptainer": {"image": "/opt/sac/scitex.sif", "binds": []},
        "health": {"enabled": True, "interval": 60},
        "restart": {"policy": "on-failure", "max_retries": 3},
    },
}

FULL_CONFIG = {
    "apiVersion": "scitex-agent-container/v3",
    "kind": "Agent",
    "metadata": {
        "name": "full-agent",
        "labels": {"role": "worker", "team": "dev"},
    },
    "spec": {
        "runtime": "apptainer",
        "host": "${HOSTNAME}",
        "workdir": "/tmp/test-workdir",
        "claude": {
            "model": "opus",
            "channels": ["plugin:telegram@claude-plugins-official"],
            "flags": ["--dangerously-skip-permissions"],
            "session": "continue",
        },
        "apptainer": {
            "image": "/opt/sac/scitex.sif",
            "binds": [],
            "env": {"MY_VAR": "my_value"},
        },
        "screen": {"name": "full-agent"},
        "container": {
            "runtime": "apptainer",
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


@pytest.fixture
def minimal_loaded_config():
    path = _write_config(MINIMAL_CONFIG)
    cfg = load_config(path)
    yield cfg
    Path(path).unlink(missing_ok=True)


@pytest.fixture
def full_loaded_config():
    path = _write_config(FULL_CONFIG)
    cfg = load_config(path)
    yield cfg
    Path(path).unlink(missing_ok=True)


class TestLoadMinimalConfig:
    def test_minimal_loads_name(self, minimal_loaded_config):
        # Arrange
        config = minimal_loaded_config
        # Act
        name = config.name
        # Assert
        assert name == "test-agent"

    def test_minimal_loads_runtime(self, minimal_loaded_config):
        # Arrange
        config = minimal_loaded_config
        # Act
        runtime = config.runtime
        # Assert — MINIMAL_CONFIG explicitly pins runtime: apptainer.
        # (The default-when-omitted is tui — see
        # test_v3_spec_structure.test_runtime_defaults_to_tui_when_omitted.)
        assert runtime == "apptainer"

    def test_minimal_defaults_model_to_sonnet(self):
        # Arrange — claude.model is now REQUIRED in YAML (no hidden default),
        # so the model-defaults-to-sonnet behaviour is only reachable by
        # constructing the config object directly (bypassing YAML validation).
        config = AgentConfig(name="test-agent")
        # Act
        model = config.model
        # Assert
        assert model == "sonnet"

    def test_minimal_auto_generates_screen_name(self, minimal_loaded_config):
        # Arrange
        config = minimal_loaded_config
        # Act
        screen_name = config.screen_name
        # Assert
        assert screen_name == "test-agent"


class TestLoadFullConfig:
    def test_full_loads_name(self, full_loaded_config):
        # Arrange
        config = full_loaded_config
        # Act
        value = config.name
        # Assert
        assert value == "full-agent"

    def test_full_loads_model(self, full_loaded_config):
        # Arrange
        config = full_loaded_config
        # Act
        value = config.model
        # Assert
        assert value == "opus"

    def test_full_loads_labels(self, full_loaded_config):
        # Arrange
        config = full_loaded_config
        # Act
        value = config.labels
        # Assert
        assert value == {"role": "worker", "team": "dev"}

    def test_full_loads_claude_channels(self, full_loaded_config):
        # Arrange
        config = full_loaded_config
        # Act — server:sac is the builtin control-plane channel auto-injected
        # by load_config for every agent (operator directive 2026-06-16).
        value = config.claude.channels
        # Assert
        assert value == ["plugin:telegram@claude-plugins-official", "server:sac"]

    def test_full_loads_claude_session(self, full_loaded_config):
        # Arrange
        config = full_loaded_config
        # Act
        value = config.claude.session
        # Assert
        assert value == "continue"

    def test_full_loads_container_runtime(self, full_loaded_config):
        # Arrange
        config = full_loaded_config
        # Act
        value = config.container.runtime
        # Assert
        assert value == "apptainer"

    def test_full_loads_container_image(self, full_loaded_config):
        # Arrange
        config = full_loaded_config
        # Act
        value = config.container.image
        # Assert
        assert value == "my-image:latest"

    def test_full_loads_container_network(self, full_loaded_config):
        # Arrange
        config = full_loaded_config
        # Act
        value = config.container.network
        # Assert
        assert value == "bridge"

    def test_full_loads_health_enabled(self, full_loaded_config):
        # Arrange
        config = full_loaded_config
        # Act
        value = config.health.enabled
        # Assert
        assert value is True

    def test_full_loads_health_interval(self, full_loaded_config):
        # Arrange
        config = full_loaded_config
        # Act
        value = config.health.interval
        # Assert
        assert value == 45

    def test_full_loads_restart_policy(self, full_loaded_config):
        # Arrange
        config = full_loaded_config
        # Act
        value = config.restart.policy
        # Assert
        assert value == "on-failure"

    def test_full_loads_restart_max_retries(self, full_loaded_config):
        # Arrange
        config = full_loaded_config
        # Act
        value = config.restart.max_retries
        # Assert
        assert value == 5

    def test_full_loads_restart_backoff_initial(self, full_loaded_config):
        # Arrange
        config = full_loaded_config
        # Act
        value = config.restart.backoff_initial
        # Assert
        assert value == 15

    def test_full_loads_restart_backoff_multiplier(self, full_loaded_config):
        # Arrange
        config = full_loaded_config
        # Act
        value = config.restart.backoff_multiplier
        # Assert
        assert value == 3

    def test_full_loads_screen_name(self, full_loaded_config):
        # Arrange
        config = full_loaded_config
        # Act
        value = config.screen_name
        # Assert
        assert value == "full-agent"

    def test_full_preserves_user_env_var(self, full_loaded_config):
        """v3 auto-derives sac env vars on top of user env."""
        # Arrange
        config = full_loaded_config
        # Act
        value = config.env.get("MY_VAR")
        # Assert
        assert value == "my_value"

    def test_full_auto_derives_claude_agent_id(self, full_loaded_config):
        # Arrange
        config = full_loaded_config
        # Act
        value = config.env.get("CLAUDE_AGENT_ID")
        # Assert
        assert value == "full-agent"

    def test_full_preserves_user_pre_start_hook(self, full_loaded_config):
        # Arrange
        config = full_loaded_config
        # Act
        hooks = config.hooks["pre_start"]
        # Assert
        assert "echo pre" in hooks

    def test_full_auto_prepends_mkdir_hook(self, full_loaded_config):
        # Arrange
        config = full_loaded_config
        # Act
        has_mkdir = any("mkdir -p" in h for h in config.hooks["pre_start"])
        # Assert
        assert has_mkdir is True


class TestLoadConfigExpansion:
    def test_expanded_workdir_resolves_tilde(self):
        # Arrange
        path = _write_config(MINIMAL_CONFIG)
        try:
            config = load_config(path)
            # Act
            expanded = config.expanded_workdir
            # Assert
            assert "~" not in expanded
        finally:
            Path(path).unlink()


class TestLoadConfigRejects:
    def test_invalid_api_version_raises_value_error(self):
        # Arrange
        data = {**MINIMAL_CONFIG, "apiVersion": "wrong/v2"}
        path = _write_config(data)
        try:
            # Act
            # Assert
            with pytest.raises(ValueError, match="apiVersion"):
                load_config(path)
        finally:
            Path(path).unlink()

    def test_metadata_name_in_yaml_rejected(self):
        """Dir-as-SSoT: metadata.name in YAML is no longer accepted."""
        # Arrange — bypass _write_config (which strips metadata.name); write raw.
        tmp_dir = Path(tempfile.mkdtemp()) / "rejected-agent"
        tmp_dir.mkdir(parents=True)
        path = tmp_dir / "rejected-agent.yaml"
        path.write_text(
            yaml.safe_dump(
                {
                    "apiVersion": "scitex-agent-container/v3",
                    "kind": "Agent",
                    "metadata": {"name": "rejected-agent"},
                    "spec": {"runtime": "apptainer"},
                }
            )
        )
        # Act
        # Assert
        with pytest.raises(ValueError, match="metadata.name is no longer accepted"):
            load_config(str(path))

    def test_invalid_runtime_raises_value_error(self):
        # Arrange
        data = {
            "apiVersion": "scitex-agent-container/v3",
            "kind": "Agent",
            "metadata": {"name": "test"},
            "spec": {"runtime": "invalid-runtime"},
        }
        path = _write_config(data)
        try:
            # Act
            # Assert
            with pytest.raises(ValueError, match="runtime"):
                load_config(path)
        finally:
            Path(path).unlink()


class TestValidateConfig:
    def test_minimal_config_validates_with_no_errors(self):
        # Arrange
        path = _write_config(MINIMAL_CONFIG)
        try:
            # Act
            errors = validate_config(path)
            # Assert
            assert errors == []
        finally:
            Path(path).unlink()

    def test_invalid_yaml_reports_errors(self):
        # Arrange
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False)
        tmp.write(":::invalid yaml:::")
        tmp.close()
        try:
            # Act
            errors = validate_config(tmp.name)
            # Assert
            assert len(errors) > 0
        finally:
            Path(tmp.name).unlink()

    def test_missing_file_reports_not_found(self):
        # Arrange
        # (path intentionally does not exist)
        # Act
        errors = validate_config("/nonexistent/path.yaml")
        # Assert
        assert any("not found" in e.lower() or "File not found" in e for e in errors)

    def test_invalid_container_runtime_flagged(self):
        # Arrange
        data = {
            "apiVersion": "scitex-agent-container/v3",
            "kind": "Agent",
            "metadata": {"name": "test"},
            "spec": {
                "runtime": "apptainer",
                "container": {"runtime": "kubernetes"},
            },
        }
        path = _write_config(data)
        try:
            # Act
            errors = validate_config(path)
            # Assert
            assert any("container.runtime" in e for e in errors)
        finally:
            Path(path).unlink()

    def test_invalid_restart_policy_flagged(self):
        # Arrange
        data = {
            "apiVersion": "scitex-agent-container/v3",
            "kind": "Agent",
            "metadata": {"name": "test"},
            "spec": {
                "runtime": "apptainer",
                "restart": {"policy": "maybe"},
            },
        }
        path = _write_config(data)
        try:
            # Act
            errors = validate_config(path)
            # Assert
            assert any("restart.policy" in e for e in errors)
        finally:
            Path(path).unlink()


class TestSkillsSpec:
    def test_default_skills_required_carries_base_defaults(self):
        # Arrange — even a minimal v3 spec (no `spec.skills` block, which
        # v3 rejects anyway) must inherit the fleet-wide base defaults
        # (e.g. `scitex-todo`) so every agent loads them at startup.
        from scitex_agent_container.config._parsers._skills_defaults import (
            BASE_REQUIRED_SKILLS,
        )

        path = _write_config(MINIMAL_CONFIG)
        try:
            config = load_config(path)
            # Act
            value = config.skills.required
            # Assert
            assert value == list(BASE_REQUIRED_SKILLS)
        finally:
            Path(path).unlink()

    def test_default_skills_available_is_empty(self):
        # Arrange
        path = _write_config(MINIMAL_CONFIG)
        try:
            config = load_config(path)
            # Act
            value = config.skills.available
            # Assert
            assert value == []
        finally:
            Path(path).unlink()


@pytest.fixture
def claude_md_setup_tmpdir():
    """Provide a tmpdir + run setup_claude_md against it; yields (config, tmpdir)."""
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
        yield config, tmpdir


class TestSetupClaudeMd:
    def test_setup_creates_claude_md_file(self, claude_md_setup_tmpdir):
        # Arrange
        _config, tmpdir = claude_md_setup_tmpdir
        claude_md = Path(tmpdir) / ".claude" / "CLAUDE.md"
        # Act
        exists = claude_md.exists()
        # Assert
        assert exists is True

    def test_setup_writes_start_marker(self, claude_md_setup_tmpdir):
        # Arrange
        _config, tmpdir = claude_md_setup_tmpdir
        # Act
        content = (Path(tmpdir) / ".claude" / "CLAUDE.md").read_text()
        # Assert
        assert '<!-- agent-container:start id="test-agent" -->' in content

    def test_setup_writes_end_marker(self, claude_md_setup_tmpdir):
        # Arrange
        _config, tmpdir = claude_md_setup_tmpdir
        # Act
        content = (Path(tmpdir) / ".claude" / "CLAUDE.md").read_text()
        # Assert
        assert '<!-- agent-container:end id="test-agent" -->' in content

    def test_setup_includes_required_skill(self, claude_md_setup_tmpdir):
        # Arrange
        _config, tmpdir = claude_md_setup_tmpdir
        # Act
        content = (Path(tmpdir) / ".claude" / "CLAUDE.md").read_text()
        # Assert
        assert "quality-guards" in content

    def test_setup_includes_autonomous_skill(self, claude_md_setup_tmpdir):
        # Arrange
        _config, tmpdir = claude_md_setup_tmpdir
        # Act
        content = (Path(tmpdir) / ".claude" / "CLAUDE.md").read_text()
        # Assert
        assert "autonomous" in content

    def test_setup_includes_role_label(self, claude_md_setup_tmpdir):
        # Arrange
        _config, tmpdir = claude_md_setup_tmpdir
        # Act
        content = (Path(tmpdir) / ".claude" / "CLAUDE.md").read_text()
        # Assert
        assert "Role: worker" in content


@pytest.fixture
def claude_md_preserve_setup():
    """Pre-populate CLAUDE.md with user content, then deploy managed section."""
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
        yield claude_md


class TestSetupPreservesExisting:
    def test_setup_preserves_user_heading(self, claude_md_preserve_setup):
        # Arrange
        claude_md = claude_md_preserve_setup
        # Act
        content = claude_md.read_text()
        # Assert
        assert "# My Project" in content

    def test_setup_preserves_user_body_text(self, claude_md_preserve_setup):
        # Arrange
        claude_md = claude_md_preserve_setup
        # Act
        content = claude_md.read_text()
        # Assert
        assert "Some existing content." in content

    def test_setup_appends_managed_marker(self, claude_md_preserve_setup):
        # Arrange
        claude_md = claude_md_preserve_setup
        # Act
        content = claude_md.read_text()
        # Assert
        assert '<!-- agent-container:start id="test-agent" -->' in content


@pytest.fixture
def claude_md_replace_setup():
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
        yield claude_md


class TestSetupReplacesExisting:
    def test_setup_removes_old_managed_content(self, claude_md_replace_setup):
        # Arrange
        claude_md = claude_md_replace_setup
        # Act
        content = claude_md.read_text()
        # Assert
        assert "old content" not in content

    def test_setup_writes_new_skill(self, claude_md_replace_setup):
        # Arrange
        claude_md = claude_md_replace_setup
        # Act
        content = claude_md.read_text()
        # Assert
        assert "new-skill" in content

    def test_setup_preserves_user_header(self, claude_md_replace_setup):
        # Arrange
        claude_md = claude_md_replace_setup
        # Act
        content = claude_md.read_text()
        # Assert
        assert "# Header" in content

    def test_setup_preserves_user_footer(self, claude_md_replace_setup):
        # Arrange
        claude_md = claude_md_replace_setup
        # Act
        content = claude_md.read_text()
        # Assert
        assert "# Footer" in content


@pytest.fixture
def claude_md_cleanup_setup():
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
        yield claude_md


class TestCleanupClaudeMd:
    def test_cleanup_removes_agent_section_body(self, claude_md_cleanup_setup):
        # Arrange
        claude_md = claude_md_cleanup_setup
        # Act
        content = claude_md.read_text()
        # Assert
        assert "agent section" not in content

    def test_cleanup_removes_start_marker(self, claude_md_cleanup_setup):
        # Arrange
        claude_md = claude_md_cleanup_setup
        # Act
        content = claude_md.read_text()
        # Assert
        assert "agent-container:start" not in content

    def test_cleanup_preserves_user_header(self, claude_md_cleanup_setup):
        # Arrange
        claude_md = claude_md_cleanup_setup
        # Act
        content = claude_md.read_text()
        # Assert
        assert "# Header" in content

    def test_cleanup_preserves_user_footer(self, claude_md_cleanup_setup):
        # Arrange
        claude_md = claude_md_cleanup_setup
        # Act
        content = claude_md.read_text()
        # Assert
        assert "# Footer" in content

    def test_cleanup_noop_when_no_file_does_not_raise(self):
        from scitex_agent_container.runtimes.claude_md import (
            cleanup_claude_md as _cleanup_claude_md,
        )

        # Arrange
        with tempfile.TemporaryDirectory() as tmpdir:
            config = AgentConfig(name="test-agent")
            claude_md = Path(tmpdir) / ".claude" / "CLAUDE.md"
            # Act
            _cleanup_claude_md(config, tmpdir)
            # Assert
            assert claude_md.exists() is False


# WI-6 (handoff §6, 2026-05-20) deleted ``RemoteSpec`` and the
# ``cfg.remote`` attribute on ``AgentConfig``. The legacy
# ``TestDefaultRemoteSpec`` / ``TestRemoteSpecDataclass`` /
# ``TestAgentConfigRemote`` / ``TestLoginShellDefault`` /
# ``test_remote_from_yaml_loads_host`` blocks were removed wholesale —
# they exercised behaviour that no longer exists. Cross-host placement
# is via ``spec.host`` (see ``HostsSpec`` tests below). The legacy
# ``test_remote_full_spec_loads_port`` / ``test_login_shell_from_yaml_can_be_false``
# skipped stubs were removed too (2026-07-13) — perpetually-skipped
# placeholders for removed spec.remote fields, flagged by STX-TQ001.
