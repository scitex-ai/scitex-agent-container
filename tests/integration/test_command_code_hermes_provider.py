"""Command Code DeepSeek V4.1 Flash is a fail-closed Hermes engine."""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container._listen._provider_env import (
    ProviderPreflightError,
    provider_secret_env,
)
from scitex_agent_container.config import load_config
from scitex_agent_container.config._engine_library import load_fleet_library
from scitex_agent_container.config._hermes_config import compile_hermes_config
from scitex_agent_container.runtimes import _hermes_profile
from scitex_agent_container.runtimes._apptainer_provider import ProviderEnvError
from scitex_agent_container.runtimes._secret_pool import PoolRead

ROOT = Path(__file__).parents[2]
EXAMPLE = ROOT / "examples" / "providers" / "command-code-hermes.yaml"
LIBRARY = ROOT / ".scitex" / "agent-container" / "engines.yaml"
ENGINE = "command-code-deepseek-v4.1-flash"


def test_command_code_example_compiles_exact_hermes_backend_without_fallback(
    env_save_restore,
) -> None:
    # Arrange
    env_save_restore.set("COMMAND_CODE_API_KEY", "secret-must-not-be-serialized")

    # Act
    config = load_config(EXAMPLE)
    plan = _hermes_profile._launch_plan(config, launch_mode="tui")
    rendered = compile_hermes_config(plan, workdir="/work")
    provider = rendered["providers"][f"sac-{ENGINE}"]

    # Assert
    assert (
        config.harness,
        plan.engine.key,
        plan.engine.model_id,
        plan.endpoint.url,
        plan.endpoint.auth_env,
        provider["base_url"],
        provider["key_env"],
        rendered["fallback_providers"],
        "secret-must-not-be-serialized" in repr(rendered),
    ) == (
        "hermes",
        ENGINE,
        "deepseek/deepseek-v4.1-flash",
        "https://api.commandcode.ai/provider/v1/chat/completions",
        "COMMAND_CODE_API_KEY",
        "https://api.commandcode.ai/provider/v1",
        "COMMAND_CODE_API_KEY",
        [],
        False,
    )


def test_tracked_fleet_library_exposes_command_code_as_manual_engine() -> None:
    # Arrange
    library_path = LIBRARY

    # Act
    library = load_fleet_library(library_path)
    engine = library.engines.get(ENGINE)

    # Assert
    assert (
        library.default_key,
        engine.model if engine else None,
        engine.provider.base_url if engine and engine.provider else None,
        engine.provider.auth_token_env if engine and engine.provider else None,
    ) == (
        "",
        "deepseek/deepseek-v4.1-flash",
        "https://api.commandcode.ai/provider/v1",
        "COMMAND_CODE_API_KEY",
    )


def test_command_code_missing_key_refuses_without_provider_fallback(
    env_save_restore, tmp_path
) -> None:
    # Arrange
    env_save_restore.set("HOME", str(tmp_path))
    env_save_restore.delete("COMMAND_CODE_API_KEY")
    config = load_config(EXAMPLE)

    # Act
    def action() -> str:
        return _hermes_profile.resolve_provider_api_key(config)

    # Assert
    with pytest.raises(ProviderEnvError, match="COMMAND_CODE_API_KEY"):
        action()


def test_listener_authorizes_only_the_exact_command_code_provider_tuple() -> None:
    # Arrange
    config = load_config(EXAMPLE)
    pool = PoolRead(
        env={"COMMAND_CODE_API_KEY": "fixture-provider-key"},
        trusted=True,
        detail="",
    )

    # Act
    overlay = provider_secret_env(config, {}, pool=pool)

    # Assert
    assert (tuple(overlay), bool(overlay["COMMAND_CODE_API_KEY"])) == (
        ("COMMAND_CODE_API_KEY",),
        True,
    )


def test_listener_rejects_command_code_key_for_an_attacker_endpoint() -> None:
    # Arrange
    config = load_config(EXAMPLE)
    provider = config.claude.provider
    if provider is None:
        raise RuntimeError("fixture did not load a provider")
    provider.base_url = "https://attacker.invalid/v1"
    pool = PoolRead(
        env={"COMMAND_CODE_API_KEY": "fixture-provider-key"},
        trusted=True,
        detail="",
    )

    # Act
    raises_ctx = pytest.raises(
        ProviderPreflightError, match="provider_tuple_unauthorized"
    )

    # Assert
    with raises_ctx:
        provider_secret_env(config, {}, pool=pool)
