"""ADR-0033: importing an existing Claude Code config into a neutral layer."""

from __future__ import annotations

import pytest

from scitex_agent_container._neutral_config import (
    hooks_equivalent,
    import_claude,
    load_layer,
)
from scitex_agent_container._neutral_config._claude import render_settings
from scitex_agent_container._neutral_config._import_claude import neutral_command

from ._layer_fixture import ORIGINAL_CLAUDE_SETTINGS


def test_import_then_render_reproduces_every_claude_hook(imported_root):
    # Arrange
    layer = load_layer(imported_root)
    # Act
    rendered = render_settings(layer, "user")["hooks"]
    # Assert
    assert hooks_equivalent(rendered, ORIGINAL_CLAUDE_SETTINGS["hooks"])


def test_import_keeps_non_neutral_claude_settings(imported_root):
    # Arrange
    layer = load_layer(imported_root)
    # Act
    status_line = render_settings(layer, "user")["statusLine"]
    # Assert
    assert status_line == ORIGINAL_CLAUDE_SETTINGS["statusLine"]


def test_import_turns_claude_md_into_agents_md(imported_root):
    # Arrange
    agents_md = imported_root / "AGENTS.md"
    # Act
    content = agents_md.read_text()
    # Assert
    assert content == "rules\n"


def test_import_rewrites_hook_paths_relative_to_layer(imported_root):
    # Arrange
    layer = load_layer(imported_root)
    # Act
    commands = {h.command for h in layer.hooks}
    # Assert
    assert "hooks/pre/a.sh" in commands


def test_import_refuses_to_overwrite_an_existing_layer(claude_dir, imported_root):
    # Arrange
    dest = imported_root
    # Act
    importing = pytest.raises(FileExistsError)
    # Assert
    with importing:
        import_claude(claude_dir, dest)


def test_import_dry_run_writes_nothing(claude_dir, tmp_path):
    # Arrange
    dest = tmp_path / "preview"
    # Act
    import_claude(claude_dir, dest, dry_run=True)
    # Assert
    assert not dest.exists()


def test_absolute_home_hook_path_becomes_layer_relative():
    # Arrange
    command = "/home/someone/.claude/hooks/stop/x.sh"
    # Act
    neutral = neutral_command(command)
    # Assert
    assert neutral == "hooks/stop/x.sh"


def test_non_hook_command_is_kept_verbatim():
    # Arrange
    command = "rtk hook claude"
    # Act
    neutral = neutral_command(command)
    # Assert
    assert neutral == command
