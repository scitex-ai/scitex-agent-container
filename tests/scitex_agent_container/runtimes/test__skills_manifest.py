"""Tests for ``runtimes/_skills_manifest.py``.

The manifest helper builds a SOFT skill-discovery block that gets
appended to ``ClaudeAgentOptions.system_prompt``. The block carries the
SKILL.md frontmatter (name + description-first-paragraph) plus the
on-disk path so the agent can lazy-load the body via the ``Skill`` tool.

Doctrine: NEVER hard-load skill bodies into the system_prompt. The
manifest is advisory only — see ``_skills/scitex-agent-container/
25_claude-setup-delivery.md`` for why setting_sources=[] makes this
necessary.

TDD: every test follows Arrange / Act / Assert. The manifest helper
raises no exceptions; broken/missing SKILL.md files degrade to a
path-only bullet so the operator SEES the gap in the agent's first-turn
context instead of an invisible "skill didn't load" failure.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from scitex_agent_container.runtimes._skills_manifest import (
    build_skills_manifest_block,
)

# ---------------------------------------------------------------------------
# Empty-input contract
# ---------------------------------------------------------------------------


class TestEmpty:
    def test_empty_skill_names_returns_none(self, tmp_path: Path) -> None:
        # Arrange — no skill names to list.
        # Act
        result = build_skills_manifest_block([], tmp_path)
        # Assert — no manifest to inject; system_prompt stays as-is.
        assert result is None

    def test_all_skills_missing_returns_block_with_path_only_bullets(
        self, tmp_path: Path
    ) -> None:
        # Arrange — operator listed a skill but its SKILL.md is absent.
        # The block STILL renders (operator sees the gap), with the
        # path-only bullet form.
        # Act
        result = build_skills_manifest_block(["ghost-skill"], tmp_path)
        # Assert
        assert result is not None
        assert "ghost-skill" in result


# ---------------------------------------------------------------------------
# Block shape — the LOCKED contract.
# ---------------------------------------------------------------------------


def _write_skill(root: Path, name: str, description: str | None) -> Path:
    """Helper: write a fake SKILL.md with the given frontmatter."""
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    skill_file = skill_dir / "SKILL.md"
    if description is None:
        body = f"---\nname: {name}\ntags: [test]\n---\n\n# {name}\n"
    else:
        body = (
            f"---\nname: {name}\ndescription: |\n  {description}\n"
            f"tags: [test]\n---\n\n# {name}\n"
        )
    skill_file.write_text(body, encoding="utf-8")
    return skill_file


class TestBlockShape:
    @pytest.fixture
    def _one_skill(self, tmp_path: Path) -> Path:
        # Arrange — one valid SKILL.md under tmp_path/<name>/SKILL.md.
        _write_skill(tmp_path, "scitex-writer", "[WHAT] Writes papers.")
        return tmp_path

    def test_one_skill_block_has_heading(self, _one_skill: Path) -> None:
        # Arrange
        # Act
        block = build_skills_manifest_block(["scitex-writer"], _one_skill)
        # Assert — the heading is the LOCKED entry point so the
        # idempotency check in build_sdk_options can spot it.
        assert block is not None
        assert block.startswith("## Available skills\n")

    def test_one_skill_block_carries_skill_path(self, _one_skill: Path) -> None:
        # Arrange
        # Act
        block = build_skills_manifest_block(["scitex-writer"], _one_skill)
        # Assert — the on-disk path the SDK can hand to the Skill tool.
        assert "path: ~/.claude/skills/scitex-writer/SKILL.md" in block

    def test_one_skill_block_carries_skill_name(self, _one_skill: Path) -> None:
        # Arrange
        # Act
        block = build_skills_manifest_block(["scitex-writer"], _one_skill)
        # Assert
        assert "- scitex-writer:" in block

    def test_one_skill_block_carries_description(self, _one_skill: Path) -> None:
        # Arrange — the [WHAT] line is the canonical summary surface.
        # Act
        block = build_skills_manifest_block(["scitex-writer"], _one_skill)
        # Assert
        assert "Writes papers." in block


class TestMultipleSkills:
    @pytest.fixture
    def _three_skills(self, tmp_path: Path) -> Path:
        # Arrange — three valid skills in a deterministic order so
        # we can pin the rendered ordering below.
        _write_skill(tmp_path, "alpha", "[WHAT] Alpha summary.")
        _write_skill(tmp_path, "bravo", "[WHAT] Bravo summary.")
        _write_skill(tmp_path, "charlie", "[WHAT] Charlie summary.")
        return tmp_path

    def test_multiple_skills_block_has_three_bullets(self, _three_skills: Path) -> None:
        # Arrange
        # Act
        block = build_skills_manifest_block(
            ["alpha", "bravo", "charlie"], _three_skills
        )
        # Assert — one bullet per name.
        assert block is not None
        n_bullets = sum(1 for line in block.splitlines() if line.startswith("- "))
        assert n_bullets == 3

    def test_multiple_skills_preserve_input_order(self, _three_skills: Path) -> None:
        # Arrange
        # Act
        block = build_skills_manifest_block(
            ["charlie", "alpha", "bravo"], _three_skills
        )
        # Assert — input order is preserved verbatim (operators control
        # the order so the most relevant skill leads the list).
        idx_c = block.index("- charlie:")
        idx_a = block.index("- alpha:")
        idx_b = block.index("- bravo:")
        assert idx_c < idx_a < idx_b


# ---------------------------------------------------------------------------
# Description handling — [WHAT] line, plain first line, missing, truncation.
# ---------------------------------------------------------------------------


class TestDescription:
    def test_what_line_is_preferred(self, tmp_path: Path) -> None:
        # Arrange — a YAML-multiline description with [WHAT]/[WHEN]/[HOW]
        # structure (the scitex skill convention). The [WHAT] line is the
        # canonical first paragraph.
        descr = "[WHAT] Main summary.\n  [WHEN] When to use.\n  [HOW] How to use."
        _write_skill(tmp_path, "skl", descr)
        # Act
        block = build_skills_manifest_block(["skl"], tmp_path)
        # Assert — only the [WHAT] line surfaces; WHEN/HOW are body
        # concerns the agent fetches via the Skill tool if needed.
        assert "[WHAT] Main summary." in block
        assert "[WHEN]" not in block

    def test_plain_description_uses_first_non_empty_line(self, tmp_path: Path) -> None:
        # Arrange — a plain prose description with no [WHAT] marker.
        descr = "Just a plain summary line.\n  And a second line."
        _write_skill(tmp_path, "skl", descr)
        # Act
        block = build_skills_manifest_block(["skl"], tmp_path)
        # Assert — first non-empty line wins.
        assert "Just a plain summary line." in block
        assert "And a second line." not in block

    def test_missing_description_field_falls_back_to_placeholder(
        self, tmp_path: Path
    ) -> None:
        # Arrange — SKILL.md without a description field.
        _write_skill(tmp_path, "skl", None)
        # Act
        block = build_skills_manifest_block(["skl"], tmp_path)
        # Assert — placeholder so the bullet still renders.
        assert "<no description>" in block

    def test_long_description_is_truncated_to_240_chars(self, tmp_path: Path) -> None:
        # Arrange — a single line longer than the 240-char cap.
        long_line = "x" * 500
        _write_skill(tmp_path, "skl", long_line)
        # Act
        block = build_skills_manifest_block(["skl"], tmp_path)
        # Assert — the bullet's description portion fits within the cap.
        # We look for the line that introduces the skill and check its
        # length excluding the "- skl: " prefix.
        bullet_line = next(
            line for line in block.splitlines() if line.startswith("- skl:")
        )
        # "- skl: " is 7 chars; truncated description must be <= 240 chars.
        description_only = bullet_line[len("- skl: ") :]
        assert len(description_only) <= 240


# ---------------------------------------------------------------------------
# Missing SKILL.md — degrade to a path-only bullet.
# ---------------------------------------------------------------------------


class TestMissingSkillFile:
    def test_missing_skill_md_renders_bullet_with_name(self, tmp_path: Path) -> None:
        # Arrange — operator lists a skill that doesn't exist on disk.
        # Act
        block = build_skills_manifest_block(["missing-one"], tmp_path)
        # Assert — the operator must SEE the gap in the agent's first
        # turn rather than discover it through silent missing behavior.
        assert block is not None
        assert "- missing-one:" in block

    def test_missing_skill_md_renders_path(self, tmp_path: Path) -> None:
        # Arrange
        # Act
        block = build_skills_manifest_block(["missing-one"], tmp_path)
        # Assert — the path field still appears so the operator can
        # diagnose ("is the to_home mount delivering the skill?").
        assert "path: ~/.claude/skills/missing-one/SKILL.md" in block


# ---------------------------------------------------------------------------
# Mixed: one valid + one missing must both surface.
# ---------------------------------------------------------------------------


class TestMixedValidAndMissing:
    def test_valid_skill_surfaces_alongside_missing(self, tmp_path: Path) -> None:
        # Arrange — one skill present, one absent.
        _write_skill(tmp_path, "good", "[WHAT] Good summary.")
        # Act
        block = build_skills_manifest_block(["good", "bad"], tmp_path)
        # Assert — both bullets render.
        assert "- good:" in block
        assert "- bad:" in block
