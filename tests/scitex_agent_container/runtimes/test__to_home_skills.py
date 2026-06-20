"""Tests for deploy-time skills→CLAUDE.md aggregation (handoff item 5).

The agent's resolved skills (the real ``*.md`` files materialized under
``<workspace_home>/.claude/skills/``) are inlined into CLAUDE.md between
idempotent ``sac:skills`` markers so they are always in context — the
operator's "guarantee" against unreliable ``@``-import / skill-chaining.

No mocks: real files on ``tmp_path`` and the real ``materialize_to_home`` /
``deploy_to_home`` entrypoints (PA-306). AAA markers throughout.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container.config._types import AgentConfig
from scitex_agent_container.runtimes._to_home import (
    deploy_to_home,
    materialize_to_home,
)
from scitex_agent_container.runtimes._to_home_skills import (
    SKILLS_END_MARKER,
    SKILLS_START_MARKER,
    aggregate_skills_into_claudemd,
)

# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _home_with_skills(
    tmp_path: Path,
    skills: dict[str, str],
    *,
    claude_md: str | None = None,
) -> Path:
    """Build a workspace home with ``.claude/skills/<name>`` files.

    ``skills`` maps a relative path (under ``.claude/skills/``) to its body.
    ``claude_md`` (when given) seeds ``<home>/CLAUDE.md``.
    """
    home = tmp_path / "home"
    skills_dir = home / ".claude" / "skills"
    skills_dir.mkdir(parents=True, exist_ok=True)
    for rel, body in skills.items():
        leaf = skills_dir / rel
        leaf.parent.mkdir(parents=True, exist_ok=True)
        leaf.write_text(body, encoding="utf-8")
    if claude_md is not None:
        (home / "CLAUDE.md").write_text(claude_md, encoding="utf-8")
    return home


# ---------------------------------------------------------------------------
# aggregate_skills_into_claudemd — core behaviour
# ---------------------------------------------------------------------------


class TestAggregateBasics:
    def test_inlines_skill_body(self, tmp_path):
        # Arrange
        home = _home_with_skills(
            tmp_path, {"s1.md": "# S1\nskill one body"}, claude_md="# Agent\n"
        )
        # Act
        aggregate_skills_into_claudemd(home)
        # Assert
        assert "skill one body" in (home / "CLAUDE.md").read_text()

    def test_emits_start_marker(self, tmp_path):
        # Arrange
        home = _home_with_skills(tmp_path, {"s1.md": "body"}, claude_md="# Agent\n")
        # Act
        aggregate_skills_into_claudemd(home)
        # Assert
        assert SKILLS_START_MARKER in (home / "CLAUDE.md").read_text()

    def test_emits_end_marker(self, tmp_path):
        # Arrange
        home = _home_with_skills(tmp_path, {"s1.md": "body"}, claude_md="# Agent\n")
        # Act
        aggregate_skills_into_claudemd(home)
        # Assert
        assert SKILLS_END_MARKER in (home / "CLAUDE.md").read_text()

    def test_preserves_existing_claude_md_content(self, tmp_path):
        # Arrange
        home = _home_with_skills(
            tmp_path, {"s1.md": "body"}, claude_md="# Agent\nmission text\n"
        )
        # Act
        aggregate_skills_into_claudemd(home)
        # Assert
        assert "mission text" in (home / "CLAUDE.md").read_text()

    def test_inlines_multiple_skill_bodies(self, tmp_path):
        # Arrange
        home = _home_with_skills(
            tmp_path,
            {"a.md": "alpha body", "b.md": "beta body"},
            claude_md="# Agent\n",
        )
        # Act
        aggregate_skills_into_claudemd(home)
        text = (home / "CLAUDE.md").read_text()
        # Assert
        assert "alpha body" in text and "beta body" in text

    def test_includes_nested_skill(self, tmp_path):
        # Arrange
        home = _home_with_skills(
            tmp_path,
            {"sub/nested.md": "nested body"},
            claude_md="# Agent\n",
        )
        # Act
        aggregate_skills_into_claudemd(home)
        # Assert
        assert "nested body" in (home / "CLAUDE.md").read_text()

    def test_returns_written_path(self, tmp_path):
        # Arrange
        home = _home_with_skills(tmp_path, {"s1.md": "body"}, claude_md="# Agent\n")
        # Act
        written = aggregate_skills_into_claudemd(home)
        # Assert
        assert home / "CLAUDE.md" in written


class TestAggregateNoSkills:
    def test_no_skills_no_claude_md_is_noop(self, tmp_path):
        # Arrange
        home = tmp_path / "home"
        (home / ".claude" / "skills").mkdir(parents=True)
        # Act
        written = aggregate_skills_into_claudemd(home)
        # Assert
        assert written == []

    def test_no_skills_does_not_create_claude_md(self, tmp_path):
        # Arrange
        home = tmp_path / "home"
        (home / ".claude" / "skills").mkdir(parents=True)
        # Act
        aggregate_skills_into_claudemd(home)
        # Assert
        assert not (home / "CLAUDE.md").exists()

    def test_removing_last_skill_strips_block(self, tmp_path):
        # Arrange
        home = _home_with_skills(tmp_path, {"s1.md": "body"}, claude_md="# Agent\n")
        aggregate_skills_into_claudemd(home)
        (home / ".claude" / "skills" / "s1.md").unlink()
        # Act
        aggregate_skills_into_claudemd(home)
        # Assert
        assert SKILLS_START_MARKER not in (home / "CLAUDE.md").read_text()

    def test_removing_last_skill_keeps_user_content(self, tmp_path):
        # Arrange
        home = _home_with_skills(
            tmp_path, {"s1.md": "body"}, claude_md="# Agent\nkeep mission\n"
        )
        aggregate_skills_into_claudemd(home)
        (home / ".claude" / "skills" / "s1.md").unlink()
        # Act
        aggregate_skills_into_claudemd(home)
        # Assert
        assert "keep mission" in (home / "CLAUDE.md").read_text()


class TestAggregateCreatesClaudeMd:
    def test_creates_root_claude_md_when_absent(self, tmp_path):
        # Arrange — skills present, no CLAUDE.md anywhere.
        home = _home_with_skills(tmp_path, {"s1.md": "body"})
        # Act
        aggregate_skills_into_claudemd(home)
        # Assert
        assert (home / "CLAUDE.md").is_file()

    def test_created_claude_md_has_skill_body(self, tmp_path):
        # Arrange
        home = _home_with_skills(tmp_path, {"s1.md": "fresh body"})
        # Act
        aggregate_skills_into_claudemd(home)
        # Assert
        assert "fresh body" in (home / "CLAUDE.md").read_text()


class TestAggregateIdempotent:
    def test_second_run_keeps_single_block(self, tmp_path):
        # Arrange
        home = _home_with_skills(tmp_path, {"s1.md": "body"}, claude_md="# Agent\n")
        aggregate_skills_into_claudemd(home)
        # Act
        aggregate_skills_into_claudemd(home)
        # Assert
        assert (home / "CLAUDE.md").read_text().count(SKILLS_START_MARKER) == 1

    def test_second_run_byte_identical(self, tmp_path):
        # Arrange
        home = _home_with_skills(tmp_path, {"s1.md": "body"}, claude_md="# Agent\n")
        aggregate_skills_into_claudemd(home)
        first = (home / "CLAUDE.md").read_text()
        # Act
        aggregate_skills_into_claudemd(home)
        # Assert
        assert (home / "CLAUDE.md").read_text() == first

    def test_changed_skill_is_refreshed(self, tmp_path):
        # Arrange
        home = _home_with_skills(tmp_path, {"s1.md": "old body"}, claude_md="# Agent\n")
        aggregate_skills_into_claudemd(home)
        (home / ".claude" / "skills" / "s1.md").write_text("new body")
        # Act
        aggregate_skills_into_claudemd(home)
        text = (home / "CLAUDE.md").read_text()
        # Assert
        assert "new body" in text and "old body" not in text

    def test_changed_skill_keeps_single_block(self, tmp_path):
        # Arrange
        home = _home_with_skills(tmp_path, {"s1.md": "old body"}, claude_md="# Agent\n")
        aggregate_skills_into_claudemd(home)
        (home / ".claude" / "skills" / "s1.md").write_text("new body")
        # Act
        aggregate_skills_into_claudemd(home)
        # Assert
        assert (home / "CLAUDE.md").read_text().count(SKILLS_START_MARKER) == 1


class TestAggregateTargetsBothClaudeMd:
    def test_aggregates_into_dot_claude_claude_md(self, tmp_path):
        # Arrange — only .claude/CLAUDE.md exists (SDK user-memory path).
        home = _home_with_skills(tmp_path, {"s1.md": "body"})
        nested = home / ".claude" / "CLAUDE.md"
        nested.write_text("# nested agent\n")
        # Act
        aggregate_skills_into_claudemd(home)
        # Assert
        assert SKILLS_START_MARKER in nested.read_text()

    def test_aggregates_into_both_when_both_exist(self, tmp_path):
        # Arrange
        home = _home_with_skills(tmp_path, {"s1.md": "body"}, claude_md="# root\n")
        (home / ".claude" / "CLAUDE.md").write_text("# nested\n")
        # Act
        aggregate_skills_into_claudemd(home)
        # Assert
        assert SKILLS_START_MARKER in (home / ".claude" / "CLAUDE.md").read_text()


# ---------------------------------------------------------------------------
# Integration via the real deploy entrypoints (marker-protection coexistence)
# ---------------------------------------------------------------------------


def _spec_with_skills(tmp_path: Path) -> Path:
    """Build a spec dir whose to_home/ has CLAUDE.md + .claude/skills."""
    spec = tmp_path / "spec"
    th = spec / "to_home"
    (th / ".claude" / "skills").mkdir(parents=True)
    (th / "CLAUDE.md").write_text("## Doctrine\nBe helpful.\n")
    (th / ".claude" / "skills" / "SKILL.md").write_text("# index")
    (th / ".claude" / "skills" / "s1.md").write_text("# S1\ndeploy skill body")
    return spec


class TestMaterializeIntegration:
    def test_materialize_inlines_skill(self, tmp_path):
        # Arrange
        spec = _spec_with_skills(tmp_path)
        home = tmp_path / "home"
        # Act
        materialize_to_home(spec, home)
        # Assert
        assert "deploy skill body" in (home / "CLAUDE.md").read_text()

    def test_materialize_keeps_generated_section(self, tmp_path):
        # Arrange
        spec = _spec_with_skills(tmp_path)
        home = tmp_path / "home"
        # Act
        materialize_to_home(spec, home)
        # Assert
        assert "Be helpful." in (home / "CLAUDE.md").read_text()

    def test_redeploy_keeps_single_skills_block(self, tmp_path):
        # Arrange
        spec = _spec_with_skills(tmp_path)
        home = tmp_path / "home"
        materialize_to_home(spec, home)
        # Act — re-deploy (marker-protect carries the prior block forward).
        materialize_to_home(spec, home)
        # Assert
        assert (home / "CLAUDE.md").read_text().count(SKILLS_START_MARKER) == 1

    def test_redeploy_preserves_user_tail_and_block(self, tmp_path):
        # Arrange
        spec = _spec_with_skills(tmp_path)
        home = tmp_path / "home"
        materialize_to_home(spec, home)
        dst = home / "CLAUDE.md"
        dst.write_text(dst.read_text() + "\n### usernote\nkeepme\n")
        # Act
        materialize_to_home(spec, home)
        # Assert
        assert "keepme" in dst.read_text()

    def test_redeploy_user_tail_keeps_single_block(self, tmp_path):
        # Arrange
        spec = _spec_with_skills(tmp_path)
        home = tmp_path / "home"
        materialize_to_home(spec, home)
        dst = home / "CLAUDE.md"
        dst.write_text(dst.read_text() + "\n### usernote\nkeepme\n")
        # Act
        materialize_to_home(spec, home)
        # Assert
        assert dst.read_text().count(SKILLS_START_MARKER) == 1


class TestDeployToHomeIntegration:
    def _cfg(self, tmp_path: Path) -> tuple[AgentConfig, Path]:
        spec = _spec_with_skills(tmp_path)
        cfg = AgentConfig(name="skills-agent")
        cfg.config_path = str(spec / "spec.yaml")
        cfg.to_home = ""
        return cfg, spec

    def test_deploy_inlines_skill(self, tmp_path):
        # Arrange
        cfg, _spec = self._cfg(tmp_path)
        home = tmp_path / "home"
        # Act
        deploy_to_home(cfg, str(home))
        # Assert
        assert "deploy skill body" in (home / "CLAUDE.md").read_text()

    def test_deploy_is_idempotent(self, tmp_path):
        # Arrange
        cfg, _spec = self._cfg(tmp_path)
        home = tmp_path / "home"
        deploy_to_home(cfg, str(home))
        first = (home / "CLAUDE.md").read_text()
        # Act
        deploy_to_home(cfg, str(home))
        # Assert
        assert (home / "CLAUDE.md").read_text() == first


# EOF
