"""Tests for the host ``~/.claude`` deep-merge (developer agents).

A FULL-DEVELOPER agent's materialized ``$HOME/.claude/{commands,skills,hooks}``
is the UNION of the host operator's ``~/.claude/{commands,skills,hooks}`` and
the ``_shared``/per-agent agent layers, with the agent layer winning on a
name collision and host-session hooks deny-listed. A capsule/solitary agent
gets the agent layers ONLY. Drift (a host file removed) fails loud.

scitex doctrine: NO mocks/monkeypatch. The host ``~/.claude`` root is pointed
at a tmp tree via the ``$SAC_HOST_CLAUDE_DIR`` seam using the project-wide
``env_save_restore`` fixture; agent layers are real files placed under the
materialized home. AAA on own lines, one assert per test.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from scitex_agent_container.config._types import AgentConfig
from scitex_agent_container.runtimes._host_merge import (
    HostMergeDriftError,
    apply_host_merge,
    assert_no_host_merge_drift,
    is_full_developer,
    plan_host_merge,
    verify_host_merge,
)

_HOST_ENV = "SAC_HOST_CLAUDE_DIR"


# ---------------------------------------------------------------------------
# Builders (real AgentConfig + real fake-host / agent-layer trees).
# ---------------------------------------------------------------------------


def _dev_config(name: str = "dev-agent") -> AgentConfig:
    """A real AgentConfig that passes the full-developer gate via role."""
    cfg = AgentConfig(name=name)
    cfg.labels = {"role": "project-maintainer"}
    return cfg


def _capsule_config(name: str = "capsule") -> AgentConfig:
    """A real AgentConfig that is NOT a developer (solitary group)."""
    cfg = AgentConfig(name=name)
    cfg.labels = {"group": "solitary", "role": "project-maintainer"}
    return cfg


def _write_host_tree(host_root: Path) -> None:
    """Stand up a fake host ~/.claude with commands, skills, and hooks.

    Includes one agent-safe hook (``enforce_ripgrep.sh``) and one
    session-only hook (``speak_on_stop.sh``) to exercise the deny-list.
    """
    (host_root / "commands").mkdir(parents=True, exist_ok=True)
    (host_root / "commands" / "where.md").write_text("# where\n")
    (host_root / "skills" / "deep-research").mkdir(parents=True, exist_ok=True)
    (host_root / "skills" / "deep-research" / "SKILL.md").write_text("# dr\n")
    hooks = host_root / "hooks" / "pre-tool-use"
    hooks.mkdir(parents=True, exist_ok=True)
    (hooks / "enforce_ripgrep.sh").write_text("#!/bin/sh\n")
    (host_root / "hooks" / "stop").mkdir(parents=True, exist_ok=True)
    (host_root / "hooks" / "stop" / "speak_on_stop.sh").write_text("#!/bin/sh\n")


def _agent_layer_file(home: Path, rel: str, body: str = "agent\n") -> Path:
    """Place a real (already-materialized) agent-layer file under the home."""
    p = home / ".claude" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)
    return p


# ---------------------------------------------------------------------------
# Gate.
# ---------------------------------------------------------------------------


class TestGate:
    def test_developer_group_is_full_developer(self):
        # Arrange
        cfg = AgentConfig(name="d")
        cfg.labels = {"group": "developer"}
        # Act
        result = is_full_developer(cfg)
        # Assert
        assert result is True

    def test_solitary_group_is_not_developer(self):
        # Arrange
        cfg = AgentConfig(name="s")
        cfg.labels = {"group": "solitary", "role": "maintainer"}
        # Act
        result = is_full_developer(cfg)
        # Assert
        assert result is False

    def test_group_unset_with_dev_role_is_developer(self):
        # Arrange
        cfg = AgentConfig(name="r")
        cfg.labels = {"role": "project-maintainer"}
        # Act
        result = is_full_developer(cfg)
        # Assert
        assert result is True

    def test_group_unset_with_non_dev_role_is_not_developer(self):
        # Arrange
        cfg = AgentConfig(name="x")
        cfg.labels = {"role": "telegrammer"}
        # Act
        result = is_full_developer(cfg)
        # Assert
        assert result is False

    def test_no_labels_is_not_developer(self):
        # Arrange
        cfg = AgentConfig(name="bare")
        # Act
        result = is_full_developer(cfg)
        # Assert
        assert result is False


# ---------------------------------------------------------------------------
# Developer agent gets host ∪ agent-layer.
# ---------------------------------------------------------------------------


class TestDeveloperGetsHostUnionAgentLayer:
    def test_host_command_is_linked_for_developer(self, tmp_path, env_save_restore):
        # Arrange
        host = tmp_path / "host_claude"
        _write_host_tree(host)
        env_save_restore.set(_HOST_ENV, str(host))
        home = tmp_path / "home"
        home.mkdir()
        # Act
        apply_host_merge(_dev_config(), home)
        # Assert
        assert (home / ".claude" / "commands" / "where.md").is_symlink()

    def test_host_command_link_target_is_absolute_host_path(
        self, tmp_path, env_save_restore
    ):
        # Arrange
        host = tmp_path / "host_claude"
        _write_host_tree(host)
        env_save_restore.set(_HOST_ENV, str(host))
        home = tmp_path / "home"
        home.mkdir()
        # Act
        apply_host_merge(_dev_config(), home)
        target = os.readlink(home / ".claude" / "commands" / "where.md")
        # Assert
        assert target == str(host / "commands" / "where.md")

    def test_host_skill_is_linked_for_developer(self, tmp_path, env_save_restore):
        # Arrange
        host = tmp_path / "host_claude"
        _write_host_tree(host)
        env_save_restore.set(_HOST_ENV, str(host))
        home = tmp_path / "home"
        home.mkdir()
        # Act
        apply_host_merge(_dev_config(), home)
        # Assert
        assert (home / ".claude" / "skills" / "deep-research" / "SKILL.md").is_symlink()

    def test_agent_layer_file_wins_over_host_collision(
        self, tmp_path, env_save_restore
    ):
        # Arrange — host AND agent layer both define commands/where.md.
        host = tmp_path / "host_claude"
        _write_host_tree(host)
        env_save_restore.set(_HOST_ENV, str(host))
        home = tmp_path / "home"
        home.mkdir()
        agent_file = _agent_layer_file(home, "commands/where.md", "AGENT WINS\n")
        # Act
        apply_host_merge(_dev_config(), home)
        # Assert — the agent-layer real file is untouched (not replaced by a link).
        assert agent_file.read_text() == "AGENT WINS\n"

    def test_agent_layer_collision_stays_a_real_file_not_a_symlink(
        self, tmp_path, env_save_restore
    ):
        # Arrange
        host = tmp_path / "host_claude"
        _write_host_tree(host)
        env_save_restore.set(_HOST_ENV, str(host))
        home = tmp_path / "home"
        home.mkdir()
        _agent_layer_file(home, "commands/where.md", "AGENT WINS\n")
        # Act
        apply_host_merge(_dev_config(), home)
        # Assert
        assert not (home / ".claude" / "commands" / "where.md").is_symlink()


# ---------------------------------------------------------------------------
# Capsule gets agent layers ONLY (no host bleed).
# ---------------------------------------------------------------------------


class TestCapsuleGetsNoHostMerge:
    def test_capsule_creates_no_host_links(self, tmp_path, env_save_restore):
        # Arrange
        host = tmp_path / "host_claude"
        _write_host_tree(host)
        env_save_restore.set(_HOST_ENV, str(host))
        home = tmp_path / "home"
        home.mkdir()
        # Act
        created = apply_host_merge(_capsule_config(), home)
        # Assert
        assert created == []

    def test_capsule_command_dir_has_no_host_command(self, tmp_path, env_save_restore):
        # Arrange
        host = tmp_path / "host_claude"
        _write_host_tree(host)
        env_save_restore.set(_HOST_ENV, str(host))
        home = tmp_path / "home"
        home.mkdir()
        # Act
        apply_host_merge(_capsule_config(), home)
        # Assert
        assert not (home / ".claude" / "commands" / "where.md").exists()


# ---------------------------------------------------------------------------
# Host-session hooks excluded; agent-safe hooks pass.
# ---------------------------------------------------------------------------


class TestHostSessionHooksExcluded:
    def test_session_only_hook_is_not_linked(self, tmp_path, env_save_restore):
        # Arrange
        host = tmp_path / "host_claude"
        _write_host_tree(host)
        env_save_restore.set(_HOST_ENV, str(host))
        home = tmp_path / "home"
        home.mkdir()
        # Act
        apply_host_merge(_dev_config(), home)
        # Assert
        assert not (home / ".claude" / "hooks" / "stop" / "speak_on_stop.sh").exists()

    def test_agent_safe_hook_is_linked(self, tmp_path, env_save_restore):
        # Arrange
        host = tmp_path / "host_claude"
        _write_host_tree(host)
        env_save_restore.set(_HOST_ENV, str(host))
        home = tmp_path / "home"
        home.mkdir()
        # Act
        apply_host_merge(_dev_config(), home)
        link = home / ".claude" / "hooks" / "pre-tool-use" / "enforce_ripgrep.sh"
        # Assert
        assert link.is_symlink()

    def test_telegram_hook_is_excluded(self, tmp_path, env_save_restore):
        # Arrange — a telegram-family host hook must never leak.
        host = tmp_path / "host_claude"
        _write_host_tree(host)
        tg = host / "hooks" / "pre-tool-use" / "limit_telegram_message_length.sh"
        tg.write_text("#!/bin/sh\n")
        env_save_restore.set(_HOST_ENV, str(host))
        home = tmp_path / "home"
        home.mkdir()
        # Act
        apply_host_merge(_dev_config(), home)
        # Assert
        assert not (home / ".claude" / "hooks" / "pre-tool-use" / tg.name).exists()

    def test_non_event_hook_subtree_is_not_linked(self, tmp_path, env_save_restore):
        # Arrange — host hooks/docs/ is documentation, not a hook event dir.
        host = tmp_path / "host_claude"
        _write_host_tree(host)
        doc = host / "hooks" / "docs" / "to_claude" / "guide.sh"
        doc.parent.mkdir(parents=True, exist_ok=True)
        doc.write_text("#!/bin/sh\n")
        env_save_restore.set(_HOST_ENV, str(host))
        home = tmp_path / "home"
        home.mkdir()
        # Act
        apply_host_merge(_dev_config(), home)
        # Assert
        assert not (
            home / ".claude" / "hooks" / "docs" / "to_claude" / "guide.sh"
        ).exists()

    def test_loose_settings_under_hooks_root_is_not_linked(
        self, tmp_path, env_save_restore
    ):
        # Arrange — a settings.json at hooks/ root is not a hook script.
        host = tmp_path / "host_claude"
        _write_host_tree(host)
        (host / "hooks" / "settings.json").write_text("{}\n")
        env_save_restore.set(_HOST_ENV, str(host))
        home = tmp_path / "home"
        home.mkdir()
        # Act
        apply_host_merge(_dev_config(), home)
        # Assert
        assert not (home / ".claude" / "hooks" / "settings.json").exists()


# ---------------------------------------------------------------------------
# Agent layer may EXCLUDE a host file.
# ---------------------------------------------------------------------------


class TestAgentLayerExcludesHostFile:
    def test_exclude_skills_drops_matching_host_skill(self, tmp_path, env_save_restore):
        # Arrange — spec excludes the deep-research skill by substring.
        host = tmp_path / "host_claude"
        _write_host_tree(host)
        env_save_restore.set(_HOST_ENV, str(host))
        home = tmp_path / "home"
        home.mkdir()
        cfg = _dev_config()
        cfg.exclude_skills = ["deep-research"]
        # Act
        apply_host_merge(cfg, home)
        # Assert
        assert not (home / ".claude" / "skills" / "deep-research" / "SKILL.md").exists()


# ---------------------------------------------------------------------------
# Drift detection — a removed host file fails loud.
# ---------------------------------------------------------------------------


class TestDriftDetection:
    def test_removed_host_file_is_reported_as_drift(self, tmp_path, env_save_restore):
        # Arrange — materialize, then delete a host file behind the link.
        host = tmp_path / "host_claude"
        _write_host_tree(host)
        env_save_restore.set(_HOST_ENV, str(host))
        home = tmp_path / "home"
        home.mkdir()
        apply_host_merge(_dev_config(), home)
        (host / "commands" / "where.md").unlink()
        # Act
        findings = verify_host_merge(_dev_config(), home)
        # Assert
        assert any("stale host-merge link" in f for f in findings)

    def test_assert_no_drift_raises_on_removed_host_file(
        self, tmp_path, env_save_restore
    ):
        # Arrange
        host = tmp_path / "host_claude"
        _write_host_tree(host)
        env_save_restore.set(_HOST_ENV, str(host))
        home = tmp_path / "home"
        home.mkdir()
        apply_host_merge(_dev_config(), home)
        (host / "commands" / "where.md").unlink()
        # Act
        ctx = pytest.raises(HostMergeDriftError)
        # Assert
        with ctx:
            assert_no_host_merge_drift(_dev_config(), home)

    def test_added_host_file_is_reported_as_missing_link(
        self, tmp_path, env_save_restore
    ):
        # Arrange — materialize, then ADD a new host command (link not yet made).
        host = tmp_path / "host_claude"
        _write_host_tree(host)
        env_save_restore.set(_HOST_ENV, str(host))
        home = tmp_path / "home"
        home.mkdir()
        apply_host_merge(_dev_config(), home)
        (host / "commands" / "newcmd.md").write_text("# new\n")
        # Act
        findings = verify_host_merge(_dev_config(), home)
        # Assert
        assert any("missing host-merge link" in f for f in findings)

    def test_reapply_self_heals_added_host_file(self, tmp_path, env_save_restore):
        # Arrange
        host = tmp_path / "host_claude"
        _write_host_tree(host)
        env_save_restore.set(_HOST_ENV, str(host))
        home = tmp_path / "home"
        home.mkdir()
        apply_host_merge(_dev_config(), home)
        (host / "commands" / "newcmd.md").write_text("# new\n")
        # Act — a fresh start re-materializes from scratch.
        apply_host_merge(_dev_config(), home)
        findings = verify_host_merge(_dev_config(), home)
        # Assert
        assert findings == []

    def test_reapply_clears_stale_link_after_host_removal(
        self, tmp_path, env_save_restore
    ):
        # Arrange — link exists, host file removed, then re-apply.
        host = tmp_path / "host_claude"
        _write_host_tree(host)
        env_save_restore.set(_HOST_ENV, str(host))
        home = tmp_path / "home"
        home.mkdir()
        apply_host_merge(_dev_config(), home)
        (host / "commands" / "where.md").unlink()
        # Act
        apply_host_merge(_dev_config(), home)
        # Assert — the now-stale link was swept.
        assert not (home / ".claude" / "commands" / "where.md").is_symlink()


# ---------------------------------------------------------------------------
# plan_host_merge purity (drift and materialize share one plan).
# ---------------------------------------------------------------------------


class TestPlanPurity:
    def test_plan_is_empty_for_capsule(self, tmp_path, env_save_restore):
        # Arrange
        host = tmp_path / "host_claude"
        _write_host_tree(host)
        env_save_restore.set(_HOST_ENV, str(host))
        home = tmp_path / "home"
        home.mkdir()
        # Act
        plan = plan_host_merge(_capsule_config(), home)
        # Assert
        assert plan == {}

    def test_plan_does_not_write_to_disk(self, tmp_path, env_save_restore):
        # Arrange
        host = tmp_path / "host_claude"
        _write_host_tree(host)
        env_save_restore.set(_HOST_ENV, str(host))
        home = tmp_path / "home"
        home.mkdir()
        # Act
        plan_host_merge(_dev_config(), home)
        # Assert — planning created nothing under the home.
        assert not (home / ".claude").exists()
