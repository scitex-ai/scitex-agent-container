from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path

import pytest
import yaml

from scitex_agent_container._listen import _config as listen_config
from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._hermes_compression import HermesCompressionSpec
from scitex_agent_container.config._provider_registry import resolve_provider
from scitex_agent_container.config._provider_types import ProviderSpec
from scitex_agent_container.runtimes import (
    _apptainer_build,
    _fleet_env,
    _pg_identity_env,
)
from scitex_agent_container.runtimes import _hermes_profile as profile
from scitex_agent_container.runtimes._hermes_cct import HermesCctRailError


@contextmanager
def _replace_attributes(replacements):
    # Load consumers that bind these functions at import time before a
    # temporary replacement can be captured as their permanent module global.
    from scitex_agent_container.runtimes import _pg_identity_credentials

    _effective_env_import_guard = _pg_identity_credentials.effective_env
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
    config.upstream_deadline_seconds = 1800
    config.client_abandonment_seconds = 1860
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
        plan.engine.upstream_deadline_seconds,
        plan.engine.client_abandonment_seconds,
    ) == (
        "http://engine.example:8000/v1/chat/completions",
        1_048_576,
        "low",
        1800,
        1860,
    )


def test_launch_plan_preserves_openai_responses_endpoint():
    # Arrange
    config = AgentConfig(name="hub", harness="hermes", runtime="tui")
    config.engine_key = "codex-subscription"
    config.model = "gpt-5.6-sol"
    config.claude.provider = ProviderSpec(
        base_url="http://127.0.0.1:18765/v1/responses",
        auth_token_env="GATEWAY_KEY",
    )

    # Act
    endpoint = profile._launch_plan(config, launch_mode="tui").endpoint

    # Assert
    assert (endpoint.protocol, endpoint.url) == (
        "openai-responses",
        "http://127.0.0.1:18765/v1/responses",
    )


def test_launch_plan_uses_responses_for_registered_codex_gateway():
    config = AgentConfig(name="hub", harness="hermes", runtime="tui")
    config.engine_key = "gpt-sol"
    config.model = "gpt-5.6-sol"
    registered = resolve_provider("codex")
    assert registered is not None
    config.claude.provider = ProviderSpec(
        base_url=str(registered["base_url"]),
        auth_token_env=str(registered["auth_token_env"]),
    )

    endpoint = profile._launch_plan(config, launch_mode="tui").endpoint

    assert (endpoint.protocol, endpoint.url) == (
        "openai-responses",
        "http://127.0.0.1:18765/v1/responses",
    )


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
                "args": ["mcp", "start", "--tools-only"],
                "env": {"SCITEX_CARDS_AGENT_ID": "${SCITEX_CARDS_AGENT_ID}"},
            }
        },
        ["mcp-cards"],
    )


def test_mcp_translation_does_not_duplicate_cards_tools_only_flag(tmp_path):
    # Arrange
    source = {
        "mcpServers": {
            "scitex-cards": {
                "command": "scitex-cards",
                "args": ["mcp", "start", "--tools-only"],
                "alwaysLoad": True,
            }
        }
    }
    (tmp_path / ".mcp.json").write_text(json.dumps(source), encoding="utf-8")

    # Act
    translated, _toolsets = profile._mcp_servers(tmp_path)

    # Assert
    assert translated["scitex-cards"]["args"] == [
        "mcp",
        "start",
        "--tools-only",
    ]


def test_selected_hermes_channel_loads_only_its_non_global_mcp(tmp_path):
    # Arrange
    source = {
        "mcpServers": {
            "claude-code-telegrammer": {
                "command": "bun",
                "args": ["run", "telegram-server.ts"],
            },
            "optional-browser": {"command": "browser-mcp"},
        }
    }
    (tmp_path / ".mcp.json").write_text(json.dumps(source), encoding="utf-8")

    # Act
    translated, eager_toolsets = profile._mcp_servers(
        tmp_path, channels=["server:claude-code-telegrammer"]
    )

    # Assert
    assert (translated, eager_toolsets) == (
        {
            "claude-code-telegrammer": {
                "command": "bun",
                "args": ["run", "telegram-server.ts"],
            }
        },
        ["mcp-claude-code-telegrammer"],
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
            "SCITEX_STORE_DSN": "postgresql://scitex-primary:55432/scitex",
            "SCITEX_CARDS_NOTIFY_DSN": "postgresql://scitex-primary:55433/scitex",
        }

    # Act
    with _replace_attributes([(_fleet_env, "effective_env", replacement)]):
        profile._bind_mcp_runtime_env(config, servers)
    # Assert
    assert servers["scitex-cards"]["env"] == {
        "PGUSER": "ywatanabe__scholar",
        "PGPASSFILE": "/home/ywatanabe/.pgpass",
        "SCITEX_CARDS_AGENT_ID": "scholar",
        "SCITEX_STORE_DSN": "postgresql://scitex-primary:55432/scitex",
        "SCITEX_CARDS_NOTIFY_DSN": "postgresql://scitex-primary:55433/scitex",
    }


def test_cct_mcp_receives_store_identity_without_template_placeholders():
    # Arrange
    config = AgentConfig(name="hub", harness="hermes", runtime="tui")
    servers = {
        "claude-code-telegrammer": {
            "command": "bun",
            "args": ["run", "telegram-server.ts"],
        }
    }

    def replacement(value):
        return {
            "PGUSER": "ywatanabe__hub",
            "PGPASSFILE": "/home/agent/.sac-pgpass",
            "SCITEX_STORE_DSN": "postgresql://scitex-primary:55432/scitex",
        }

    # Act
    with _replace_attributes([(_fleet_env, "effective_env", replacement)]):
        profile._bind_mcp_runtime_env(config, servers)
    # Assert
    assert servers["claude-code-telegrammer"]["env"] == {
        "PGUSER": "ywatanabe__hub",
        "PGPASSFILE": "/home/agent/.sac-pgpass",
        "SCITEX_STORE_DSN": "postgresql://scitex-primary:55432/scitex",
    }


def test_mcp_pg_binding_derives_and_validates_provisioned_project_role(tmp_path):
    # Arrange
    profile_home = tmp_path / "runtime-home"
    profile_home.mkdir()
    passfile = profile_home / ".sac-pgpass"
    passfile.write_text(
        "*:*:*:operator__scitex-agent-container:secret\n", encoding="utf-8"
    )
    passfile.chmod(0o600)
    servers = {"scitex-cards": {"command": "scitex-cards", "args": ["mcp", "start"]}}
    config = AgentConfig(
        name="scitex-agent-container-gui",
        harness="hermes",
        labels={"project": "scitex-agent-container"},
        env={"SCITEX_STORE_DSN": "postgresql://scitex-primary:55432/scitex"},
    )
    # Act
    with _replace_attributes(
        [
            (_pg_identity_env.getpass, "getuser", lambda: "operator"),
            (_fleet_env, "declared_fleet_defaults", lambda: {}),
        ]
    ):
        profile._bind_mcp_runtime_env(config, servers)
        profile._validate_mcp_pg_credentials(
            servers,
            launch_argv=("apptainer", "exec", "--bind", f"{profile_home}:/home/agent"),
        )
    # Assert
    assert servers["scitex-cards"]["env"]["PGUSER"] == (
        "operator__scitex-agent-container"
    )


def test_signup_variant_gets_project_role_passfile_and_cards_target():
    # Arrange
    # The real signup shape declares only the consolidated store;
    # Hermes must compile a complete scitex-cards child-process environment.
    config = AgentConfig(
        name="scitex-hub-signup",
        harness="hermes",
        labels={"project": "scitex-hub"},
    )
    servers = {"scitex-cards": {"command": "scitex-cards"}}
    replacements = [
        (
            _fleet_env,
            "declared_fleet_defaults",
            lambda: {"SCITEX_STORE_DSN": "postgresql://scitex-primary:55432/scitex"},
        ),
        (_pg_identity_env.getpass, "getuser", lambda: "operator"),
    ]
    # Act
    with _replace_attributes(replacements):
        profile._bind_mcp_runtime_env(config, servers)
    # Assert
    assert servers["scitex-cards"]["env"] == {
        "PGPASSFILE": "/home/agent/.sac-pgpass",
        "PGUSER": "operator__scitex-hub",
        "SCITEX_CARDS_AGENT_ID": "scitex-hub-signup",
        "SCITEX_STORE_DSN": "postgresql://scitex-primary:55432/scitex",
    }


def test_cards_mcp_removes_retired_store_alias():
    # Arrange
    config = AgentConfig(
        name="scholar",
        harness="hermes",
        env={"SCITEX_STORE_DSN": "postgresql://scitex-primary:55432/scitex"},
    )
    servers = {
        "scitex-cards": {
            "command": "scitex-cards",
            "env": {"SCITEX_CARDS_DB": "postgresql://wrong:55432/private"},
        }
    }

    # Act
    profile._bind_mcp_runtime_env(config, servers)

    # Assert
    env = servers["scitex-cards"]["env"]
    assert (
        env["SCITEX_STORE_DSN"].endswith(":55432/scitex"),
        "SCITEX_CARDS_DB" in env,
    ) == (True, False)


def test_mcp_pg_validation_refuses_unprovisioned_variant_role(tmp_path):
    # Arrange
    passfile = tmp_path / ".pgpass"
    passfile.write_text(
        "*:*:*:operator__scitex-agent-container:secret\n", encoding="utf-8"
    )
    passfile.chmod(0o600)
    servers = {
        "scitex-cards": {
            "env": {
                "SCITEX_STORE_DSN": "postgresql://scitex-primary:55432/scitex",
                "PGUSER": "operator__scitex-agent-container-gui",
                "PGPASSFILE": str(passfile),
            }
        }
    }
    # Act
    ctx = pytest.raises(RuntimeError, match="scitex-agent-container-gui")
    # Assert
    with ctx:
        profile._validate_mcp_pg_credentials(
            servers,
            launch_argv=("apptainer", "exec", "--bind", f"{passfile}:{passfile}"),
        )


def test_mcp_pg_validation_rejects_dsn_userinfo_that_overrides_pguser(tmp_path):
    # Arrange
    passfile = tmp_path / ".pgpass"
    passfile.write_text("*:*:*:operator__scitex-hub:secret\n", encoding="utf-8")
    passfile.chmod(0o600)
    servers = {
        "scitex-cards": {
            "env": {
                "SCITEX_STORE_DSN": (
                    "postgresql://different_role@scitex-primary:55432/scitex"
                ),
                "PGUSER": "operator__scitex-hub",
                "PGPASSFILE": "/creds/.pgpass",
            }
        }
    }
    # Act
    ctx = pytest.raises(RuntimeError, match="no credential")
    # Assert
    with ctx:
        profile._validate_mcp_pg_credentials(
            servers,
            launch_argv=(
                "apptainer",
                "exec",
                "--bind",
                f"{passfile}:/creds/.pgpass:ro",
            ),
        )


def test_mcp_pg_validation_does_not_treat_unbound_host_passfile_as_container_file(
    tmp_path,
):
    # Arrange
    # The host credential exists, but the generated MCP path names
    # /home/agent/.pgpass and neither materialization nor a bind supplies it.
    host_passfile = tmp_path / "host.pgpass"
    host_passfile.write_text("*:*:*:operator__project:secret\n", encoding="utf-8")
    host_passfile.chmod(0o600)
    servers = {
        "scitex-cards": {
            "env": {
                "SCITEX_STORE_DSN": "postgresql://scitex-primary:55432/scitex",
                "PGUSER": "operator__project",
                "PGPASSFILE": "/home/agent/.pgpass",
            }
        }
    }
    # Act
    ctx = pytest.raises(RuntimeError, match="no credential")
    # Assert
    with ctx:
        profile._validate_mcp_pg_credentials(
            servers,
            launch_argv=(
                "apptainer",
                "exec",
                "--bind",
                f"{host_passfile}:/unrelated/.pgpass:ro",
            ),
        )


def test_raw_args_env_and_bind_are_the_hermes_mcp_identity_source(tmp_path):
    # Arrange
    # Exact last-wins escape-hatch shape used by real relaxed specs.
    passfile = tmp_path / ".pgpass"
    passfile.write_text("*:*:*:operator__scitex-hub:secret\n", encoding="utf-8")
    passfile.chmod(0o600)
    config = AgentConfig(
        name="scitex-hub-deepseek",
        harness="hermes",
        env={"SCITEX_STORE_DSN": "postgresql://scitex-primary:55432/scitex"},
        labels={"project": "scitex-hub"},
    )
    config.apptainer.raw_args = [
        "--env",
        "PGUSER=operator__scitex-hub",
        "--env",
        "PGPASSFILE=/creds/.pgpass",
        "--bind",
        f"{passfile}:/creds/.pgpass:ro",
    ]
    servers = {
        "scitex-cards": {
            "command": "scitex-cards",
            "env": {"PGUSER": "${PGUSER}", "PGPASSFILE": "${PGPASSFILE}"},
        }
    }
    # Act
    profile._bind_mcp_runtime_env(config, servers)
    profile._validate_mcp_pg_credentials(
        servers, launch_argv=("apptainer", "exec", *config.apptainer.raw_args)
    )
    # Assert
    assert (
        servers["scitex-cards"]["env"]["PGUSER"],
        servers["scitex-cards"]["env"]["PGPASSFILE"],
    ) == ("operator__scitex-hub", "/creds/.pgpass")


def test_pg_validation_uses_first_bind_for_duplicate_destination(tmp_path):
    # Arrange
    # Apptainer keeps the first bind for an exact destination.
    invalid = tmp_path / "invalid.pgpass"
    invalid.write_text("*:*:*:some_other_role:secret\n", encoding="utf-8")
    invalid.chmod(0o600)
    valid = tmp_path / "valid.pgpass"
    valid.write_text("*:*:*:operator__scitex-hub:secret\n", encoding="utf-8")
    valid.chmod(0o600)
    servers = {
        "scitex-cards": {
            "env": {
                "SCITEX_STORE_DSN": "postgresql://scitex-primary:55432/scitex",
                "PGUSER": "operator__scitex-hub",
                "PGPASSFILE": "/creds/.pgpass",
            }
        }
    }
    # Act
    ctx = pytest.raises(RuntimeError, match="no credential")
    # Assert
    with ctx:
        profile._validate_mcp_pg_credentials(
            servers,
            launch_argv=(
                "apptainer",
                "exec",
                "--bind",
                f"{invalid}:/creds/.pgpass:ro",
                "--bind",
                f"{valid}:/creds/.pgpass:ro",
            ),
        )


def test_pg_validation_uses_longest_covering_bind_destination(tmp_path):
    # Arrange
    # A nested mount shadows its broader parent for this path.
    broad_home = tmp_path / "broad"
    nested_home = tmp_path / "nested"
    broad_home.mkdir()
    nested_home.mkdir()
    (broad_home / ".pgpass").write_text(
        "*:*:*:some_other_role:secret\n", encoding="utf-8"
    )
    (broad_home / ".pgpass").chmod(0o600)
    (nested_home / ".pgpass").write_text(
        "*:*:*:operator__scitex-hub:secret\n", encoding="utf-8"
    )
    (nested_home / ".pgpass").chmod(0o600)
    servers = {
        "scitex-cards": {
            "env": {
                "SCITEX_STORE_DSN": "postgresql://scitex-primary:55432/scitex",
                "PGUSER": "operator__scitex-hub",
                "PGPASSFILE": "/home/agent/.pgpass",
            }
        }
    }
    # Act
    result = profile._validate_mcp_pg_credentials(
        servers,
        launch_argv=(
            "apptainer",
            "exec",
            "--bind",
            f"{broad_home}:/home",
            "--bind",
            f"{nested_home}:/home/agent",
        ),
    )
    # Assert
    assert result is None


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
    config.hermes_compression = HermesCompressionSpec(
        threshold=0.85,
        threshold_tokens=524_288,
        target_ratio=0.30,
        tail_mode="legacy",
        in_place=False,
    )
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
    expected_headers = {
        "X-SciTeX-Agent-ID": "scholar",
        "X-SciTeX-Session-ID": "sac:scholar:qwen",
    }
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
        and parsed["providers"]["sac-qwen"]["extra_headers"] == expected_headers
        and parsed["compression"]
        == {
            "enabled": True,
            "threshold": 0.85,
            "threshold_tokens": 524_288,
            "target_ratio": 0.30,
            "tail_mode": "legacy",
            "in_place": False,
        }
        and env_text == "QWEN_KEY=secret\n"
        and len((tmp_path / profile.API_KEY_FILE).read_text().strip()) >= 16
        and (tmp_path / profile.API_KEY_FILE).stat().st_mode & 0o777 == 0o600
    )


def test_tui_profile_materializes_selected_cct_mcp_token_and_turn_url(tmp_path):
    # Arrange
    config = AgentConfig(
        name="business", harness="hermes", runtime="tui", workdir="/work"
    )
    config.engine_key = "qwen"
    config.model = "qwen-model"
    config.a2a.port = 19007
    config.claude.channels = ["server:claude-code-telegrammer"]
    config.claude.provider = ProviderSpec(
        base_url="http://qwen.example:8000/v1",
        auth_token_env="QWEN_KEY",
    )

    def deploy(_config, target):
        home = Path(target)
        (home / ".env").write_text(
            "CCT_BOT_TOKEN=token-materialized-by-existing-resolver\n",
            encoding="utf-8",
        )
        (home / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "claude-code-telegrammer": {
                            "command": "bun",
                            "args": ["run", "telegram-server.ts"],
                            "env": {"CCT_BOT_TOKEN": "${CCT_BOT_TOKEN}"},
                        },
                        "optional-browser": {"command": "browser-mcp"},
                    }
                }
            ),
            encoding="utf-8",
        )

    replacements = [
        (profile, "resolve_provider_api_key", lambda value: "engine-secret"),
        (profile, "deploy_to_home", deploy),
        (profile, "deploy_to_home_overlay", lambda value: None),
        (profile, "resolve_overlay_upper_home", lambda value: None),
    ]

    # Act
    with _replace_attributes(replacements):
        targets = profile.materialize_hermes_tui_profile(config, state_dir=tmp_path)
    rendered = yaml.safe_load(
        (targets[0] / ".hermes" / "config.yaml").read_text(encoding="utf-8")
    )
    profile_env = (targets[0] / ".hermes" / ".env").read_text(encoding="utf-8")

    # Assert
    assert (
        set(rendered["mcp_servers"]),
        rendered["mcp_servers"]["claude-code-telegrammer"]["env"][
            "CLAUDE_CODE_TELEGRAMMER_TURN_URL"
        ],
        rendered["mcp_servers"]["claude-code-telegrammer"]["env"][
            "CCT_BOT_TOKEN"
        ],
        "mcp-claude-code-telegrammer" in rendered["toolsets"],
        "CLAUDE_CODE_TELEGRAMMER_TURN_URL=http://127.0.0.1:19007/v1/turn"
        in profile_env,
    ) == (
        {"claude-code-telegrammer"},
        "http://127.0.0.1:19007/v1/turn",
        "${env:CCT_BOT_TOKEN}",
        True,
        True,
    )


def test_tui_profile_refuses_selected_cct_rail_without_token(tmp_path):
    # Arrange
    config = AgentConfig(
        name="business", harness="hermes", runtime="tui", workdir="/work"
    )
    config.engine_key = "qwen"
    config.model = "qwen-model"
    config.a2a.port = 19007
    config.claude.channels = ["server:claude-code-telegrammer"]
    config.env["CCT_BOT_TOKEN"] = ""
    config.claude.provider = ProviderSpec(
        base_url="http://qwen.example:8000/v1",
        auth_token_env="QWEN_KEY",
    )

    def deploy(_config, target):
        home = Path(target)
        (home / ".mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "claude-code-telegrammer": {"command": "bun"}
                    }
                }
            ),
            encoding="utf-8",
        )

    replacements = [
        (profile, "resolve_provider_api_key", lambda value: "engine-secret"),
        (profile, "deploy_to_home", deploy),
        (profile, "deploy_to_home_overlay", lambda value: None),
        (profile, "resolve_overlay_upper_home", lambda value: None),
    ]

    # Act
    ctx = pytest.raises(HermesCctRailError, match="no Telegram bot token")

    # Assert
    with _replace_attributes(replacements), ctx:
        profile.materialize_hermes_tui_profile(config, state_dir=tmp_path)


def test_tui_deepseek_profile_contains_only_neutral_gateway_credential(tmp_path):
    # Arrange
    config = AgentConfig(
        name="hub-flash", harness="hermes", runtime="tui", workdir="/work"
    )
    config.engine_key = "deepseek-flash"
    config.model = "deepseek-flash"
    entry = resolve_provider("external-gateway")
    config.claude.provider = ProviderSpec(**(entry or {}))
    replacements = [
        (profile, "resolve_provider_api_key", lambda value: "local-gateway-token"),
        (profile, "deploy_to_home", lambda value, target: None),
        (profile, "deploy_to_home_overlay", lambda value: None),
        (profile, "resolve_overlay_upper_home", lambda value: None),
    ]
    # Act
    with _replace_attributes(replacements):
        targets = profile.materialize_hermes_tui_profile(config, state_dir=tmp_path)
    rendered = yaml.safe_load(
        (targets[0] / ".hermes" / "config.yaml").read_text(encoding="utf-8")
    )
    env_text = (targets[0] / ".hermes" / ".env").read_text(encoding="utf-8")
    provider = rendered["providers"]["sac-deepseek-flash"]
    # Assert
    assert (
        provider["base_url"],
        provider["model"],
        env_text,
        "DEEPSEEK_API_KEY" in env_text,
    ) == (
        "http://scitex-compute-04:18775/v1",
        "deepseek-flash",
        "SCITEX_GENAI_GATEWAY_API_KEY=local-gateway-token\n",
        False,
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
