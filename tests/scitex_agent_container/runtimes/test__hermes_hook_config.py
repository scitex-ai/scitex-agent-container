"""Native hook bindings must survive model/profile regeneration."""

import pytest
import yaml

from scitex_agent_container.runtimes._hermes_hook_config import (
    apply_neutral_hooks,
    merge_neutral_hooks,
    preserve_hook_config,
)


def _write_layer(root, hooks):
    root.mkdir(parents=True, exist_ok=True)
    (root / "hooks.yaml").write_text(yaml.safe_dump({"hooks": hooks}))
    return root


def test_profile_regeneration_preserves_hooks_without_old_credentials(tmp_path):
    # Arrange: only hook policy is retained from the previous profile.
    hooks = {
        "pre_tool_call": [
            {"command": "/policy/python -m generic.dispatch", "fail_closed": True}
        ]
    }
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"model": "old", "api_key": "fake-old-key", "hooks": hooks})
    )
    generated = {"model": "new"}
    # Act
    result = preserve_hook_config(tmp_path, generated)
    # Assert
    assert result == {"model": "new", "hooks": hooks} and generated == {"model": "new"}


@pytest.mark.parametrize(
    "body", ["hooks: broken", "hooks: [invalid", "api_key: [fake-unparseable-key"]
)
def test_invalid_profile_hook_input_refuses_regeneration_without_secret_output(
    tmp_path, body
):
    # Arrange
    (tmp_path / "config.yaml").write_text(body)
    # Act
    # Assert: only the fixed, sanitized messages are permitted.
    with pytest.raises(
        ValueError,
        match=r"^(Cannot preserve Hermes hooks from unreadable configuration|Hermes hook configuration must be a mapping)$",
    ):
        preserve_hook_config(tmp_path, {})


def test_new_profile_does_not_invent_hooks(tmp_path):
    # Arrange
    generated = {"model": "current"}
    # Act
    result = preserve_hook_config(tmp_path, generated)
    # Assert
    assert result == generated


def test_merge_puts_preserved_bindings_first(tmp_path):
    # Arrange
    preserved = {"post_tool_call": [{"command": "/native/dispatch"}]}
    neutral = {"post_tool_call": [{"command": "hooks/lint.sh"}]}
    # Act
    merged = merge_neutral_hooks(preserved, neutral)
    # Assert
    assert [h["command"] for h in merged["post_tool_call"]] == [
        "/native/dispatch",
        "hooks/lint.sh",
    ]


def test_merge_skips_neutral_command_already_preserved(tmp_path):
    # Arrange
    preserved = {"post_tool_call": [{"command": "hooks/lint.sh"}]}
    neutral = {"post_tool_call": [{"command": "hooks/lint.sh"}]}
    # Act
    merged = merge_neutral_hooks(preserved, neutral)
    # Assert
    assert merged["post_tool_call"] == [{"command": "hooks/lint.sh"}]


def test_merge_with_no_neutral_keeps_preserved_mapping(tmp_path):
    # Arrange
    preserved = {"pre_tool_call": [{"command": "/native/pre"}]}
    # Act
    merged = merge_neutral_hooks(preserved, {})
    # Assert
    assert merged == preserved


def test_apply_without_layer_dir_equals_preserve(tmp_path):
    # Arrange
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"model": "old", "hooks": {"pre_tool_call": [{"command": "x"}]}})
    )
    generated = {"model": "new"}
    # Act
    result = apply_neutral_hooks(tmp_path, generated, layer_root=tmp_path / "absent")
    # Assert
    assert result == {"model": "new", "hooks": {"pre_tool_call": [{"command": "x"}]}}


def test_apply_merges_layer_hooks_under_preserved_ones(tmp_path):
    # Arrange
    (tmp_path / "config.yaml").write_text(
        yaml.safe_dump({"model": "old", "hooks": {"post_tool_call": [{"command": "/native/d"}]}})
    )
    layer = _write_layer(
        tmp_path / "layer",
        [{"name": "lint", "event": "post_tool", "command": "/abs/lint.sh"}],
    )
    # Act
    result = apply_neutral_hooks(tmp_path, {"model": "new"}, layer_root=layer)
    # Assert
    assert result["hooks"]["post_tool_call"] == [
        {"command": "/native/d"},
        {"command": "/abs/lint.sh"},
    ]


def test_apply_leaves_non_hook_keys_untouched(tmp_path):
    # Arrange
    layer = _write_layer(
        tmp_path / "layer",
        [{"name": "lint", "event": "post_tool", "command": "/abs/lint.sh"}],
    )
    # Act
    result = apply_neutral_hooks(tmp_path, {"model": "new", "k": "v"}, layer_root=layer)
    # Assert
    assert result["model"] == "new" and result["k"] == "v"


def test_apply_ignores_hooks_for_other_harnesses(tmp_path):
    # Arrange
    layer = _write_layer(
        tmp_path / "layer",
        [{"name": "c", "event": "claude:WorktreeCreate", "command": "hooks/wt.sh"}],
    )
    # Act
    result = apply_neutral_hooks(tmp_path, {"model": "new"}, layer_root=layer)
    # Assert
    assert result == {"model": "new"}
