from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import pytest
import yaml

from scitex_agent_container._listen import _config as listen_config
from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._provider_types import ProviderSpec
from scitex_agent_container.runtimes import _apptainer_build, _fleet_env
from scitex_agent_container.runtimes import _hermes_profile as profile


@contextmanager
def _replace_attributes(replacements):
    originals = [
        (target, name, getattr(target, name)) for target, name, _ in replacements
    ]
    try:
        for target, name, value in replacements:
            setattr(target, name, value)
        yield
    finally:
        for target, name, value in originals:
            setattr(target, name, value)


def test_launch_plan_normalizes_openai_api_root():
    # Arrange
    config = AgentConfig(name="scholar", harness="hermes", runtime="headless")
    config.engine_key = "qwen"
    config.model = "qwen-model"
    config.max_context_tokens = 1_048_576
    config.reasoning_effort = "low"
    config.claude.provider = ProviderSpec(
        base_url="http://engine.example:8000",
        auth_token_env="ENGINE_KEY",
    )

    # Act
    plan = profile._launch_plan(config)
    # Assert
    assert (
        plan.endpoint.url,
        plan.engine.context_window_tokens,
        plan.engine.reasoning_effort,
    ) == ("http://engine.example:8000/v1/chat/completions", 1_048_576, "low")


def test_launch_plan_carries_live_spawn_and_parallelism_policy():
    # Arrange
    config = AgentConfig(name="cards", harness="hermes", runtime="headless")
    config.engine_key = "deepseek"
    config.model = "deepseek-chat"
    config.claude.provider = ProviderSpec(
        base_url="https://api.deepseek.com/v1",
        auth_token_env="DEEPSEEK_API_KEY",
    )
    config.lineage.may_spawn = False
    config.delegation.max_concurrent_children = 1
    config.delegation.worktree_isolation = False

    # Act
    plan = profile._launch_plan(config)
    # Assert
    assert (
        plan.may_spawn,
        plan.delegation.max_concurrent_children,
        plan.delegation.worktree_isolation,
    ) == (False, 1, False)


def test_mcp_translation_preserves_commands_and_drops_claude_metadata(tmp_path):
    # Arrange
    source = {
        "mcpServers": {
            "cards": {
                "type": "stdio",
                "command": "scitex-cards",
                "args": ["mcp", "start"],
                "alwaysLoad": True,
                "env": {"SCITEX_CARDS_AGENT_ID": "${SCITEX_CARDS_AGENT_ID}"},
            },
            "optional-browser": {
                "type": "stdio",
                "command": "browser-mcp",
                "alwaysLoad": False,
            },
        }
    }
    (tmp_path / ".mcp.json").write_text(json.dumps(source), encoding="utf-8")

    # Act
    translated, eager_toolsets = profile._mcp_servers(tmp_path)
    # Assert
    assert (translated, eager_toolsets) == (
        {
            "cards": {
                "command": "scitex-cards",
                "args": ["mcp", "start"],
                "env": {"SCITEX_CARDS_AGENT_ID": "${SCITEX_CARDS_AGENT_ID}"},
            }
        },
        ["mcp-cards"],
    )


def test_api_key_is_stable_and_owner_only(tmp_path: Path):
    # Arrange
    key_path = tmp_path / profile.API_KEY_FILE
    # Act
    first = profile.ensure_api_key(tmp_path)
    second = profile.ensure_api_key(tmp_path)

    # Assert
    assert (
        first == second
        and len(first) >= 16
        and key_path.stat().st_mode & 0o777 == 0o600
    )


def test_mcp_runtime_env_uses_agent_identity():
    # Arrange
    config = AgentConfig(name="scholar", harness="hermes", runtime="headless")
    servers = {
        "cards": {
            "env": {
                "PGUSER": "ywatanabe__cli",
                "PGPASSFILE": "${PGPASSFILE}",
                "SCITEX_CARDS_SCOPE": "${SCITEX_CARDS_SCOPE}",
                "SECRET_TOKEN": "${SECRET_TOKEN}",
            }
        }
    }

    def replacement(value):
        return {
            "PGUSER": "ywatanabe__scholar",
            "PGPASSFILE": "/home/ywatanabe/.pgpass",
            "SCITEX_CARDS_SCOPE": "agent:scholar",
            "SECRET_TOKEN": "must-not-be-materialized",
        }

    # Act
    with _replace_attributes([(_fleet_env, "effective_env", replacement)]):
        profile._bind_mcp_runtime_env(config, servers)
    # Assert
    assert servers["cards"]["env"] == {
        "PGUSER": "ywatanabe__scholar",
        "PGPASSFILE": "/home/ywatanabe/.pgpass",
        "SCITEX_CARDS_SCOPE": "agent:scholar",
        "SECRET_TOKEN": "${SECRET_TOKEN}",
    }


def test_mcp_runtime_env_adds_cards_scope_when_source_does_not_declare_it():
    # Arrange
    config = AgentConfig(name="scholar", harness="hermes", runtime="headless")
    servers = {"scitex-cards": {"command": "scitex-cards", "args": ["mcp", "start"]}}

    def replacement(value):
        return {
            "SCITEX_CARDS_AGENT_ID": "scholar",
            "SCITEX_CARDS_SCOPE": "agent:scholar",
        }

    # Act
    with _replace_attributes([(_fleet_env, "effective_env", replacement)]):
        profile._bind_mcp_runtime_env(config, servers)
    # Assert
    assert servers["scitex-cards"]["env"] == {
        "SCITEX_CARDS_AGENT_ID": "scholar",
        "SCITEX_CARDS_SCOPE": "agent:scholar",
    }


def test_cards_mcp_receives_postgres_identity_without_template_placeholders():
    # Arrange
    config = AgentConfig(name="scholar", harness="hermes", runtime="tui")
    servers = {"scitex-cards": {"command": "scitex-cards", "args": ["mcp", "start"]}}

    def replacement(value):
        return {
            "PGUSER": "ywatanabe__scholar",
            "PGPASSFILE": "/home/ywatanabe/.pgpass",
            "SCITEX_CARDS_AGENT_ID": "scholar",
            "SCITEX_CARDS_DB": "postgresql://scitex-primary:55432/scitex",
        }

    # Act
    with _replace_attributes([(_fleet_env, "effective_env", replacement)]):
        profile._bind_mcp_runtime_env(config, servers)
    # Assert
    assert servers["scitex-cards"]["env"] == {
        "PGUSER": "ywatanabe__scholar",
        "PGPASSFILE": "/home/ywatanabe/.pgpass",
        "SCITEX_CARDS_AGENT_ID": "scholar",
        "SCITEX_CARDS_DB": "postgresql://scitex-primary:55432/scitex",
    }


def test_sac_mcp_receives_bus_auth_refs_without_persisting_bearer(env_save_restore):
    # Arrange
    config = AgentConfig(name="scholar", harness="hermes", runtime="tui")
    env_save_restore.set("SAC_LISTEN_BEARER", "actual-secret")
    servers = {
        "scitex-agent-container": {
            "command": "sac",
            "args": ["mcp", "start"],
        }
    }

    def replacement(value):
        return {
            "PGUSER": "ywatanabe__scholar",
            "PGPASSFILE": "/home/ywatanabe/.pgpass",
            "SCITEX_STORE_DSN": "postgresql://scitex-primary:55432/scitex",
        }

    # Act
    with _replace_attributes([(_fleet_env, "effective_env", replacement)]):
        profile._bind_mcp_runtime_env(config, servers)
    # Assert
    assert servers["scitex-agent-container"]["env"] == {
        "SAC_LISTEN_BASE_URL": "${env:SAC_LISTEN_BASE_URL}",
        "SAC_LISTEN_BEARER": "${env:SAC_LISTEN_BEARER}",
        "SAC_NAME": "${env:SAC_NAME}",
        "PGUSER": "ywatanabe__scholar",
        "PGPASSFILE": "/home/ywatanabe/.pgpass",
        "SCITEX_STORE_DSN": "postgresql://scitex-primary:55432/scitex",
    } and "actual-secret" not in json.dumps(servers)


def test_sac_profile_env_keeps_bus_bearer_in_secret_file_values():
    # Arrange
    config = AgentConfig(name="scholar", harness="hermes", runtime="tui")
    servers = {"scitex-agent-container": {"command": "sac"}}
    replacements = [
        (_apptainer_build, "_read_listen_bearer", lambda: "host-listen-secret"),
        (listen_config, "listen_base_url", lambda: "http://127.0.0.1:7878"),
    ]
    # Act
    with _replace_attributes(replacements):
        result = profile._sac_profile_env(config, servers)
    # Assert
    assert result == {
        "SAC_LISTEN_BASE_URL": "http://127.0.0.1:7878",
        "SAC_LISTEN_BEARER": "host-listen-secret",
        "SAC_NAME": "scholar",
    }


def test_profile_env_file_is_owner_only(tmp_path):
    # Arrange
    env_path = tmp_path / ".env"
    # Act
    profile._write_profile_env(env_path, {"SAFE": "value"})
    safe_result = (env_path.read_text(), env_path.stat().st_mode & 0o777)
    # Assert
    assert safe_result == ("SAFE=value\n", 0o600)


def test_profile_env_file_rejects_newlines(tmp_path):
    # Arrange
    env_path = tmp_path / ".env"
    # Act
    ctx = pytest.raises(ValueError, match="contains a newline")
    # Assert
    with ctx:
        profile._write_profile_env(env_path, {"UNSAFE": "first\nsecond"})


def test_tui_profile_contains_qwen_config_without_api_gateway(tmp_path):
    # Arrange
    config = AgentConfig(
        name="scholar", harness="hermes", runtime="tui", workdir="/work"
    )
    config.engine_key = "qwen"
    config.model = "qwen-model"
    config.reasoning_effort = "low"
    config.autonomous.enabled = True
    config.claude.provider = ProviderSpec(
        base_url="http://qwen.example:8000/v1",
        auth_token_env="QWEN_KEY",
    )
    replacements = [
        (profile, "resolve_provider_api_key", lambda value: "secret"),
        (profile, "deploy_to_home", lambda value, target: None),
        (profile, "deploy_to_home_overlay", lambda value: None),
        (profile, "resolve_overlay_upper_home", lambda value: None),
    ]
    expected_headers = {"X-SciTeX-Session-ID": "sac:scholar"}
    # Act
    with _replace_attributes(replacements):
        targets = profile.materialize_hermes_tui_profile(config, state_dir=tmp_path)
    rendered = (targets[0] / ".hermes" / "config.yaml").read_text(encoding="utf-8")
    parsed = yaml.safe_load(rendered)
    env_text = (targets[0] / ".hermes" / ".env").read_text()
    # Assert
    assert (
        "qwen-model" in rendered
        and "reasoning_effort: low" in rendered
        and "mode: 'off'" in rendered
        and "api_server:" not in rendered
        and parsed["providers"]["sac-qwen"]["extra_headers"]
        == expected_headers
        and env_text == "QWEN_KEY=secret\n"
    )


def test_tui_profile_disables_harness_approvals_even_without_autonomous_drive(
    tmp_path,
):
    # Arrange
    config = AgentConfig(
        name="scholar", harness="hermes", runtime="tui", workdir="/work"
    )
    config.engine_key = "qwen"
    config.model = "qwen-model"
    config.autonomous.enabled = False
    config.claude.provider = ProviderSpec(
        base_url="http://qwen.example:8000/v1",
        auth_token_env="QWEN_KEY",
    )
    replacements = [
        (profile, "resolve_provider_api_key", lambda value: "secret"),
        (profile, "deploy_to_home", lambda value, target: None),
        (profile, "deploy_to_home_overlay", lambda value: None),
        (profile, "resolve_overlay_upper_home", lambda value: None),
    ]
    # Act
    with _replace_attributes(replacements):
        targets = profile.materialize_hermes_tui_profile(config, state_dir=tmp_path)
    rendered = (targets[0] / ".hermes" / "config.yaml").read_text(encoding="utf-8")
    # Assert
    assert "mode: 'off'" in rendered
