"""Tests for ``runtimes/_opencode_profile.py``.

Home deploy + MCP setup run for real into ``tmp_path`` (deploy is a
no-op copy when nothing resolves; ``setup_mcp_config`` no-ops on an
empty ``mcp_servers``); the provider key resolves from the
environment like production. No live serve, no patches.
"""

from __future__ import annotations

import json
import os

import pytest

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._provider_types import ProviderSpec
from scitex_agent_container.runtimes import _opencode_profile as profile


def _config(**overrides):
    config = AgentConfig(name="worker", harness="opencode", runtime="tui")
    config.engine_key = "free"
    config.model = "muse-spark-1.3-contributor-free"
    config.claude.provider = ProviderSpec(
        base_url="http://127.0.0.1:18779/v1",
        auth_token_env="OPENCODE_PROFILE_TEST_KEY",
    )
    for key, value in overrides.items():
        setattr(config, key, value)
    return config


def _materialize(tmp_path, env_save_restore, **overrides):
    env_save_restore.set("OPENCODE_PROFILE_TEST_KEY", "secret-must-not-be-serialized")
    return profile.materialize_opencode_profile(
        _config(**overrides), state_dir=tmp_path
    )


def test_launch_plan_flips_the_harness_axis_to_opencode():
    # Arrange
    config = _config()
    # Act
    plan = profile._launch_plan(config)
    # Assert
    assert plan.harness == "opencode"


def test_materialize_writes_the_derived_model(tmp_path, env_save_restore):
    # Arrange
    targets = _materialize(tmp_path, env_save_restore)
    # Act
    document = json.loads(
        (targets[0] / profile.PROFILE_DIRNAME / profile.PROFILE_FILENAME).read_text()
    )
    # Assert
    assert document["model"] == "sac-free/muse-spark-1.3-contributor-free"


def test_materialize_never_serializes_the_secret(tmp_path, env_save_restore):
    # Arrange
    targets = _materialize(tmp_path, env_save_restore)
    # Act
    body = (targets[0] / profile.PROFILE_DIRNAME / profile.PROFILE_FILENAME).read_text()
    # Assert
    assert "secret-must-not-be-serialized" not in body


def test_materialize_env_file_is_owner_only(tmp_path, env_save_restore):
    # Arrange
    targets = _materialize(tmp_path, env_save_restore)
    # Act
    mode = (
        (targets[0] / profile.PROFILE_DIRNAME / profile.PROFILE_ENV_FILENAME)
        .stat()
        .st_mode
        & 0o777
    )
    # Assert
    assert mode == 0o600


def test_materialize_env_file_carries_the_key_name(tmp_path, env_save_restore):
    # Arrange
    targets = _materialize(tmp_path, env_save_restore)
    env_path = targets[0] / profile.PROFILE_DIRNAME / profile.PROFILE_ENV_FILENAME
    # Act
    body = env_path.read_text()
    # Assert
    assert "OPENCODE_PROFILE_TEST_KEY=" in body


def test_profile_env_argv_points_at_the_materialized_env(tmp_path, env_save_restore):
    # Arrange
    _materialize(tmp_path, env_save_restore)
    # Act
    argv = profile.profile_env_argv(tmp_path)
    # Assert
    assert argv[1].endswith(profile.PROFILE_ENV_FILENAME)


def test_profile_env_argv_refuses_a_missing_env(tmp_path):
    # Arrange
    missing = tmp_path / "empty-state"
    missing.mkdir()

    def action():
        return profile.profile_env_argv(missing)

    # Act
    run = action
    # Assert
    with pytest.raises(RuntimeError, match="profile env is absent"):
        run()


def test_validate_passes_on_the_materialized_profile(tmp_path, env_save_restore):
    # Arrange
    _materialize(tmp_path, env_save_restore)
    config = _config()
    # Act
    outcome = profile.validate_opencode_profile(
        config, state_dir=tmp_path, launch_argv=[]
    )
    # Assert
    assert outcome is None


def test_validate_refuses_a_missing_profile(tmp_path):
    # Arrange
    config = _config()

    def action():
        return profile.validate_opencode_profile(
            config, state_dir=tmp_path, launch_argv=[]
        )

    # Act
    run = action
    # Assert
    with pytest.raises(RuntimeError, match="not materialized"):
        run()


def test_validate_refuses_a_model_less_profile(tmp_path):
    # Arrange
    config = _config()
    profile_dir = tmp_path / "home" / profile.PROFILE_DIRNAME
    profile_dir.mkdir(parents=True)
    (profile_dir / profile.PROFILE_FILENAME).write_text("{}")
    (profile_dir / profile.PROFILE_ENV_FILENAME).write_text("")
    os.chmod(profile_dir / profile.PROFILE_ENV_FILENAME, 0o600)

    def action():
        return profile.validate_opencode_profile(
            config, state_dir=tmp_path, launch_argv=[]
        )

    # Act
    run = action
    # Assert
    with pytest.raises(RuntimeError, match="names no model"):
        run()


def test_effective_port_prefers_the_explicit_spec():
    # Arrange
    from scitex_agent_container.runtimes._gateway_opencode import effective_serve_port

    config = _config(opencode_serve_port=4199)
    # Act
    port = effective_serve_port(config)
    # Assert
    assert port == 4199


def test_effective_port_picks_a_live_loopback_port_for_auto():
    # Arrange
    from scitex_agent_container.runtimes._gateway_opencode import effective_serve_port

    config = _config()
    # Act
    port = effective_serve_port(config)
    # Assert
    assert 0 < port < 65536


def test_materialize_writes_the_startup_file_with_engine_binding(
    tmp_path, env_save_restore
):
    # Arrange
    _materialize(tmp_path, env_save_restore, startup_prompts=["do the thing"])
    # Act
    startup = json.loads((tmp_path / "opencode-startup.json").read_text())
    # Assert
    assert startup["texts"] == ["do the thing"]


def test_materialize_startup_file_names_the_resolved_engine(
    tmp_path, env_save_restore
):
    # Arrange
    _materialize(tmp_path, env_save_restore, startup_prompts=["do the thing"])
    # Act
    startup = json.loads((tmp_path / "opencode-startup.json").read_text())
    # Assert
    assert (startup["provider_id"], startup["model_id"]) == (
        "sac-free",
        "muse-spark-1.3-contributor-free",
    )


def test_materialize_startup_file_is_owner_only(tmp_path, env_save_restore):
    # Arrange
    _materialize(tmp_path, env_save_restore, startup_prompts=["do the thing"])
    # Act
    mode = (tmp_path / "opencode-startup.json").stat().st_mode & 0o777
    # Assert
    assert mode == 0o600


def test_materialize_without_prompts_writes_no_startup_file(
    tmp_path, env_save_restore
):
    # Arrange
    _materialize(tmp_path, env_save_restore)
    # Act
    missing = tmp_path / "opencode-startup.json"
    # Assert
    assert not missing.exists()


def test_materialize_translates_stdio_mcp_servers(tmp_path, env_save_restore):
    # Arrange
    home = tmp_path / "home"
    home.mkdir(parents=True)
    (home / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "demo": {
                        "command": "demo-bin",
                        "args": ["--serve"],
                        "env": {"DEMO_KEY": "v"},
                    }
                }
            }
        )
    )
    _materialize(tmp_path, env_save_restore)
    # Act
    document = json.loads(
        (home / ".config" / "opencode" / "opencode.json").read_text()
    )
    # Assert
    assert document["mcp"]["demo"] == {
        "type": "local",
        "command": ["demo-bin", "--serve"],
        "environment": {"DEMO_KEY": "v"},
        "enabled": True,
    }


def test_materialize_translates_remote_mcp_servers(tmp_path, env_save_restore):
    # Arrange
    home = tmp_path / "home"
    home.mkdir(parents=True)
    (home / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"far": {"url": "https://mcp.example/mcp"}}})
    )
    _materialize(tmp_path, env_save_restore)
    # Act
    document = json.loads(
        (home / ".config" / "opencode" / "opencode.json").read_text()
    )
    # Assert
    assert document["mcp"]["far"] == {
        "type": "remote",
        "url": "https://mcp.example/mcp",
        "enabled": True,
    }


def test_materialize_without_mcp_servers_omits_the_mcp_section(
    tmp_path, env_save_restore
):
    # Arrange
    _materialize(tmp_path, env_save_restore)
    # Act
    document = json.loads(
        (tmp_path / "home" / ".config" / "opencode" / "opencode.json").read_text()
    )
    # Assert
    assert "mcp" not in document
