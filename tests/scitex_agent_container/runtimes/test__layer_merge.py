"""Tests for the layered to_home config deep-merge (ADR-0018).

Covers ``_layer_merge.deep_merge_layers``: the deterministic cascade
deep-merge used to assemble an agent's effective ``.claude/settings.json``
/ ``.mcp.json`` from user-``_shared`` → project-``_shared`` → per-agent
layers. Verifies additive merges (dict / list / hooks), idempotent equal
scalars, provenance tracking, and the SSOT raise-on-scalar-conflict.

STX-NM002: no mocks / monkeypatch — pure-function inputs/outputs.
STX-TQ002 / TQ007: AAA markers per test, one fact per test.
"""

from __future__ import annotations

import pytest

from scitex_agent_container.runtimes._layer_merge import deep_merge_layers
from scitex_agent_container.runtimes._to_home_errors import LayerMergeConflict


def test_disjoint_keys_union() -> None:
    # Arrange
    layers = [("user", {"a": 1}), ("agent", {"b": 2})]
    # Act
    merged, _ = deep_merge_layers(layers)
    # Assert
    assert merged == {"a": 1, "b": 2}


def test_provenance_maps_leaf_to_owning_layer() -> None:
    # Arrange
    layers = [("user", {"a": 1}), ("agent", {"b": 2})]
    # Act
    _, prov = deep_merge_layers(layers)
    # Assert
    assert prov == {"a": "user", "b": "agent"}


def test_equal_scalar_is_idempotent() -> None:
    # Arrange
    layers = [("user", {"k": "v"}), ("agent", {"k": "v"})]
    # Act
    merged, _ = deep_merge_layers(layers)
    # Assert
    assert merged == {"k": "v"}


def test_conflicting_scalar_raises() -> None:
    # Arrange
    layers = [("user", {"k": "a"}), ("agent", {"k": "b"})]
    # Act
    # Assert
    with pytest.raises(LayerMergeConflict):
        deep_merge_layers(layers)


def test_conflict_message_names_conflicting_key() -> None:
    # Arrange
    layers = [("user", {"statusLine": "x"}), ("agent", {"statusLine": "y"})]
    # Act
    # Assert
    with pytest.raises(LayerMergeConflict, match="statusLine"):
        deep_merge_layers(layers)


def test_nested_dicts_merge_recursively() -> None:
    # Arrange
    layers = [("user", {"d": {"a": 1}}), ("agent", {"d": {"b": 2}})]
    # Act
    merged, _ = deep_merge_layers(layers)
    # Assert
    assert merged == {"d": {"a": 1, "b": 2}}


def test_lists_union_preserving_order() -> None:
    # Arrange
    layers = [("user", {"xs": [1, 2]}), ("agent", {"xs": [2, 3]})]
    # Act
    merged, _ = deep_merge_layers(layers)
    # Assert
    assert merged["xs"] == [1, 2, 3]


def test_hooks_block_is_additive_per_event() -> None:
    # Arrange
    grp_a = {"matcher": "Bash", "hooks": [{"type": "command", "command": "a"}]}
    grp_b = {"matcher": "Bash", "hooks": [{"type": "command", "command": "b"}]}
    layers = [
        ("user", {"hooks": {"PreToolUse": [grp_a]}}),
        ("agent", {"hooks": {"PreToolUse": [grp_b]}}),
    ]
    # Act
    merged, _ = deep_merge_layers(layers)
    # Assert
    assert merged["hooks"]["PreToolUse"] == [grp_a, grp_b]
