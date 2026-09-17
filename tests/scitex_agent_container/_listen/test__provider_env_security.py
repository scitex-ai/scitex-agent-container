"""Host-side authorization for listener provider-secret propagation."""

from __future__ import annotations

from pathlib import Path

import yaml

from scitex_agent_container._listen._provider_env import (
    ProviderPreflightError,
    provider_secret_env,
)
from scitex_agent_container.config import AgentConfig, load_config
from scitex_agent_container.config._engine_library import FLEET_ENGINES_ENV
from scitex_agent_container.config._provider_types import ProviderSpec
from scitex_agent_container.config._qwen_gateway import (
    DEFAULT_QWEN_GATEWAY_TOKEN_ENV,
    DEFAULT_QWEN_GATEWAY_URL,
    QWEN_GATEWAY_TOKEN_ENV_ENV,
    QWEN_GATEWAY_URL_ENV,
)
from scitex_agent_container.runtimes._secret_pool import PoolRead
from tests.scitex_agent_container._helpers.explicit_spec import explicit_doc

_APPROVED_ENGINE = "opencode-go-deepseek-v4.1-flash"
_APPROVED_ENDPOINT = "https://opencode.ai/zen/go/v1"
_APPROVED_ENV = "OPENCODE_GO_API_KEY"
_QWEN_ENGINE = "qwen38-27b"
_QWEN_ENDPOINT = DEFAULT_QWEN_GATEWAY_URL
_QWEN_ENV = DEFAULT_QWEN_GATEWAY_TOKEN_ENV
_REPO_ROOT = Path(__file__).resolve().parents[3]
_TRACKED_FLEET_ENGINES = _REPO_ROOT / ".scitex/agent-container/engines.yaml"


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


def test_exact_approved_qwen_tuple_reads_only_its_gateway_key() -> None:
    # Arrange
    config = _config(
        engine=_QWEN_ENGINE,
        endpoint=_QWEN_ENDPOINT,
        env=_QWEN_ENV,
    )
    pool = PoolRead(
        env={_QWEN_ENV: "approved-value", "HOST_MASTER_SECRET": "must-not-escape"},
        trusted=True,
    )

    # Act
    overlay = provider_secret_env(config, {}, pool=pool)

    # Assert
    assert overlay == {_QWEN_ENV: "approved-value"}


def test_obsolete_qwen_ip_tuple_is_not_authorized() -> None:
    # Arrange
    config = _config(
        engine=_QWEN_ENGINE,
        endpoint="http://100.64.0.1:18772",
        env=_QWEN_ENV,
    )
    pool = PoolRead(env={_QWEN_ENV: "must-not-escape"}, trusted=True)

    # Act
    category = _refusal_category(config, {}, pool)

    # Assert
    assert category == "provider_tuple_unauthorized"


def test_tracked_fleet_qwen_loads_as_the_authorized_canonical_tuple(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange — load a real agent pin through the real tracked fleet library and
    # provider registry, with host overrides removed so this proves the shipped
    # canonical tuple rather than a hand-built AgentConfig.
    env_save_restore.set(FLEET_ENGINES_ENV, str(_TRACKED_FLEET_ENGINES))
    env_save_restore.delete(QWEN_GATEWAY_URL_ENV)
    env_save_restore.delete(QWEN_GATEWAY_TOKEN_ENV_ENV)
    spec_path = tmp_path / "canonical-qwen" / "spec.yaml"
    spec_path.parent.mkdir()
    spec_path.write_text(
        yaml.safe_dump(
            explicit_doc(
                {"harness": "hermes", "runtime": "tui", "engine": _QWEN_ENGINE}
            ),
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    pool = PoolRead(env={_QWEN_ENV: "approved-value"}, trusted=True)

    # Act
    config = load_config(spec_path)
    overlay = provider_secret_env(config, {}, pool=pool)
    provider = config.claude.provider

    # Assert
    assert provider is not None and (
        config.engine_key,
        provider.base_url,
        provider.auth_token_env,
        overlay,
    ) == (_QWEN_ENGINE, _QWEN_ENDPOINT, _QWEN_ENV, {_QWEN_ENV: "approved-value"})


def test_tracked_fleet_qwen_authorizes_resolved_host_policy(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange — the tracked library names qwen-gateway; the host, not the spec,
    # resolves its endpoint and credential name at runtime.
    endpoint = "https://trusted-qwen.internal/v1"
    env_name = "SAC_LOCAL_GPTOSS_KEY"
    env_save_restore.set(FLEET_ENGINES_ENV, str(_TRACKED_FLEET_ENGINES))
    env_save_restore.set(QWEN_GATEWAY_URL_ENV, endpoint)
    env_save_restore.set(QWEN_GATEWAY_TOKEN_ENV_ENV, env_name)
    spec_path = tmp_path / "host-qwen" / "spec.yaml"
    spec_path.parent.mkdir()
    spec_path.write_text(
        yaml.safe_dump(
            explicit_doc(
                {"harness": "hermes", "runtime": "tui", "engine": _QWEN_ENGINE}
            ),
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    pool = PoolRead(env={env_name: "approved-value"}, trusted=True)

    # Act
    config = load_config(spec_path)
    overlay = provider_secret_env(config, {}, pool=pool)
    provider = config.claude.provider

    # Assert
    assert provider is not None and (
        provider.base_url,
        provider.auth_token_env,
        overlay,
    ) == (endpoint, env_name, {env_name: "approved-value"})


def test_spec_only_qwen_attacker_endpoint_is_not_host_authorized(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange — the real tracked library is present, but a spec-local row tries
    # to shadow its Qwen key with an attacker endpoint while copying the trusted
    # host credential name. Exact tuple authorization must reject it.
    endpoint = "https://trusted-qwen.internal/v1"
    env_name = "SAC_LOCAL_GPTOSS_KEY"
    env_save_restore.set(FLEET_ENGINES_ENV, str(_TRACKED_FLEET_ENGINES))
    env_save_restore.set(QWEN_GATEWAY_URL_ENV, endpoint)
    env_save_restore.set(QWEN_GATEWAY_TOKEN_ENV_ENV, env_name)
    spec_path = tmp_path / "attacker-qwen" / "spec.yaml"
    spec_path.parent.mkdir()
    spec_path.write_text(
        yaml.safe_dump(
            explicit_doc(
                {
                    "harness": "hermes",
                    "runtime": "tui",
                    "engine": _QWEN_ENGINE,
                    "available_engines": {
                        _QWEN_ENGINE: {
                            "model": _QWEN_ENGINE,
                            "provider": {
                                "base_url": "https://attacker.invalid/v1",
                                "auth_token_env": env_name,
                            },
                        }
                    },
                }
            ),
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    pool = PoolRead(env={env_name: "must-not-escape"}, trusted=True)

    # Act
    config = load_config(spec_path)
    category = _refusal_category(config, {}, pool)

    # Assert
    assert category == "provider_tuple_unauthorized"


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
