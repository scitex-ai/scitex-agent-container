"""Tests for ``config._parsers._model_chain.parse_model_chain`` (ADR-0018 PR A).

PR A — pure additive: existing v3 specs continue to load via the
``spec.model.legacy`` alias, AND a new v4 ``spec.model.<label>.*`` shape
parses cleanly preserving insertion order. The mutex case (both keys
present) collapses to an empty chain so the validator can surface the
loud error against the raw block; the parser is intentionally
non-raising.

AAA markers (TQ002), descriptive names (TQ003), one assert per test
(TQ007).
"""

from __future__ import annotations

import pytest

from scitex_agent_container.config._model_chain_types import ModelLabel
from scitex_agent_container.config._parsers._model_chain import (
    _reset_v3_alias_warn_tracker_for_tests,
    parse_model_chain,
)


@pytest.fixture(autouse=True)
def _reset_v3_alias_warn():
    """Clear the per-agent one-shot warn tracker before every test.

    The tracker is module-level so warns fire exactly once per agent
    per process; tests need a fresh slate to assert the warning
    consistently regardless of preceding tests.
    """
    _reset_v3_alias_warn_tracker_for_tests()
    yield
    _reset_v3_alias_warn_tracker_for_tests()


# ---------------------------------------------------------------------------
# Empty / absent paths
# ---------------------------------------------------------------------------


def test_no_model_and_no_claude_yields_empty_chain():
    # Arrange
    spec: dict = {}
    # Act
    chain = parse_model_chain(spec)
    # Assert
    assert chain == {}


def test_empty_model_dict_yields_empty_chain():
    # Arrange — ``spec.model: {}`` is operator-explicit "no chain"; the
    # validator surfaces the "declare at least one label" error.
    spec: dict = {"model": {}}
    # Act
    chain = parse_model_chain(spec)
    # Assert
    assert chain == {}


# ---------------------------------------------------------------------------
# v4 happy path — single label
# ---------------------------------------------------------------------------


def test_single_label_chain_has_one_entry():
    # Arrange
    spec = {
        "model": {
            "default": {
                "provider": "anthropic",
                "model_id": "claude-sonnet-4-6",
                "account": "ywatanabe-scitex-ai",
            }
        }
    }
    # Act
    chain = parse_model_chain(spec)
    # Assert
    assert len(chain) == 1


def test_single_label_chain_uses_operator_label_key():
    # Arrange
    spec = {
        "model": {
            "default": {
                "provider": "anthropic",
                "model_id": "claude-sonnet-4-6",
            }
        }
    }
    # Act
    chain = parse_model_chain(spec)
    # Assert
    assert list(chain.keys()) == ["default"]


def test_single_label_chain_surfaces_provider_field():
    # Arrange
    spec = {
        "model": {
            "default": {
                "provider": "anthropic",
                "model_id": "claude-sonnet-4-6",
            }
        }
    }
    # Act
    chain = parse_model_chain(spec)
    # Assert
    assert chain["default"].provider == "anthropic"


def test_single_label_chain_surfaces_model_id_field():
    # Arrange
    spec = {
        "model": {
            "default": {
                "provider": "anthropic",
                "model_id": "claude-sonnet-4-6",
            }
        }
    }
    # Act
    chain = parse_model_chain(spec)
    # Assert
    assert chain["default"].model_id == "claude-sonnet-4-6"


def test_single_label_chain_surfaces_account_field():
    # Arrange
    spec = {
        "model": {
            "default": {
                "provider": "anthropic",
                "model_id": "claude-sonnet-4-6",
                "account": "ywatanabe-scitex-ai",
            }
        }
    }
    # Act
    chain = parse_model_chain(spec)
    # Assert
    assert chain["default"].account == "ywatanabe-scitex-ai"


# ---------------------------------------------------------------------------
# v4 multi-label — insertion order preserved (= fallback cascade order).
# ---------------------------------------------------------------------------


def test_multi_label_chain_has_three_entries():
    # Arrange — operator declares primary + 2 fallbacks.
    spec = {
        "model": {
            "label-1": {"provider": "anthropic", "model_id": "claude-sonnet-4-6"},
            "label-2": {"provider": "xiaomi", "model_id": "mimo-v2.5-pro"},
            "label-3": {"provider": "deepseek", "model_id": "deepseek-v4-pro"},
        }
    }
    # Act
    chain = parse_model_chain(spec)
    # Assert
    assert len(chain) == 3


def test_multi_label_chain_preserves_insertion_order():
    # Arrange — ADR-0018: dict insertion order IS the fallback order.
    spec = {
        "model": {
            "label-1": {"provider": "anthropic", "model_id": "claude-sonnet-4-6"},
            "label-2": {"provider": "xiaomi", "model_id": "mimo-v2.5-pro"},
            "label-3": {"provider": "deepseek", "model_id": "deepseek-v4-pro"},
        }
    }
    # Act
    chain = parse_model_chain(spec)
    # Assert
    assert list(chain.keys()) == ["label-1", "label-2", "label-3"]


def test_multi_label_chain_third_label_provider_is_deepseek():
    # Arrange
    spec = {
        "model": {
            "label-1": {"provider": "anthropic", "model_id": "claude-sonnet-4-6"},
            "label-2": {"provider": "xiaomi", "model_id": "mimo-v2.5-pro"},
            "label-3": {"provider": "deepseek", "model_id": "deepseek-v4-pro"},
        }
    }
    # Act
    chain = parse_model_chain(spec)
    # Assert
    assert chain["label-3"].provider == "deepseek"


# ---------------------------------------------------------------------------
# api_key — raw value pass-through (parser does NOT resolve env vars).
# ---------------------------------------------------------------------------


def test_api_key_dollar_var_form_stored_raw():
    # Arrange
    spec = {
        "model": {
            "default": {
                "provider": "xiaomi",
                "model_id": "mimo-v2.5-pro",
                "api_key": "$XIAOMI_API_KEY",
            }
        }
    }
    # Act
    chain = parse_model_chain(spec)
    # Assert — env-var resolution lands in PR B; parser stores raw.
    assert chain["default"].api_key == "$XIAOMI_API_KEY"


def test_api_key_dollar_brace_form_stored_raw():
    # Arrange
    spec = {
        "model": {
            "default": {
                "provider": "deepseek",
                "model_id": "deepseek-v4-pro",
                "api_key": "${DEEPSEEK_API_KEY}",
            }
        }
    }
    # Act
    chain = parse_model_chain(spec)
    # Assert
    assert chain["default"].api_key == "${DEEPSEEK_API_KEY}"


def test_api_key_literal_form_stored_raw():
    # Arrange — literal secret in spec; validator emits the warning.
    spec = {
        "model": {
            "default": {
                "provider": "anthropic",
                "model_id": "claude-sonnet-4-6",
                "api_key": "sk-ant-literal",
            }
        }
    }
    # Act
    chain = parse_model_chain(spec)
    # Assert
    assert chain["default"].api_key == "sk-ant-literal"


# ---------------------------------------------------------------------------
# v3 alias — single-label "legacy" chain + one-shot stderr deprecation warning
# ---------------------------------------------------------------------------


def test_v3_string_provider_alias_creates_legacy_label():
    # Arrange — v3 string-form: provider name + model + account.
    spec = {
        "claude": {
            "provider": "deepseek",
            "model": "deepseek-chat",
            "account": "ywatanabe-scitex-ai",
        }
    }
    # Act
    chain = parse_model_chain(spec, agent_name="agent-a")
    # Assert
    assert "legacy" in chain


def test_v3_string_provider_alias_provider_matches_v3_name():
    # Arrange
    spec = {
        "claude": {"provider": "deepseek", "model": "deepseek-chat"},
    }
    # Act
    chain = parse_model_chain(spec, agent_name="agent-b")
    # Assert
    assert chain["legacy"].provider == "deepseek"


def test_v3_string_provider_alias_model_id_promoted_from_claude_model():
    # Arrange — v3 ``spec.claude.model`` maps to v4 ``model_id``.
    spec = {
        "claude": {"provider": "deepseek", "model": "deepseek-chat"},
    }
    # Act
    chain = parse_model_chain(spec, agent_name="agent-c")
    # Assert
    assert chain["legacy"].model_id == "deepseek-chat"


def test_v3_string_provider_alias_account_passes_through():
    # Arrange
    spec = {
        "claude": {
            "provider": "anthropic",
            "model": "sonnet",
            "account": "ywatanabe-scitex-ai",
        }
    }
    # Act
    chain = parse_model_chain(spec, agent_name="agent-d")
    # Assert
    assert chain["legacy"].account == "ywatanabe-scitex-ai"


def test_v3_alias_emits_deprecation_warning_to_stderr(capsys):
    # Arrange
    spec = {"claude": {"provider": "deepseek", "model": "deepseek-chat"}}
    # Act
    parse_model_chain(spec, agent_name="warn-agent")
    captured = capsys.readouterr()
    # Assert
    assert "[sac:deprecation]" in captured.err


def test_v3_alias_warning_mentions_agent_name(capsys):
    # Arrange
    spec = {"claude": {"provider": "deepseek", "model": "deepseek-chat"}}
    # Act
    parse_model_chain(spec, agent_name="my-specific-agent")
    captured = capsys.readouterr()
    # Assert
    assert "my-specific-agent" in captured.err


def test_v3_alias_warning_emits_only_once_per_agent(capsys):
    # Arrange
    spec = {"claude": {"provider": "deepseek", "model": "deepseek-chat"}}
    # Act — parse twice for the same agent.
    parse_model_chain(spec, agent_name="once-agent")
    parse_model_chain(spec, agent_name="once-agent")
    captured = capsys.readouterr()
    # Assert — one occurrence of the deprecation header in stderr.
    assert captured.err.count("[sac:deprecation]") == 1


def test_v3_alias_warning_emits_independently_per_agent(capsys):
    # Arrange — distinct agents each get their own one-shot warning.
    spec = {"claude": {"provider": "deepseek", "model": "deepseek-chat"}}
    # Act
    parse_model_chain(spec, agent_name="agent-x")
    parse_model_chain(spec, agent_name="agent-y")
    captured = capsys.readouterr()
    # Assert
    assert captured.err.count("[sac:deprecation]") == 2


# ---------------------------------------------------------------------------
# Mutex — both spec.model AND spec.claude present → parser defers to validator
# ---------------------------------------------------------------------------


def test_both_model_and_claude_present_yields_empty_chain():
    # Arrange — mutex violation; parser stays non-raising, returns
    # empty so validator can surface the named error.
    spec = {
        "model": {
            "default": {"provider": "anthropic", "model_id": "claude-sonnet-4-6"}
        },
        "claude": {"provider": "deepseek", "model": "deepseek-chat"},
    }
    # Act
    chain = parse_model_chain(spec, agent_name="mutex-agent")
    # Assert
    assert chain == {}


# ---------------------------------------------------------------------------
# Defensive — malformed per-label entries do not raise
# ---------------------------------------------------------------------------


def test_non_dict_label_entry_yields_empty_label_object():
    # Arrange — a yaml typo: label value is a scalar, not a mapping.
    spec = {"model": {"broken": "not-a-dict"}}
    # Act
    chain = parse_model_chain(spec)
    # Assert — parser does not raise; produces default ModelLabel.
    assert chain["broken"] == ModelLabel()


def test_partial_label_entry_fills_missing_fields_with_defaults():
    # Arrange — only provider declared; everything else defaults.
    spec = {"model": {"partial": {"provider": "anthropic"}}}
    # Act
    chain = parse_model_chain(spec)
    # Assert
    assert chain["partial"].model_id == ""
