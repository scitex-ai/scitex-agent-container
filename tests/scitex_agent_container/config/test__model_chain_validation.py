"""Tests for ``config._model_chain_validation.validate_model_chain`` (ADR-0018 PR A).

Returns a list of error strings (empty = clean). Soft warnings are
prefixed with ``[warn]``. Errors are operator-targeted — naming the
offending label, field, and (when applicable) closed set of valid
values.

AAA markers (TQ002), descriptive names (TQ003), one assert per test
(TQ007).
"""

from __future__ import annotations

from scitex_agent_container.config._model_chain_validation import validate_model_chain

# ---------------------------------------------------------------------------
# Absent / empty / non-dict
# ---------------------------------------------------------------------------


def test_absent_model_block_yields_no_errors():
    # Arrange / Act — None means key not present in spec.
    errors = validate_model_chain(None)
    # Assert
    assert errors == []


def test_non_dict_model_block_yields_typed_error():
    # Arrange / Act
    errors = validate_model_chain("garbage")
    # Assert
    assert any("spec.model must be a dict" in e for e in errors)


def test_empty_dict_model_block_yields_declare_at_least_one_label_error():
    # Arrange / Act
    errors = validate_model_chain({})
    # Assert
    assert any("at least one label" in e for e in errors)


# ---------------------------------------------------------------------------
# provider — required, must be registered.
# ---------------------------------------------------------------------------


def test_missing_provider_yields_required_field_error():
    # Arrange
    block = {"default": {"model_id": "claude-sonnet-4-6"}}
    # Act
    errors = validate_model_chain(block)
    # Assert
    assert any("spec.model.default.provider is required" in e for e in errors)


def test_empty_string_provider_yields_required_field_error():
    # Arrange
    block = {"default": {"provider": "", "model_id": "claude-sonnet-4-6"}}
    # Act
    errors = validate_model_chain(block)
    # Assert
    assert any("spec.model.default.provider is required" in e for e in errors)


def test_unknown_provider_yields_known_providers_list():
    # Arrange — operator typo'd or invented a provider name.
    block = {"default": {"provider": "myprovider", "model_id": "x"}}
    # Act
    errors = validate_model_chain(block)
    # Assert
    assert any(
        "is not a registered provider name" in e and "Known providers" in e
        for e in errors
    )


def test_known_provider_yields_no_provider_error():
    # Arrange — anthropic is registered.
    block = {"default": {"provider": "anthropic", "model_id": "claude-sonnet-4-6"}}
    # Act
    errors = validate_model_chain(block)
    # Assert
    assert not any("provider" in e and "not a registered" in e for e in errors)


# ---------------------------------------------------------------------------
# model_id — required, non-empty.
# ---------------------------------------------------------------------------


def test_missing_model_id_yields_required_field_error():
    # Arrange
    block = {"default": {"provider": "anthropic"}}
    # Act
    errors = validate_model_chain(block)
    # Assert
    assert any("spec.model.default.model_id is required" in e for e in errors)


def test_empty_string_model_id_yields_required_field_error():
    # Arrange
    block = {"default": {"provider": "anthropic", "model_id": ""}}
    # Act
    errors = validate_model_chain(block)
    # Assert
    assert any("spec.model.default.model_id is required" in e for e in errors)


def test_non_string_model_id_yields_required_field_error():
    # Arrange — yaml ``model_id: 42`` (int).
    block = {"default": {"provider": "anthropic", "model_id": 42}}
    # Act
    errors = validate_model_chain(block)
    # Assert
    assert any("spec.model.default.model_id is required" in e for e in errors)


# ---------------------------------------------------------------------------
# account XOR api_key — mutual exclusion.
# ---------------------------------------------------------------------------


def test_both_account_and_api_key_set_yields_mutex_error():
    # Arrange
    block = {
        "default": {
            "provider": "anthropic",
            "model_id": "claude-sonnet-4-6",
            "account": "ywatanabe-scitex-ai",
            "api_key": "$ANTHROPIC_API_KEY",
        }
    }
    # Act
    errors = validate_model_chain(block)
    # Assert
    assert any(
        "spec.model.default.account" in e
        and "spec.model.default.api_key" in e
        and "mutually exclusive" in e
        for e in errors
    )


def test_neither_account_nor_api_key_set_yields_no_mutex_error():
    # Arrange — both omitted; falls back to provider registry's
    # auth_token_env (PR #244 path). Validator is clean on this axis.
    block = {
        "default": {
            "provider": "deepseek",
            "model_id": "deepseek-v4-pro",
        }
    }
    # Act
    errors = validate_model_chain(block)
    # Assert
    assert not any("mutually exclusive" in e for e in errors)


# ---------------------------------------------------------------------------
# Soft warnings — account on non-Anthropic, literal api_key.
# ---------------------------------------------------------------------------


def test_account_on_non_anthropic_provider_yields_soft_warning():
    # Arrange — operator copy-pasted account field from a v3 anthropic
    # spec; deepseek doesn't use OAuth.
    block = {
        "default": {
            "provider": "deepseek",
            "model_id": "deepseek-v4-pro",
            "account": "ywatanabe-scitex-ai",
        }
    }
    # Act
    errors = validate_model_chain(block)
    # Assert
    assert any(
        e.startswith("[warn]") and "account is set on a non-Anthropic" in e
        for e in errors
    )


def test_account_on_anthropic_provider_yields_no_warning():
    # Arrange — the intended use case; no warning.
    block = {
        "default": {
            "provider": "anthropic",
            "model_id": "claude-sonnet-4-6",
            "account": "ywatanabe-scitex-ai",
        }
    }
    # Act
    errors = validate_model_chain(block)
    # Assert
    assert not any("account is set on a non-Anthropic" in e for e in errors)


def test_literal_api_key_yields_soft_secret_warning():
    # Arrange — operator pasted secret value into spec.yaml.
    block = {
        "default": {
            "provider": "anthropic",
            "model_id": "claude-sonnet-4-6",
            "api_key": "sk-ant-literal-paste",
        }
    }
    # Act
    errors = validate_model_chain(block)
    # Assert
    assert any(
        e.startswith("[warn]") and "secrets in spec.yaml is anti-pattern" in e
        for e in errors
    )


def test_env_ref_api_key_yields_no_secret_warning():
    # Arrange — env var ref form; no warning.
    block = {
        "default": {
            "provider": "anthropic",
            "model_id": "claude-sonnet-4-6",
            "api_key": "$ANTHROPIC_API_KEY",
        }
    }
    # Act
    errors = validate_model_chain(block)
    # Assert
    assert not any("secrets in spec.yaml" in e for e in errors)


def test_dollar_brace_api_key_yields_no_secret_warning():
    # Arrange — ${VAR} form is also valid.
    block = {
        "default": {
            "provider": "anthropic",
            "model_id": "claude-sonnet-4-6",
            "api_key": "${ANTHROPIC_API_KEY}",
        }
    }
    # Act
    errors = validate_model_chain(block)
    # Assert
    assert not any("secrets in spec.yaml" in e for e in errors)


# ---------------------------------------------------------------------------
# Multi-label — errors carry the offending label name.
# ---------------------------------------------------------------------------


def test_multi_label_errors_name_offending_label():
    # Arrange — label-1 clean, label-2 missing model_id.
    block = {
        "label-1": {"provider": "anthropic", "model_id": "claude-sonnet-4-6"},
        "label-2": {"provider": "xiaomi"},
    }
    # Act
    errors = validate_model_chain(block)
    # Assert
    assert any("spec.model.label-2.model_id" in e for e in errors)


def test_multi_label_clean_chain_validates_with_no_errors():
    # Arrange — three valid labels, no warnings.
    block = {
        "label-1": {"provider": "anthropic", "model_id": "claude-sonnet-4-6"},
        "label-2": {"provider": "xiaomi", "model_id": "mimo-v2.5-pro"},
        "label-3": {"provider": "deepseek", "model_id": "deepseek-v4-pro"},
    }
    # Act
    errors = validate_model_chain(block)
    # Assert
    assert errors == []


def test_non_dict_label_entry_yields_typed_error():
    # Arrange — yaml typo: label value is a string.
    block = {"broken": "not-a-dict"}
    # Act
    errors = validate_model_chain(block)
    # Assert
    assert any("spec.model.broken must be a mapping" in e for e in errors)


# ---------------------------------------------------------------------------
# Label-ordered iteration — validator visits labels in insertion order.
# ---------------------------------------------------------------------------


def test_label_iteration_preserves_insertion_order_in_error_list():
    # Arrange — label-1 broken, label-2 broken; label-1's error must
    # appear before label-2's in the returned list.
    block = {
        "label-1": {},  # missing both provider and model_id
        "label-2": {},
    }
    # Act
    errors = validate_model_chain(block)
    # Filter to just the per-label provider errors so we know the order.
    provider_errors = [e for e in errors if "provider is required" in e]
    # Assert
    assert provider_errors[0].startswith("spec.model.label-1.")


def test_label_iteration_second_label_provider_error_appears_second():
    # Arrange
    block = {
        "label-1": {},
        "label-2": {},
    }
    # Act
    errors = validate_model_chain(block)
    provider_errors = [e for e in errors if "provider is required" in e]
    # Assert
    assert provider_errors[1].startswith("spec.model.label-2.")
