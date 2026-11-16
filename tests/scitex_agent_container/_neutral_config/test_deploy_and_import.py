"""ADR-0033: safe deploy (backup, idempotent, stale cleanup), import round trip, CLI."""

from __future__ import annotations

import os

import pytest
from click.testing import CliRunner

from scitex_agent_container._neutral_config import (
    apply,
    check,
    hooks_equivalent,
    import_claude,
    load_layer,
    plan_claude,
)
from scitex_agent_container._neutral_config._claude import render_settings
from scitex_agent_container.cli_pkg.config_group import config_group

from .conftest import ORIGINAL_CLAUDE_SETTINGS, make_layer


def test_real_dir_in_the_way_is_moved_into_backup(deployed):
    # Arrange
    target, _, _ = deployed
    # Act
    backups = list((target / ".sac-backup").glob("*/hooks/old.sh"))
    # Assert
    assert [b.read_text() for b in backups] == ["old"]


def test_unmanaged_skill_link_is_left_alone(deployed, tmp_path):
    # Arrange
    target, _, _ = deployed
    # Act
    link = os.readlink(target / "skills" / "scitex")
    # Assert
    assert link == str(tmp_path)


def test_live_credentials_are_never_touched(deployed):
    # Arrange
    target, _, _ = deployed
    # Act
    content = (target / ".credentials.json").read_text()
    # Assert
    assert content == "secret"


def test_claude_md_reads_through_to_agents_md(deployed):
    # Arrange
    target, _, _ = deployed
    # Act
    content = (target / "CLAUDE.md").read_text()
    # Assert
    assert content == "be kind\n"


def test_second_deploy_changes_nothing(deployed):
    # Arrange
    target, actions, _ = deployed
    # Act
    states = {r.state for r in apply(target, actions)}
    # Assert
    assert states == {"ok"}


def test_check_after_deploy_reports_in_sync(deployed):
    # Arrange
    target, actions, _ = deployed
    # Act
    states = {r.state for r in check(target, actions)}
    # Assert
    assert states == {"ok"}


@pytest.fixture
def removed_skill(tmp_path):
    root = make_layer(tmp_path / "u", skills=("alpha", "beta"))
    target = tmp_path / "claude"
    apply(target, plan_claude(load_layer(root), target, "user")[0])
    (root / "skills" / "beta" / "SKILL.md").unlink()
    (root / "skills" / "beta").rmdir()
    return target, plan_claude(load_layer(root), target, "user")[0]


def test_check_flags_entry_removed_from_source_as_stale(removed_skill):
    # Arrange
    target, actions = removed_skill
    # Act
    states = [r.state for r in check(target, actions)]
    # Assert
    assert "stale" in states


def test_deploy_unlinks_entry_removed_from_source(removed_skill):
    # Arrange
    target, actions = removed_skill
    # Act
    apply(target, actions)
    # Assert
    assert not os.path.lexists(target / "skills" / "beta")


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


@pytest.fixture
def cli_args(tmp_path):
    root = make_layer(tmp_path / "u")
    return [
        "--scope",
        "user",
        "--source",
        str(root),
        "--target",
        str(tmp_path / "claude"),
    ]


def test_cli_dry_run_creates_nothing(cli_args, tmp_path):
    # Arrange
    runner = CliRunner()
    # Act
    runner.invoke(config_group, ["deploy", *cli_args, "--dry-run"])
    # Assert
    assert not (tmp_path / "claude").exists()


def test_cli_check_exits_1_before_deploy(cli_args):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(config_group, ["check", *cli_args])
    # Assert
    assert result.exit_code == 1


def test_cli_check_exits_0_after_deploy(cli_args):
    # Arrange
    runner = CliRunner()
    runner.invoke(config_group, ["deploy", *cli_args])
    # Act
    result = runner.invoke(config_group, ["check", *cli_args])
    # Assert
    assert result.exit_code == 0
