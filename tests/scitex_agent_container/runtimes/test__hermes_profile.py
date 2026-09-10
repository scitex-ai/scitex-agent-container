from __future__ import annotations

import json
from pathlib import Path

import pytest

from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._provider_types import ProviderSpec
from scitex_agent_container.runtimes import _hermes_profile as profile


def test_launch_plan_normalizes_openai_api_root():
    config = AgentConfig(name="scholar", harness="hermes", runtime="headless")
    config.engine_key = "qwen"
    config.model = "qwen-model"
    config.max_context_tokens = 1_048_576
    config.reasoning_effort = "low"
    config.claude.provider = ProviderSpec(
        base_url="http://engine.example:8000",
        auth_token_env="ENGINE_KEY",
    )

    plan = profile._launch_plan(config)

    assert plan.endpoint.url == "http://engine.example:8000/v1/chat/completions"
    assert plan.engine.context_window_tokens == 1_048_576
    assert plan.engine.reasoning_effort == "low"


def test_launch_plan_carries_live_spawn_and_parallelism_policy():
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

    plan = profile._launch_plan(config)

    assert plan.may_spawn is False
    assert plan.delegation.max_concurrent_children == 1
    assert plan.delegation.worktree_isolation is False


def test_mcp_translation_preserves_commands_and_drops_claude_metadata(tmp_path):
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

    translated, eager_toolsets = profile._mcp_servers(tmp_path)

    assert translated == {
        "cards": {
            "command": "scitex-cards",
            "args": ["mcp", "start"],
            "env": {"SCITEX_CARDS_AGENT_ID": "${SCITEX_CARDS_AGENT_ID}"},
        }
    }
    assert eager_toolsets == ["mcp-cards"]


def test_api_key_is_stable_and_owner_only(tmp_path: Path):
    first = profile.ensure_api_key(tmp_path)
    second = profile.ensure_api_key(tmp_path)

    assert first == second
    assert len(first) >= 16
    assert (tmp_path / profile.API_KEY_FILE).stat().st_mode & 0o777 == 0o600


def test_mcp_runtime_env_uses_agent_identity(monkeypatch):
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
    monkeypatch.setattr(
        "scitex_agent_container.runtimes._fleet_env.effective_env",
        lambda value: {
            "PGUSER": "ywatanabe__scholar",
            "PGPASSFILE": "/home/ywatanabe/.pgpass",
            "SCITEX_CARDS_SCOPE": "agent:scholar",
            "SECRET_TOKEN": "must-not-be-materialized",
        },
    )

    profile._bind_mcp_runtime_env(config, servers)

    assert servers["cards"]["env"] == {
        "PGUSER": "ywatanabe__scholar",
        "PGPASSFILE": "/home/ywatanabe/.pgpass",
        "SCITEX_CARDS_SCOPE": "agent:scholar",
        "SECRET_TOKEN": "${SECRET_TOKEN}",
    }


def test_mcp_runtime_env_adds_cards_scope_when_source_does_not_declare_it(
    monkeypatch,
):
    config = AgentConfig(name="scholar", harness="hermes", runtime="headless")
    servers = {"scitex-cards": {"command": "scitex-cards", "args": ["mcp", "start"]}}
    monkeypatch.setattr(
        "scitex_agent_container.runtimes._fleet_env.effective_env",
        lambda value: {
            "SCITEX_CARDS_AGENT_ID": "scholar",
            "SCITEX_CARDS_SCOPE": "agent:scholar",
        },
    )

    profile._bind_mcp_runtime_env(config, servers)

    assert servers["scitex-cards"]["env"] == {
        "SCITEX_CARDS_AGENT_ID": "scholar",
        "SCITEX_CARDS_SCOPE": "agent:scholar",
    }


def test_cards_mcp_receives_postgres_identity_without_template_placeholders(
    monkeypatch,
):
    config = AgentConfig(name="scholar", harness="hermes", runtime="tui")
    servers = {"scitex-cards": {"command": "scitex-cards", "args": ["mcp", "start"]}}
    monkeypatch.setattr(
        "scitex_agent_container.runtimes._fleet_env.effective_env",
        lambda value: {
            "PGUSER": "ywatanabe__scholar",
            "PGPASSFILE": "/home/ywatanabe/.pgpass",
            "SCITEX_CARDS_AGENT_ID": "scholar",
            "SCITEX_CARDS_DB": "postgresql://scitex-primary:55432/scitex",
        },
    )

    profile._bind_mcp_runtime_env(config, servers)

    assert servers["scitex-cards"]["env"] == {
        "PGUSER": "ywatanabe__scholar",
        "PGPASSFILE": "/home/ywatanabe/.pgpass",
        "SCITEX_CARDS_AGENT_ID": "scholar",
        "SCITEX_CARDS_DB": "postgresql://scitex-primary:55432/scitex",
    }


def test_sac_mcp_receives_bus_auth_refs_without_persisting_bearer(monkeypatch):
    config = AgentConfig(name="scholar", harness="hermes", runtime="tui")
    monkeypatch.setenv("SAC_LISTEN_BEARER", "actual-secret")
    servers = {
        "scitex-agent-container": {
            "command": "sac",
            "args": ["mcp", "start"],
        }
    }
    monkeypatch.setattr(
        "scitex_agent_container.runtimes._fleet_env.effective_env",
        lambda value: {
            "PGUSER": "ywatanabe__scholar",
            "PGPASSFILE": "/home/ywatanabe/.pgpass",
            "SCITEX_STORE_DSN": "postgresql://scitex-primary:55432/scitex",
        },
    )

    profile._bind_mcp_runtime_env(config, servers)

    assert servers["scitex-agent-container"]["env"] == {
        "SAC_LISTEN_BASE_URL": "${env:SAC_LISTEN_BASE_URL}",
        "SAC_LISTEN_BEARER": "${env:SAC_LISTEN_BEARER}",
        "SAC_NAME": "${env:SAC_NAME}",
        "PGUSER": "ywatanabe__scholar",
        "PGPASSFILE": "/home/ywatanabe/.pgpass",
        "SCITEX_STORE_DSN": "postgresql://scitex-primary:55432/scitex",
    }
    assert "actual-secret" not in json.dumps(servers)


def test_sac_profile_env_keeps_bus_bearer_in_secret_file_values(monkeypatch):
    config = AgentConfig(name="scholar", harness="hermes", runtime="tui")
    servers = {"scitex-agent-container": {"command": "sac"}}
    monkeypatch.setattr(
        "scitex_agent_container.runtimes._apptainer_build._read_listen_bearer",
        lambda: "host-listen-secret",
    )
    monkeypatch.setattr(
        "scitex_agent_container._listen._config.listen_base_url",
        lambda: "http://127.0.0.1:7878",
    )

    assert profile._sac_profile_env(config, servers) == {
        "SAC_LISTEN_BASE_URL": "http://127.0.0.1:7878",
        "SAC_LISTEN_BEARER": "host-listen-secret",
        "SAC_NAME": "scholar",
    }


def test_profile_env_file_is_owner_only_and_rejects_newlines(tmp_path):
    env_path = tmp_path / ".env"
    profile._write_profile_env(env_path, {"SAFE": "value"})

    assert env_path.read_text() == "SAFE=value\n"
    assert env_path.stat().st_mode & 0o777 == 0o600

    with pytest.raises(ValueError, match="contains a newline"):
        profile._write_profile_env(env_path, {"UNSAFE": "first\nsecond"})


def test_tui_profile_contains_qwen_config_without_api_gateway(monkeypatch, tmp_path):
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
    monkeypatch.setattr(profile, "resolve_provider_api_key", lambda value: "secret")
    monkeypatch.setattr(profile, "deploy_to_home", lambda value, target: None)
    monkeypatch.setattr(profile, "deploy_to_home_overlay", lambda value: None)
    monkeypatch.setattr(profile, "resolve_overlay_upper_home", lambda value: None)

    targets = profile.materialize_hermes_tui_profile(config, state_dir=tmp_path)

    rendered = (targets[0] / ".hermes" / "config.yaml").read_text(encoding="utf-8")
    assert "qwen-model" in rendered
    assert "reasoning_effort: low" in rendered
    assert "mode: 'off'" in rendered
    assert "api_server:" not in rendered
    assert (targets[0] / ".hermes" / ".env").read_text() == "QWEN_KEY=secret\n"


def test_tui_profile_disables_harness_approvals_even_without_autonomous_drive(
    monkeypatch, tmp_path
):
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
    monkeypatch.setattr(profile, "resolve_provider_api_key", lambda value: "secret")
    monkeypatch.setattr(profile, "deploy_to_home", lambda value, target: None)
    monkeypatch.setattr(profile, "deploy_to_home_overlay", lambda value: None)
    monkeypatch.setattr(profile, "resolve_overlay_upper_home", lambda value: None)

    targets = profile.materialize_hermes_tui_profile(config, state_dir=tmp_path)

    rendered = (targets[0] / ".hermes" / "config.yaml").read_text(encoding="utf-8")
    assert "mode: 'off'" in rendered
