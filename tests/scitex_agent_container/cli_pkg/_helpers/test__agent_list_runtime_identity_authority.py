"""Runtime identity shown by listings comes from launch evidence, not dotfiles."""

from __future__ import annotations

import json

from scitex_agent_container._lifecycle._runtime_identity import (
    resolve_runtime_identity,
)
from scitex_agent_container._state.state_store_incarnations import get_incarnations
from scitex_agent_container.config import AgentConfig
from scitex_agent_container.config._provider_types import ProviderSpec


def _birth(**overrides) -> dict:
    snapshot = {
        "runtime": "tui",
        "harness": "hermes",
        "engine_key": "actual-engine",
        "model": "actual-model",
        "subscription_provider": "",
        "subscription_account": "",
        "claude": {"account": "", "provider": None},
    }
    snapshot.update(overrides)
    return {"compiled_spec_json": json.dumps(snapshot)}


def test_running_identity_uses_birth_certificate_over_stale_spec() -> None:
    # Arrange
    stale = AgentConfig(
        name="alpha",
        runtime="apptainer",
        harness="anthropic",
        engine_key="stale-engine",
        model="stale-model",
    )

    # Act
    identity = resolve_runtime_identity(stale, running=True, birth_record=_birth())

    # Assert
    assert (
        identity["runtime"],
        identity["harness"],
        identity["engine"],
        identity["model"],
        identity["runtime_identity_source"],
    ) == ("tui", "hermes", "actual-engine", "actual-model", "birth_certificate")


def test_subscription_identity_is_provider_slash_account() -> None:
    # Arrange
    birth = _birth(
        subscription_provider="openai",
        subscription_account="openai:research-plan",
    )

    # Act
    identity = resolve_runtime_identity(
        None,
        running=True,
        birth_record=birth,
    )

    # Assert
    assert (identity["billing_mode"], identity["auth_identity"]) == (
        "subscription",
        "openai/research-plan",
    )


def test_api_auth_is_opaque_without_claiming_usage_billing() -> None:
    # Arrange
    birth = _birth(
        claude={
            "account": "",
            "provider": {
                "base_url": "https://provider.invalid/v1",
                "auth_token_env": "DEEPSEEK_API_KEY",
            },
        }
    )

    # Act
    identity = resolve_runtime_identity(None, running=True, birth_record=birth)

    # Assert
    assert (identity["billing_mode"], identity["auth_identity"]) == (
        "unspecified",
        "api-key",
    )


def test_explicit_usage_billing_declaration_is_preserved() -> None:
    # Arrange
    birth = _birth(billing_mode="usage")

    # Act
    identity = resolve_runtime_identity(
        None,
        running=True,
        birth_record=birth,
    )

    # Assert
    assert identity["billing_mode"] == "usage"


def test_claude_oauth_identity_names_actual_stored_account() -> None:
    # Arrange
    birth = _birth(
        harness="anthropic", claude={"account": "team-max", "provider": None}
    )

    # Act
    identity = resolve_runtime_identity(
        None,
        running=True,
        birth_record=birth,
    )

    # Assert
    assert (identity["billing_mode"], identity["auth_identity"]) == (
        "unspecified",
        "claude-code:team-max",
    )


def test_claude_oauth_pool_path_does_not_invent_an_account_label() -> None:
    # Arrange
    birth = _birth(
        harness="anthropic",
        claude={
            "account": "",
            "provider": None,
            "credentials_file": "/safe/accounts/picked-max/.credentials.json",
        }
    )

    # Act
    identity = resolve_runtime_identity(
        None,
        running=True,
        birth_record=birth,
    )

    # Assert
    assert identity["auth_identity"] == "unknown"


def test_spec_fallback_is_labelled_when_birth_is_unavailable() -> None:
    # Arrange
    config = AgentConfig(
        name="alpha", harness="hermes", engine_key="declared", model="m"
    )
    config.claude.provider = ProviderSpec(auth_token_env="SAFE_ENV_NAME")

    # Act
    identity = resolve_runtime_identity(config, running=True, birth_record=None)

    # Assert
    assert (
        identity["engine"],
        identity["auth_identity"],
        identity["runtime_identity_source"],
    ) == ("declared", "api-key", "spec")


def test_unknown_identity_is_explicit_when_no_evidence_exists() -> None:
    # Arrange
    config = None

    # Act
    identity = resolve_runtime_identity(config, running=True, birth_record=None)

    # Assert
    assert (
        identity["billing_mode"],
        identity["auth_identity"],
        identity["runtime_identity_source"],
    ) == ("unspecified", "unknown", "unknown")


def test_identity_never_emits_secret_value() -> None:
    # Arrange
    sensitive = "sensitive-value-should-not-leak"
    birth = _birth(
        claude={
            "account": "",
            "provider": {"auth_token_env": "TOKEN_ENV", "api_key": sensitive},
        },
        env={"TOKEN_ENV": sensitive},
    )

    # Act
    rendered = json.dumps(
        resolve_runtime_identity(None, running=True, birth_record=birth)
    )

    # Assert
    assert sensitive not in rendered and "TOKEN_ENV" not in rendered


def test_incarnation_batch_reader_uses_one_bounded_search_not_per_id_gets() -> None:
    # Arrange
    class _Row:
        def __init__(self, incarnation_id: str) -> None:
            self.values = {
                "incarnation_id": incarnation_id,
                "compiled_spec_json": "{}",
            }

    class _Store:
        def __init__(self) -> None:
            self.search_calls = 0
            self.query_limit = None
            self.closed = False

        def search(self, query):
            self.search_calls += 1
            self.query_limit = query.limit
            return [_Row("inc-1"), _Row("inc-2")]

        def get(self, key):
            raise AssertionError("N+1 get() must not be used")

        def close(self) -> None:
            self.closed = True

    store = _Store()

    # Act
    records = get_incarnations(["inc-1", "inc-2", "inc-1"], store_factory=lambda: store)

    # Assert
    assert (
        set(records),
        store.search_calls,
        store.query_limit,
        store.closed,
    ) == ({"inc-1", "inc-2"}, 1, 2, True)
