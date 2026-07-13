"""Tests for ``config._provider_registry`` (PS-204 mirror).

Backend metadata for ``spec.claude.provider`` as a registered string
identifier (ADR-0011 extension, operator directive 2026-05-28 msg 6783).
Each test pins one observable fact (TQ007), AAA markers on separate
lines (TQ002), descriptive name with ≥3 tokens (TQ003).
"""

from __future__ import annotations

from scitex_agent_container.config._provider_registry import (
    AGENT_SDK_PROVIDERS,
    PROVIDERS,
    is_known_agent_provider,
    list_agent_providers,
    list_providers,
    resolve_provider,
)


def test_resolve_provider_returns_dict_for_known_mimo():
    # Arrange
    name = "mimo"
    # Act
    entry = resolve_provider(name)
    # Assert
    assert entry == {
        "base_url": "https://token-plan-sgp.xiaomimimo.com/anthropic",
        "auth_token_env": "XIAOMI_API_KEY",
    }


def test_resolve_provider_returns_dict_for_known_deepseek():
    # Arrange
    name = "deepseek"
    # Act
    entry = resolve_provider(name)
    # Assert
    assert entry == {
        "base_url": "https://api.deepseek.com/anthropic",
        "auth_token_env": "DEEPSEEK_API_KEY",
    }


def test_resolve_provider_returns_none_for_unknown_name():
    # Arrange
    name = "this-provider-does-not-exist"
    # Act
    entry = resolve_provider(name)
    # Assert
    assert entry is None


def test_resolve_provider_anthropic_has_no_overrides_set():
    # Arrange
    name = "anthropic"
    # Act
    entry = resolve_provider(name)
    # Assert
    assert entry == {"base_url": None, "auth_token_env": None}


def test_resolve_provider_returns_copy_not_shared_reference():
    # Arrange
    name = "deepseek"
    # Act
    entry = resolve_provider(name)
    entry["base_url"] = "mutated"
    # Assert
    assert PROVIDERS["deepseek"]["base_url"] != "mutated"


def test_list_providers_includes_mimo_and_deepseek():
    # Arrange
    expected = {"mimo", "deepseek", "anthropic", "xiaomi"}
    # Act
    names = set(list_providers())
    # Assert
    assert expected.issubset(names)


def test_list_providers_returns_sorted_order_for_stable_diagnostics():
    # Arrange
    # Act
    names = list_providers()
    # Assert
    assert names == sorted(names)


def test_xiaomi_alias_resolves_to_same_backend_as_mimo():
    # Arrange
    # Act
    xiaomi = resolve_provider("xiaomi")
    # Assert
    assert xiaomi == resolve_provider("mimo")


# ---------------------------------------------------------------------------
# AGENT_SDK_PROVIDERS — spec.provider (TOP-LEVEL agent SDK family selector;
# openai-compat-1 foundation). A SEPARATE, flat registry from PROVIDERS
# above — see the naming-collision note in config._provider_types.AgentProvider.
# ---------------------------------------------------------------------------


def test_agent_sdk_providers_is_exactly_anthropic_and_openai():
    # Arrange
    expected = {"anthropic", "openai"}
    # Act
    names = set(AGENT_SDK_PROVIDERS)
    # Assert
    assert names == expected


def test_is_known_agent_provider_true_for_anthropic():
    # Arrange
    name = "anthropic"
    # Act
    known = is_known_agent_provider(name)
    # Assert
    assert known is True


def test_is_known_agent_provider_true_for_openai():
    # Arrange
    name = "openai"
    # Act
    known = is_known_agent_provider(name)
    # Assert
    assert known is True


def test_is_known_agent_provider_false_for_unregistered_name():
    # Arrange
    name = "totally-made-up-sdk"
    # Act
    known = is_known_agent_provider(name)
    # Assert
    assert known is False


def test_list_agent_providers_returns_sorted_order():
    # Arrange
    # Act
    names = list_agent_providers()
    # Assert
    assert names == sorted(names)


def test_list_agent_providers_matches_agent_sdk_providers_set():
    # Arrange
    # Act
    names = set(list_agent_providers())
    # Assert
    assert names == set(AGENT_SDK_PROVIDERS)
