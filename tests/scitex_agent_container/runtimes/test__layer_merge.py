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


def test_comment_key_conflict_is_exempt_and_keeps_first() -> None:
    # Arrange — two layers each document themselves via ``_comment``.
    layers = [
        ("user", {"_comment": "user layer"}),
        ("agent", {"_comment": "agent layer"}),
    ]
    # Act
    merged, _ = deep_merge_layers(layers)
    # Assert
    assert merged["_comment"] == "user layer"


def test_comment_prefixed_key_conflict_is_exempt() -> None:
    # Arrange — ``_comment_*`` documentation keys are also exempt.
    layers = [("user", {"_comment_note": "a"}), ("agent", {"_comment_note": "b"})]
    # Act
    merged, _ = deep_merge_layers(layers)
    # Assert
    assert merged["_comment_note"] == "a"


def test_comment_conflict_does_not_hard_fail_the_cascade() -> None:
    # Arrange — a doc-key clash must not abort the surrounding real merge.
    layers = [
        ("user", {"_comment": "x", "k": 1}),
        ("agent", {"_comment": "y", "j": 2}),
    ]
    # Act
    merged, _ = deep_merge_layers(layers)
    # Assert
    assert merged == {"_comment": "x", "k": 1, "j": 2}


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


def test_lower_layer_hook_names_the_lower_layer() -> None:
    # Arrange — the additively-merged case: two layers, one guard each.
    grp_a = {"matcher": "Bash", "hooks": [{"type": "command", "command": "a"}]}
    grp_b = {"matcher": "Bash", "hooks": [{"type": "command", "command": "b"}]}
    layers = [
        ("user", {"hooks": {"PreToolUse": [grp_a]}}),
        ("agent", {"hooks": {"PreToolUse": [grp_b]}}),
    ]
    # Act
    _, prov = deep_merge_layers(layers)
    # Assert
    assert prov["hooks.PreToolUse.a"] == "user"


def test_higher_layer_hook_names_the_higher_layer() -> None:
    # Arrange — the same cascade, asserting the overlay's own guard.
    grp_a = {"matcher": "Bash", "hooks": [{"type": "command", "command": "a"}]}
    grp_b = {"matcher": "Bash", "hooks": [{"type": "command", "command": "b"}]}
    layers = [
        ("user", {"hooks": {"PreToolUse": [grp_a]}}),
        ("agent", {"hooks": {"PreToolUse": [grp_b]}}),
    ]
    # Act
    _, prov = deep_merge_layers(layers)
    # Assert
    assert prov["hooks.PreToolUse.b"] == "agent"


def test_hooks_provenance_is_never_the_merged_sentinel() -> None:
    # Arrange — the regression: a merged block used to report "(merged)",
    # which names no layer and so cannot answer "who armed this hook?".
    grp_a = {"matcher": "Bash", "hooks": [{"type": "command", "command": "a"}]}
    grp_b = {"matcher": "Bash", "hooks": [{"type": "command", "command": "b"}]}
    layers = [
        ("user", {"hooks": {"PreToolUse": [grp_a]}}),
        ("agent", {"hooks": {"PreToolUse": [grp_b]}}),
    ]
    # Act
    _, prov = deep_merge_layers(layers)
    # Assert — the bare "hooks" key must be checked too: the regression wrote
    # the sentinel there, and a "hooks."-prefixed filter alone silently misses
    # it, which makes this test pass with the fix reverted.
    owners = {v for k, v in prov.items() if k == "hooks" or k.startswith("hooks.")}
    assert "(merged)" not in owners


def test_repeated_hook_command_stays_owned_by_the_lower_layer() -> None:
    # Arrange — the same guard shipped by both layers has ONE origin.
    grp = {"matcher": "Bash", "hooks": [{"type": "command", "command": "guard"}]}
    layers = [
        ("user", {"hooks": {"PreToolUse": [grp]}}),
        ("agent", {"hooks": {"PreToolUse": [grp]}}),
    ]
    # Act
    _, prov = deep_merge_layers(layers)
    # Assert
    assert prov["hooks.PreToolUse.guard"] == "user"


def test_same_command_on_another_event_keeps_its_own_owner() -> None:
    # Arrange — identical command text armed on two DIFFERENT events must not
    # collapse onto one provenance key and mis-attribute the second event.
    pre = {"matcher": "", "hooks": [{"type": "command", "command": "x"}]}
    stop = {"matcher": "", "hooks": [{"type": "command", "command": "x"}]}
    layers = [
        ("user", {"hooks": {"PreToolUse": [pre]}}),
        ("agent", {"hooks": {"Stop": [stop]}}),
    ]
    # Act
    _, prov = deep_merge_layers(layers)
    # Assert
    assert prov["hooks.Stop.x"] == "agent"


def test_malformed_hook_entries_do_not_break_provenance() -> None:
    # Arrange — a hand-edited layer with junk where groups/hooks should be.
    layers = [
        ("user", {"hooks": {"PreToolUse": "not-a-list", "Stop": ["not-a-dict"]}}),
        (
            "agent",
            {
                "hooks": {
                    "Stop": [
                        {"matcher": "", "hooks": [{"type": "command"}]},
                        {"matcher": "", "hooks": [{"command": "real"}]},
                    ]
                }
            },
        ),
    ]
    # Act
    _, prov = deep_merge_layers(layers)
    # Assert
    assert prov["hooks.Stop.real"] == "agent"
