"""Tests for ``config._provider_validation`` (PS-204 mirror).

Validation rules for ``spec.claude.provider`` — string form via
registry lookup + existing dict form back-compat. ADR-0011 extension,
operator directive 2026-05-28 msg 6783. Each test pins one observable
fact (TQ007), AAA markers on separate lines (TQ002), descriptive name
with ≥3 tokens (TQ003).
"""

from __future__ import annotations

from scitex_agent_container.config._provider_validation import (
    provider_is_active,
    validate_provider,
)

_VALID_DICT = {
    "base_url": "https://api.deepseek.com/anthropic",
    "auth_token_env": "DEEPSEEK_API_KEY",
}


# ---------------------------------------------------------------------------
# String form
# ---------------------------------------------------------------------------


def test_string_form_known_provider_returns_no_errors():
    # Arrange
    block = "mimo"
    # Act
    errors = validate_provider(block)
    # Assert
    assert errors == []


def test_string_form_unknown_provider_returns_loud_error():
    # Arrange
    block = "not-a-real-provider"
    # Act
    errors = validate_provider(block)
    # Assert
    assert any("not a registered provider name" in e for e in errors)


def test_string_form_unknown_provider_lists_known_providers_in_error():
    # Arrange
    block = "not-a-real-provider"
    # Act
    errors = validate_provider(block)
    # Assert
    assert any("mimo" in e and "deepseek" in e for e in errors)


# ---------------------------------------------------------------------------
# Dict form (back-compat unchanged)
# ---------------------------------------------------------------------------


def test_dict_form_complete_block_returns_no_errors():
    # Arrange
    block = dict(_VALID_DICT)
    # Act
    errors = validate_provider(block)
    # Assert
    assert errors == []


def test_dict_form_missing_base_url_returns_required_error():
    # Arrange
    block = {"auth_token_env": "X_KEY"}
    # Act
    errors = validate_provider(block)
    # Assert
    assert any("base_url" in e and "required" in e for e in errors)


def test_dict_form_missing_auth_token_env_returns_required_error():
    # Arrange
    block = {"base_url": "https://x.example.com"}
    # Act
    errors = validate_provider(block)
    # Assert
    assert any("auth_token_env" in e and "required" in e for e in errors)


def test_dict_form_non_string_base_url_returns_type_error():
    # Arrange
    block = {"base_url": 42, "auth_token_env": "X_KEY"}
    # Act
    errors = validate_provider(block)
    # Assert
    assert any("base_url" in e and "must be a string" in e for e in errors)


# ---------------------------------------------------------------------------
# Absent / null
# ---------------------------------------------------------------------------


def test_absent_provider_block_returns_no_errors():
    # Arrange
    block = None
    # Act
    errors = validate_provider(block)
    # Assert
    assert errors == []


# ---------------------------------------------------------------------------
# Active predicate (mutual-exclusion with account)
# ---------------------------------------------------------------------------


def test_active_predicate_true_for_known_string_with_base_url():
    # Arrange
    block = "deepseek"
    # Act
    result = provider_is_active(block)
    # Assert
    assert result is True


def test_active_predicate_false_for_anthropic_sentinel_string():
    # Arrange
    block = "anthropic"
    # Act
    result = provider_is_active(block)
    # Assert
    assert result is False


def test_active_predicate_false_for_unknown_provider_string():
    # Arrange
    block = "not-a-provider"
    # Act
    result = provider_is_active(block)
    # Assert
    assert result is False


def test_active_predicate_true_for_valid_dict_form():
    # Arrange
    block = dict(_VALID_DICT)
    # Act
    result = provider_is_active(block)
    # Assert
    assert result is True


def test_active_predicate_false_for_missing_block():
    # Arrange
    block = None
    # Act
    result = provider_is_active(block)
    # Assert
    assert result is False
