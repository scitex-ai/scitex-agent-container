"""ADR-0033: loading a neutral layer and merging user + project layers."""

from __future__ import annotations

import pytest

from scitex_agent_container._neutral_config import (
    NeutralConfigError,
    load_layer,
    merge_layers,
)

from ._layer_fixture import make_layer


def test_load_layer_finds_agents_md(user_layer):
    # Arrange
    layer = user_layer
    # Act
    name = layer.agents_md.name
    # Assert
    assert name == "AGENTS.md"


def test_load_layer_lists_extra_docs_but_not_agents_md(user_layer):
    # Arrange
    layer = user_layer
    # Act
    docs = set(layer.docs)
    # Assert
    assert docs == {"RTK.md"}


def test_load_layer_reads_hook_events_in_order(user_layer):
    # Arrange
    layer = user_layer
    # Act
    events = [h.event for h in layer.hooks]
    # Assert
    assert events == ["pre_tool", "claude:WorktreeCreate", "codex:Weird"]


def test_unknown_neutral_event_is_rejected_with_file_and_index(tmp_path):
    # Arrange
    root = make_layer(
        tmp_path / "u", hooks=[{"name": "x", "event": "pretool", "command": "c"}]
    )
    # Act
    loading = pytest.raises(
        NeutralConfigError, match=r"hooks\.yaml: hooks\[0\] event 'pretool'"
    )
    # Assert
    with loading:
        load_layer(root)


def test_duplicate_hook_name_is_rejected(tmp_path):
    # Arrange
    hook = {"name": "x", "event": "stop", "command": "c"}
    root = make_layer(tmp_path / "u", hooks=[hook, hook])
    # Act
    loading = pytest.raises(NeutralConfigError, match="duplicate hook name 'x'")
    # Assert
    with loading:
        load_layer(root)


def test_missing_layer_dir_is_an_empty_layer(tmp_path):
    # Arrange
    root = tmp_path / "absent"
    # Act
    layer = load_layer(root)
    # Assert
    assert (layer.agents_md, layer.hooks, layer.skills) == (None, [], {})


@pytest.fixture
def merged(tmp_path):
    user = make_layer(
        tmp_path / "u",
        skills=("alpha", "beta"),
        settings={"model": "opus", "permissions": {"deny": ["Bash(rm -rf /)"]}},
    )
    project = make_layer(
        tmp_path / "p",
        skills=("alpha",),
        hooks=[],
        disable=["wt"],
        settings={"model": "haiku", "permissions": {"allow": ["Bash(make)"]}},
    )
    return merge_layers(load_layer(user), load_layer(project))


def test_project_skill_replaces_user_skill_of_same_name(merged, tmp_path):
    # Arrange
    expected = tmp_path / "p" / "skills" / "alpha"
    # Act
    actual = merged.skills["alpha"]
    # Assert
    assert actual == expected


def test_user_skill_absent_from_project_is_kept(merged, tmp_path):
    # Arrange
    expected = tmp_path / "u" / "skills" / "beta"
    # Act
    actual = merged.skills["beta"]
    # Assert
    assert actual == expected


def test_project_setting_wins(merged):
    # Arrange
    settings = merged.settings
    # Act
    model = settings["model"]
    # Assert
    assert model == "haiku"


def test_user_deny_rule_survives_project_settings(merged):
    # Arrange
    settings = merged.settings
    # Act
    permissions = settings["permissions"]
    # Assert
    assert permissions == {"deny": ["Bash(rm -rf /)"], "allow": ["Bash(make)"]}


def test_project_disables_user_hook_only_by_name(merged):
    # Arrange
    hooks = merged.hooks
    # Act
    names = [h.name for h in hooks]
    # Assert
    assert names == ["guard", "cx"]
