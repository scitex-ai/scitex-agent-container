"""Tests for ``a2a._card_model.resolve_card_model``.

Lead directive 431365c (2026-06-08): the AgentCard surface MUST
reflect the resolved model id, not just ``spec.claude.model``. Each
test pins one observable fact (TQ007), AAA markers (TQ002),
descriptive name with >=3 tokens (TQ003).
"""

from __future__ import annotations

from scitex_agent_container.a2a._card_model import resolve_card_model


def test_custom_provider_explicit_model_wins():
    # Arrange — Form C: type=custom carries its own model.
    spec = {
        "claude": {
            "model": "fallback-claude",
            "provider": {
                "type": "custom",
                "label": "x",
                "endpoint": {"base_url": "https://x"},
                "model": "qwen36-35b-a3b",
                "auth_token_env": "K",
            },
        }
    }
    # Act
    result = resolve_card_model(spec)
    # Assert
    assert result == "qwen36-35b-a3b"


def test_form_b_model_override_wins_over_registry_default():
    # Arrange — Form B inlines a model override.
    spec = {
        "claude": {
            "provider": {"name": "deepseek", "model": "deepseek-reasoner"},
        }
    }
    # Act
    result = resolve_card_model(spec)
    # Assert
    assert result == "deepseek-reasoner"


def test_form_a_bare_string_uses_registry_default_model():
    # Arrange — no explicit model, registry default carries it.
    spec = {"claude": {"provider": "deepseek"}}
    # Act
    result = resolve_card_model(spec)
    # Assert
    assert result == "deepseek-chat"


def test_form_b_name_only_uses_registry_default_model():
    # Arrange
    spec = {"claude": {"provider": {"name": "deepseek"}}}
    # Act
    result = resolve_card_model(spec)
    # Assert
    assert result == "deepseek-chat"


def test_no_provider_falls_back_to_spec_claude_model():
    # Arrange
    spec = {"claude": {"model": "opus[1m]"}}
    # Act
    result = resolve_card_model(spec)
    # Assert
    assert result == "opus[1m]"


def test_v2_legacy_spec_model_used_when_no_claude_model():
    # Arrange — pre-v3 specs put model at top-level spec.model.
    spec = {"model": "legacy-v2-model"}
    # Act
    result = resolve_card_model(spec)
    # Assert
    assert result == "legacy-v2-model"


def test_provider_active_but_no_model_falls_back_to_claude_model():
    # Arrange — registry default_model is None (e.g. mimo) and the
    # operator left provider as a bare name; the card surfaces the
    # spec.claude.model fall-through.
    spec = {
        "claude": {
            "model": "claude-3-5-sonnet-20241022",
            "provider": "mimo",
        }
    }
    # Act
    result = resolve_card_model(spec)
    # Assert
    assert result == "claude-3-5-sonnet-20241022"


def test_no_model_anywhere_returns_none():
    # Arrange — operator hasn't declared any model on the spec.
    spec = {}
    # Act
    result = resolve_card_model(spec)
    # Assert
    assert result is None


def test_unknown_provider_falls_through_to_claude_model():
    # Arrange — operator typo / unregistered name; the validator
    # surfaces the loud error separately, but the card resolution
    # must still pick the next sane chain step rather than 500.
    spec = {
        "claude": {
            "model": "fallback",
            "provider": "ghost-provider-typo",
        }
    }
    # Act
    result = resolve_card_model(spec)
    # Assert
    assert result == "fallback"
