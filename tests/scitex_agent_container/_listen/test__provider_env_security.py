"""Host-side authorization for listener provider-secret propagation."""

from __future__ import annotations

from scitex_agent_container._listen._provider_env import (
    ProviderPreflightError,
    provider_secret_env,
)
from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._provider_types import ProviderSpec
from scitex_agent_container.runtimes._secret_pool import PoolRead

_APPROVED_ENGINE = "opencode-go-deepseek-v4.1-flash"
_APPROVED_ENDPOINT = "https://opencode.ai/zen/go/v1"
_APPROVED_ENV = "OPENCODE_GO_API_KEY"


def _config(
    *,
    engine: str = _APPROVED_ENGINE,
    endpoint: str = _APPROVED_ENDPOINT,
    env: str = _APPROVED_ENV,
) -> AgentConfig:
    config = AgentConfig(name="provider-security", harness="hermes")
    config.engine_key = engine
    config.claude.provider = ProviderSpec(base_url=endpoint, auth_token_env=env)
    return config


def _refusal_category(
    config: AgentConfig, child_env: dict[str, str], pool: PoolRead
) -> str:
    try:
        provider_secret_env(config, child_env, pool=pool)
    except ProviderPreflightError as exc:
        return exc.category
    return "not_refused"


def test_exact_approved_opencode_tuple_reads_only_its_key() -> None:
    # Arrange
    pool = PoolRead(
        env={
            _APPROVED_ENV: "approved-value",
            "HOST_MASTER_SECRET": "must-not-escape",
        },
        trusted=True,
    )

    # Act
    overlay = provider_secret_env(_config(), {}, pool=pool)

    # Assert
    assert overlay == {_APPROVED_ENV: "approved-value"}


def test_spec_controlled_arbitrary_env_is_refused() -> None:
    # Arrange
    config = _config(env="HOST_MASTER_SECRET")
    pool = PoolRead(env={"HOST_MASTER_SECRET": "must-not-escape"}, trusted=True)

    # Act
    category = _refusal_category(config, {}, pool)

    # Assert
    assert category == "provider_tuple_unauthorized"


def test_approved_env_with_attacker_endpoint_is_refused() -> None:
    # Arrange
    config = _config(endpoint="https://attacker.invalid/v1")
    pool = PoolRead(env={_APPROVED_ENV: "must-not-escape"}, trusted=True)

    # Act
    category = _refusal_category(config, {}, pool)

    # Assert
    assert category == "provider_tuple_unauthorized"


def test_inherited_authorized_value_precedes_pool_value() -> None:
    # Arrange
    pool = PoolRead(env={_APPROVED_ENV: "pool-value"}, trusted=True)

    # Act
    overlay = provider_secret_env(
        _config(), {_APPROVED_ENV: "inherited-value"}, pool=pool
    )

    # Assert
    assert overlay == {_APPROVED_ENV: "inherited-value"}


def test_missing_authorized_key_refuses_without_fallback() -> None:
    # Arrange
    pool = PoolRead(env={"SOME_OTHER_KEY": "not-a-fallback"}, trusted=True)

    # Act
    category = _refusal_category(_config(), {}, pool)

    # Assert
    assert category == "provider_key_unavailable"


def test_untrusted_pool_cannot_supply_provider_key() -> None:
    # Arrange
    pool = PoolRead(
        env={_APPROVED_ENV: "unverified-value"}, trusted=False, detail="unsafe"
    )

    # Act
    category = _refusal_category(_config(), {}, pool)

    # Assert
    assert category == "provider_pool_untrusted"
