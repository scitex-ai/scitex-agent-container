"""Tests for ``config._provider_registry`` (PS-204 mirror).

Backend metadata for ``spec.claude.provider`` as a registered string
identifier (ADR-0011 extension, operator directive 2026-05-28 msg 6783).
Each test pins one observable fact (TQ007), AAA markers on separate
lines (TQ002), descriptive name with ≥3 tokens (TQ003).
"""

from __future__ import annotations

from scitex_agent_container.config._provider_registry import (
    PROVIDERS,
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


def test_resolve_provider_returns_local_codex_gateway():
    # Arrange
    name = "codex"
    # Act
    entry = resolve_provider(name)
    # Assert
    assert entry == {
        "base_url": "http://127.0.0.1:18765",
        "auth_token_env": "SCITEX_GENAI_GATEWAY_API_KEY",
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


def test_list_providers_includes_registered_backends():
    # Arrange
    expected = {"mimo", "deepseek", "anthropic", "codex", "xiaomi"}
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


# The harness registry (``spec.harness``) used to be a second, unrelated
# constant in this module. It moved to ``config._harness_types`` with the
# spec-key migration; its tests live in ``test__harness_types.py``.
