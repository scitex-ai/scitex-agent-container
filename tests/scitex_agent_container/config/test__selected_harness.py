"""Real canonical files retain their default while selecting one declared route."""

from __future__ import annotations

import copy

import pytest
import yaml

from scitex_agent_container.config import load_config
from scitex_agent_container.config._explicit_validation import explicit_spec_defaults
from scitex_agent_container.config._selected_harness import project_declared_harness


def _write_declaration(tmp_path):
    spec = explicit_spec_defaults("Agent")
    spec.pop("claude", None)
    spec.pop("engines", None)
    spec.pop("watchdog", None)
    spec.pop("container", None)
    spec["comms"] = {
        **spec.get("comms", {}),
        "channels": ["server:sac", "server:scitex-cards"],
    }
    spec.update(
        runtime="tui",
        harness="codex",
        engine="native",
        workdir=str(tmp_path),
        host="${HOSTNAME}",
        available_engines={
            "native": {
                "model": "gpt-6.1-sol",
                "subscription": {"provider": "openai", "account": "openai:owned"},
                "reasoning_effort": "ultra",
                "service_tier": "fast",
            },
            "go-muse": {
                "model": "muse-spark-1.3-contributor",
                "max_context_tokens": 1048576,
                "provider": {
                    "hermes_provider": "opencode-go",
                    "auth_token_env": "TEST_GO_TWO",
                },
            },
        },
        available_harnesses={
            "codex": {
                "session": {"mode": "continue", "max_age_minutes": None},
                "approval_policy": "never",
                "sandbox_mode": "danger-full-access",
            },
            "hermes": {
                "session": {"mode": "continue", "max_age_minutes": None},
                "failover": {"accounts": {"go-muse": ["TEST_GO_TWO"]}, "engines": []},
            },
        },
    )
    folder = tmp_path / "app"
    folder.mkdir()
    path = folder / "spec.yaml"
    raw = {"apiVersion": "scitex-agent-container/v3", "kind": "Agent", "spec": spec}
    path.write_text(yaml.safe_dump(raw))
    return path, raw


@pytest.fixture
def declaration(tmp_path):
    return _write_declaration(tmp_path)


def test_selected_route_is_parsed_before_subscription_validation(declaration):
    # Arrange
    path, _raw = declaration
    # Act
    config = load_config(path, harness_override="hermes", engine_override="go-muse")
    # Assert
    assert config.model == "muse-spark-1.3-contributor"


def test_selected_harness_options_are_actually_loaded(declaration):
    # Arrange
    path, _raw = declaration
    # Act
    config = load_config(path, harness_override="hermes", engine_override="go-muse")
    # Assert
    assert config.hermes_failover.accounts == {"go-muse": ["TEST_GO_TWO"]}


def test_api_projection_clears_native_subscription_account(declaration):
    # Arrange
    path, _raw = declaration
    # Act
    config = load_config(path, harness_override="hermes", engine_override="go-muse")
    # Assert
    assert config.subscription_account == ""


def test_api_projection_clears_native_subscription_provider(declaration):
    # Arrange
    path, _raw = declaration
    # Act
    config = load_config(path, harness_override="hermes", engine_override="go-muse")
    # Assert
    assert config.subscription_provider == ""


def test_api_projection_selects_declared_go_account_alias(declaration):
    # Arrange
    path, _raw = declaration
    # Act
    config = load_config(path, harness_override="hermes", engine_override="go-muse")
    # Assert
    assert config.claude.provider.auth_token_env == "TEST_GO_TWO"


def test_api_projection_selects_declared_go_provider(declaration):
    # Arrange
    path, _raw = declaration
    # Act
    config = load_config(path, harness_override="hermes", engine_override="go-muse")
    # Assert
    assert config.claude.provider.hermes_provider == "opencode-go"


def test_api_projection_does_not_keep_a_native_legacy_account(declaration):
    # Arrange
    path, _raw = declaration
    # Act
    config = load_config(path, harness_override="hermes", engine_override="go-muse")
    # Assert
    assert config.claude.account == ""


def test_selected_projection_does_not_rewrite_canonical_file(declaration):
    # Arrange
    path, _raw = declaration
    original = path.read_bytes()
    # Act
    load_config(path, harness_override="hermes", engine_override="go-muse")
    # Assert
    assert path.read_bytes() == original


def test_selected_projection_does_not_poison_cached_default(declaration):
    # Arrange
    path, _raw = declaration
    load_config(path, harness_override="hermes", engine_override="go-muse")
    # Act
    original = load_config(path)
    # Assert
    assert original.harness == "codex"


def test_selected_projection_keeps_native_default_account(declaration):
    # Arrange
    path, _raw = declaration
    load_config(path, harness_override="hermes", engine_override="go-muse")
    # Act
    original = load_config(path)
    # Assert
    assert original.subscription_account == "openai:owned"


def test_projection_does_not_mutate_caller_mapping(declaration):
    # Arrange
    _path, raw = declaration
    original = copy.deepcopy(raw)
    # Act
    project_declared_harness(raw, "hermes", engine="go-muse")
    # Assert
    assert raw == original


@pytest.mark.parametrize("requested", ["", "missing-harness", "opencode"])
def test_undeclared_harness_refuses(declaration, requested):
    # Arrange
    path, _raw = declaration

    # Act
    def operation():
        load_config(path, harness_override=requested, engine_override="go-muse")

    # Assert
    with pytest.raises(ValueError):
        operation()


@pytest.mark.parametrize("engine", ["", "missing-engine"])
def test_undeclared_engine_refuses(declaration, engine):
    # Arrange
    path, _raw = declaration

    # Act
    def operation():
        load_config(path, harness_override="hermes", engine_override=engine)

    # Assert
    with pytest.raises(ValueError, match="selected-harness-engine"):
        operation()


def test_hermes_projection_does_not_bypass_subscription_harness_rule(declaration):
    # Arrange
    path, _raw = declaration

    # Act
    def operation():
        load_config(path, harness_override="hermes", engine_override="native")

    # Assert
    with pytest.raises(ValueError):
        operation()


def test_dormant_invalid_failover_becomes_a_selected_refusal(declaration):
    # Arrange
    path, raw = declaration
    raw["spec"]["available_harnesses"]["hermes"]["failover"] = {
        "accounts": {"go-muse": []}
    }
    path.write_text(yaml.safe_dump(raw))

    # Act
    def operation():
        load_config(path, harness_override="hermes", engine_override="go-muse")

    # Assert
    with pytest.raises(ValueError):
        operation()
