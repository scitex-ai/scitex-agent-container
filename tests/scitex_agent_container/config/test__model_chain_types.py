"""Tests for ``config._model_chain_types`` (ADR-0018 v4 schema).

The :class:`ModelLabel` dataclass is the parsed shape of one
``spec.model.<label>.*`` entry. The :data:`ModelChain` alias is the
parsed shape of the whole ``spec.model`` block (label -> ModelLabel,
insertion order = fallback order).

Each test pins exactly one observable behaviour. AAA markers (TQ002),
descriptive names (TQ003), one assert per test (TQ007).
"""

from __future__ import annotations

import pytest

from scitex_agent_container.config._model_chain_types import ModelChain, ModelLabel

# ---------------------------------------------------------------------------
# Default-construction invariants (every field has a documented "missing" sentinel)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("attr", "expected"),
    [
        ("provider", ""),
        ("model_id", ""),
        ("account", ""),
        ("api_key", ""),
    ],
)
def test_default_model_label_uses_empty_string_sentinel(attr, expected):
    # Arrange
    label = ModelLabel()
    # Act
    actual = getattr(label, attr)
    # Assert
    assert actual == expected


# ---------------------------------------------------------------------------
# Explicit-construction pass-through (every field is keyword-settable)
# ---------------------------------------------------------------------------


def test_model_label_provider_field_round_trips():
    # Arrange / Act
    label = ModelLabel(provider="anthropic")
    # Assert
    assert label.provider == "anthropic"


def test_model_label_model_id_field_round_trips():
    # Arrange / Act
    label = ModelLabel(model_id="claude-sonnet-4-6")
    # Assert
    assert label.model_id == "claude-sonnet-4-6"


def test_model_label_account_field_round_trips():
    # Arrange / Act
    label = ModelLabel(account="ywatanabe-scitex-ai")
    # Assert
    assert label.account == "ywatanabe-scitex-ai"


def test_model_label_api_key_field_round_trips():
    # Arrange / Act
    label = ModelLabel(api_key="$XIAOMI_API_KEY")
    # Assert
    assert label.api_key == "$XIAOMI_API_KEY"


# ---------------------------------------------------------------------------
# Frozen contract — labels are operator intent, immutable post-parse.
# ---------------------------------------------------------------------------


def test_model_label_is_frozen_against_mutation():
    # Arrange
    label = ModelLabel(provider="anthropic", model_id="claude-sonnet-4-6")
    # Act / Assert — dataclasses.FrozenInstanceError subclasses AttributeError.
    with pytest.raises(AttributeError):
        label.provider = "mimo"  # type: ignore[misc]


def test_model_label_is_hashable_so_dispatcher_state_can_use_sets():
    # Arrange — PR B's dispatcher tracks ``disabled-this-session``
    # state by label; sets require hash, which the frozen contract gives.
    label = ModelLabel(provider="anthropic", model_id="claude-sonnet-4-6")
    # Act / Assert
    assert hash(label) is not None


# ---------------------------------------------------------------------------
# ModelChain alias — just a dict, but its insertion order is the contract.
# ---------------------------------------------------------------------------


def test_model_chain_alias_is_assignable_to_dict_of_labels():
    # Arrange / Act — the alias is documentary; runtime is just dict.
    chain: ModelChain = {
        "primary": ModelLabel(provider="anthropic", model_id="claude-sonnet-4-6"),
        "backup": ModelLabel(provider="mimo", model_id="mimo-v2.5-pro"),
    }
    # Assert
    assert isinstance(chain, dict)


def test_model_chain_preserves_insertion_order_for_fallback_cascade():
    # Arrange — ADR-0018: dict insertion order IS the fallback cascade
    # order. Pin the Python-dict contract so the dispatcher contract
    # (PR B) cannot regress silently.
    chain: ModelChain = {}
    chain["label-1"] = ModelLabel(provider="anthropic", model_id="claude-sonnet-4-6")
    chain["label-2"] = ModelLabel(provider="mimo", model_id="mimo-v2.5-pro")
    chain["label-3"] = ModelLabel(provider="deepseek", model_id="deepseek-v4-pro")
    # Act
    ordered_keys = list(chain.keys())
    # Assert
    assert ordered_keys == ["label-1", "label-2", "label-3"]
