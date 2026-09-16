"""Exact materialization from the canonical SciTeX skill-package root."""

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from scitex_agent_container.runtimes._skill_packages import (
    SkillPackageError,
    materialize_skill_packages,
    retain_adjacent_to_home_skills,
)


@dataclass
class _Config:
    skill_packages: list[str] = field(default_factory=list)


def _package(root: Path, package_id: str) -> Path:
    package = root / package_id
    package.mkdir(parents=True)
    (package / "guide.md").write_text("# skill\n", encoding="utf-8")
    return package


def test_only_declared_packages_are_materialized(tmp_path):
    root = tmp_path / "canonical"
    _package(root, "selected")
    _package(root, "not-selected")
    home = tmp_path / "agent-home"

    materialize_skill_packages(
        _Config(["selected"]), home, source_root=root
    )

    skills = home / ".claude" / "skills"
    assert (skills / "selected" / "guide.md").read_text() == "# skill\n"
    assert not (skills / "selected").is_symlink()
    assert not (skills / "not-selected").exists()


def test_missing_package_lists_available_and_never_falls_back(tmp_path):
    root = tmp_path / "canonical"
    _package(root, "installed-a")
    _package(root, "installed-b")

    with pytest.raises(SkillPackageError) as caught:
        materialize_skill_packages(
            _Config(["missing"]), tmp_path / "agent-home", source_root=root
        )

    assert str(caught.value) == (
        f"skill package 'missing' is not installed at {root / 'missing'}; "
        "available packages: installed-a, installed-b. No fallback was "
        "materialized."
    )


def test_broken_package_refuses_before_materializing_anything(tmp_path):
    root = tmp_path / "canonical"
    _package(root, "good")
    (root / "broken").mkdir(parents=True)
    home = tmp_path / "agent-home"

    with pytest.raises(SkillPackageError, match="contains no Markdown"):
        materialize_skill_packages(
            _Config(["good", "broken"]), home, source_root=root
        )

    assert not (home / ".claude" / "skills" / "good").exists()


def test_removed_declaration_removes_previous_managed_link(tmp_path):
    root = tmp_path / "canonical"
    _package(root, "old")
    home = tmp_path / "agent-home"
    materialize_skill_packages(_Config(["old"]), home, source_root=root)

    materialize_skill_packages(_Config([]), home, source_root=root)

    assert not (home / ".claude" / "skills" / "old").exists()


def test_to_home_name_collision_refuses_instead_of_overwriting(tmp_path):
    root = tmp_path / "canonical"
    _package(root, "same")
    home = tmp_path / "agent-home"
    collision = home / ".claude" / "skills" / "same"
    collision.mkdir(parents=True)
    (collision / "SKILL.md").write_text("local\n", encoding="utf-8")

    with pytest.raises(SkillPackageError, match="already exists from spec.to_home"):
        materialize_skill_packages(_Config(["same"]), home, source_root=root)


def test_shared_or_host_skills_are_pruned_but_adjacent_to_home_survives(tmp_path):
    home = tmp_path / "agent-home"
    skills = home / ".claude" / "skills"
    (skills / "implicit").mkdir(parents=True)
    adjacent = tmp_path / "agent" / "to_home"
    (adjacent / ".claude" / "skills" / "declared-here").mkdir(parents=True)
    (skills / "declared-here").mkdir()

    retain_adjacent_to_home_skills(home, adjacent)

    assert not (skills / "implicit").exists()
    assert (skills / "declared-here").is_dir()
