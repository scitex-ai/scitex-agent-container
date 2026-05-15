"""Behavioural tests for ``config._parsers._helpers``.

Covers the four public surfaces of the module:

- ``HOOK_KEYS`` — the recognised lifecycle hook names.
- ``MODEL_DISPLAY_NAMES`` — alias-to-display-name map.
- ``get_nested`` — dotted-path dict accessor with a default.
- ``interpolate_metadata`` — ``${metadata.*}`` placeholder substitution.
- ``_parse_command_list`` — coerces a YAML command list into typed
  ``Command(delay, command)`` records.

Each test is single-assertion and AAA-marked so a failing CI line maps
directly to one behavioural contract.
"""

from __future__ import annotations

import importlib

import pytest

from scitex_agent_container.config._parsers._helpers import (
    HOOK_KEYS,
    MODEL_DISPLAY_NAMES,
    _parse_command_list,
    get_nested,
    interpolate_metadata,
)

# ---------------------------------------------------------------------------
# Module import
# ---------------------------------------------------------------------------


def test_helpers_module_imports_without_side_effects():
    # Arrange
    module_name = "scitex_agent_container.config._parsers._helpers"
    # Act
    module = importlib.import_module(module_name)
    # Assert
    assert module.__name__ == module_name


# ---------------------------------------------------------------------------
# HOOK_KEYS — parametrize the known lifecycle keys
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "hook_key",
    [
        pytest.param("pre_start", id="pre_start"),
        pytest.param("post_stop", id="post_stop"),
        pytest.param("on_compact", id="on_compact"),
    ],
)
def test_hook_keys_contains_known_lifecycle_hook(hook_key):
    # Arrange
    keys = HOOK_KEYS
    # Act
    present = hook_key in keys
    # Assert
    assert present is True


# ---------------------------------------------------------------------------
# MODEL_DISPLAY_NAMES
# ---------------------------------------------------------------------------


def test_model_display_names_resolves_opus_1m_alias_to_label():
    # Arrange
    alias = "opus[1m]"
    # Act
    label = MODEL_DISPLAY_NAMES[alias]
    # Assert
    assert label == "Claude Opus (1M)"


def test_model_display_names_includes_haiku_alias_key():
    # Arrange
    aliases = MODEL_DISPLAY_NAMES
    # Act
    present = "haiku" in aliases
    # Assert
    assert present is True


# ---------------------------------------------------------------------------
# get_nested
# ---------------------------------------------------------------------------


def test_get_nested_returns_value_for_simple_top_level_key():
    # Arrange
    data = {"a": 1}
    # Act
    result = get_nested(data, "a")
    # Assert
    assert result == 1


def test_get_nested_walks_dotted_path_through_nested_dicts():
    # Arrange
    data = {"a": {"b": {"c": "deep"}}}
    # Act
    result = get_nested(data, "a.b.c")
    # Assert
    assert result == "deep"


def test_get_nested_returns_default_when_intermediate_key_missing():
    # Arrange
    data = {"a": {}}
    # Act
    result = get_nested(data, "a.b.c", default="X")
    # Assert
    assert result == "X"


def test_get_nested_returns_none_when_default_not_provided():
    # Arrange
    data: dict = {}
    # Act
    result = get_nested(data, "x.y")
    # Assert
    assert result is None


def test_get_nested_returns_default_when_intermediate_value_not_dict():
    # Arrange
    data = {"a": "string"}
    # Act
    result = get_nested(data, "a.b", default="d")
    # Assert
    assert result == "d"


# ---------------------------------------------------------------------------
# interpolate_metadata
# ---------------------------------------------------------------------------


def test_interpolate_metadata_substitutes_name_placeholder_with_value():
    # Arrange
    template = "hello ${metadata.name}"
    metadata = {"name": "agent-7"}
    # Act
    out = interpolate_metadata(template, metadata)
    # Assert
    assert out == "hello agent-7"


def test_interpolate_metadata_substitutes_label_placeholder_with_value():
    # Arrange
    template = "tier=${metadata.labels.tier}"
    metadata = {"labels": {"tier": "edge"}}
    # Act
    out = interpolate_metadata(template, metadata)
    # Assert
    assert out == "tier=edge"


def test_interpolate_metadata_keeps_name_placeholder_when_name_missing():
    # Arrange
    template = "x=${metadata.name}"
    metadata: dict = {}
    # Act
    out = interpolate_metadata(template, metadata)
    # Assert
    assert out == "x=${metadata.name}"


def test_interpolate_metadata_keeps_label_placeholder_when_label_missing():
    # Arrange
    template = "x=${metadata.labels.zz}"
    metadata = {"labels": {}}
    # Act
    out = interpolate_metadata(template, metadata)
    # Assert
    assert out == "x=${metadata.labels.zz}"


def test_interpolate_metadata_keeps_label_placeholder_when_labels_field_is_none():
    # Arrange
    template = "x=${metadata.labels.zz}"
    metadata = {"labels": None}
    # Act
    out = interpolate_metadata(template, metadata)
    # Assert
    assert out == "x=${metadata.labels.zz}"


def test_interpolate_metadata_leaves_unknown_metadata_key_intact():
    # Arrange
    template = "${metadata.other.thing}"
    metadata = {"name": "a"}
    # Act
    out = interpolate_metadata(template, metadata)
    # Assert
    assert out == "${metadata.other.thing}"


def test_interpolate_metadata_returns_string_unchanged_when_no_placeholders():
    # Arrange
    template = "nothing here"
    metadata = {"name": "x"}
    # Act
    out = interpolate_metadata(template, metadata)
    # Assert
    assert out == "nothing here"


def test_interpolate_metadata_substitutes_multiple_placeholders_in_one_pass():
    # Arrange
    template = "n=${metadata.name} t=${metadata.labels.tier}"
    metadata = {"name": "alpha", "labels": {"tier": "core"}}
    # Act
    out = interpolate_metadata(template, metadata)
    # Assert
    assert out == "n=alpha t=core"


# ---------------------------------------------------------------------------
# _parse_command_list
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(None, id="none"),
        pytest.param([], id="empty-list"),
    ],
)
def test_parse_command_list_returns_empty_for_falsy_input(raw):
    # Arrange
    payload = raw
    # Act
    result = _parse_command_list(payload)
    # Assert
    assert result == []


def test_parse_command_list_converts_strings_to_zero_delay_records():
    # Arrange
    payload = ["echo a", "echo b"]
    # Act
    out = _parse_command_list(payload)
    # Assert
    assert [(c.delay, c.command) for c in out] == [(0, "echo a"), (0, "echo b")]


def test_parse_command_list_skips_empty_string_entries():
    # Arrange
    payload = ["", "echo go"]
    # Act
    out = _parse_command_list(payload)
    # Assert
    assert [c.command for c in out] == ["echo go"]


def test_parse_command_list_preserves_delay_from_dict_entry():
    # Arrange
    payload = [{"command": "x", "delay": 7}]
    # Act
    out = _parse_command_list(payload)
    # Assert
    assert out[0].delay == 7


def test_parse_command_list_preserves_command_text_from_dict_entry():
    # Arrange
    payload = [{"command": "x", "delay": 7}]
    # Act
    out = _parse_command_list(payload)
    # Assert
    assert out[0].command == "x"


def test_parse_command_list_falls_back_to_zero_delay_when_delay_invalid():
    # Arrange
    payload = [{"command": "x", "delay": "junk"}]
    # Act
    out = _parse_command_list(payload)
    # Assert
    assert out[0].delay == 0


def test_parse_command_list_skips_dict_entry_missing_command_field():
    # Arrange
    payload = [{"delay": 5}, {"command": "ok"}]
    # Act
    out = _parse_command_list(payload)
    # Assert
    assert [c.command for c in out] == ["ok"]


def test_parse_command_list_skips_entries_that_are_neither_str_nor_dict():
    # Arrange
    payload = ["echo", 42, None, {"command": "go"}]
    # Act
    out = _parse_command_list(payload)
    # Assert
    assert [c.command for c in out] == ["echo", "go"]
