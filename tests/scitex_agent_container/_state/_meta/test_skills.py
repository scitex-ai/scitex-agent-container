"""Tests for ``_state._meta.skills`` — CLAUDE.md skills-block parser.

PS-202 src-tests mirror. Real ``tmp_path``-backed CLAUDE.md fixtures —
no mocks, no monkeypatch.
"""

from __future__ import annotations

from pathlib import Path

from scitex_agent_container._state._meta.skills import _parse_skills


def test_parse_skills_returns_empty_for_missing_workdir(tmp_path: Path):
    # Arrange
    workdir = tmp_path / "no-such-dir"
    # Act
    skills = _parse_skills(str(workdir))
    # Assert
    assert skills == []


def test_parse_skills_returns_empty_when_claude_md_absent(tmp_path: Path):
    # Arrange
    workdir = tmp_path
    # Act
    skills = _parse_skills(str(workdir))
    # Assert
    assert skills == []


def test_parse_skills_extracts_single_block(tmp_path: Path):
    # Arrange
    cmd = tmp_path / "CLAUDE.md"
    cmd.write_text("preamble\n```skills\nalpha\nbeta\n```\nfooter\n")
    # Act
    skills = _parse_skills(str(tmp_path))
    # Assert
    assert skills == ["alpha", "beta"]


def test_parse_skills_skips_comment_lines(tmp_path: Path):
    # Arrange
    cmd = tmp_path / "CLAUDE.md"
    cmd.write_text("```skills\n# a comment\ngamma\n```\n")
    # Act
    skills = _parse_skills(str(tmp_path))
    # Assert
    assert skills == ["gamma"]


def test_parse_skills_concatenates_multiple_blocks(tmp_path: Path):
    # Arrange
    cmd = tmp_path / "CLAUDE.md"
    cmd.write_text("```skills\nfirst\n```\n\nbody text\n\n```skills\nsecond\n```\n")
    # Act
    skills = _parse_skills(str(tmp_path))
    # Assert
    assert skills == ["first", "second"]
