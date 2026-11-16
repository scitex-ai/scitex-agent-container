"""ADR-0033: deploy engine — backup instead of delete, idempotence, stale cleanup."""

from __future__ import annotations

import os

import pytest

from scitex_agent_container._neutral_config import apply, check, load_layer, plan_claude

from ._layer_fixture import make_layer


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


def test_check_never_writes(user_layer, tmp_path):
    # Arrange
    target = tmp_path / "claude"
    actions, _ = plan_claude(user_layer, target, "user")
    # Act
    check(target, actions)
    # Assert
    assert not target.exists()


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
