"""Tests for ``runtimes/_skills_resolve.py`` — host-skills resolution.

The host's ``~/.claude/skills/`` is typically a directory of symlinks
pointing into per-project source trees (``general`` →
``~/proj/scitex-dev/src/scitex_dev/_skills/general/`` etc.). Under
apptainer ``--containall`` the host ``proj/`` paths are not inside the
container, so those symlinks dangle. The runtime must materialize them
as symlink-resolved real copies inside the container ``$HOME`` before
launch, so the agent can read its required skills.

PA-306 no-mocks: every test builds real symlink trees under
``tmp_path``. The ``SAC_HOST_SKILLS_DIR`` env override is used to point
the resolver at the test source dir without touching ``$HOME``.
"""

from __future__ import annotations

from pathlib import Path

from scitex_agent_container.runtimes._skills_resolve import (
    deploy_host_skills_resolved,
    resolve_host_skills_dir,
)

_ENV = "SAC_HOST_SKILLS_DIR"


# ---------------------------------------------------------------------------
# Source-tree builders — each test materializes the layout it needs.
# ---------------------------------------------------------------------------


def _make_real_skill_tree(host_skills_dir: Path, name: str, files: dict[str, str]) -> Path:
    """Create a real on-disk skill directory ``host_skills_dir/<name>/``
    with ``files`` (relative path → content). Returns the skill dir.
    """
    skill_dir = host_skills_dir / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    for rel, content in files.items():
        f = skill_dir / rel
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(content)
    return skill_dir


def _make_symlinked_skill(
    host_skills_dir: Path,
    link_name: str,
    *,
    target: Path,
) -> Path:
    """Create ``host_skills_dir/<link_name>`` as a symlink to ``target``.
    Returns the link path.
    """
    host_skills_dir.mkdir(parents=True, exist_ok=True)
    link = host_skills_dir / link_name
    link.symlink_to(target)
    return link


# ---------------------------------------------------------------------------
# resolve_host_skills_dir — discovery semantics
# ---------------------------------------------------------------------------


class TestResolveHostSkillsDir:
    def test_returns_none_when_env_unset_and_default_dir_absent(
        self, tmp_path, env_save_restore
    ):
        # Arrange — clear env; point HOME at a tmp dir with no .claude/skills.
        env_save_restore.delete(_ENV)
        env_save_restore.set("HOME", str(tmp_path))
        # Act
        resolved = resolve_host_skills_dir()
        # Assert
        assert resolved is None

    def test_honours_env_override_to_arbitrary_existing_dir(
        self, tmp_path, env_save_restore
    ):
        # Arrange — env points at an existing dir outside HOME.
        override = tmp_path / "alt_skills"
        override.mkdir()
        env_save_restore.set(_ENV, str(override))
        # Act
        resolved = resolve_host_skills_dir()
        # Assert
        assert resolved == override

    def test_returns_none_when_env_override_points_to_missing_dir(
        self, tmp_path, env_save_restore
    ):
        # Arrange — env points at a path that does not exist.
        env_save_restore.set(_ENV, str(tmp_path / "ghost"))
        # Act
        resolved = resolve_host_skills_dir()
        # Assert
        assert resolved is None

    def test_falls_back_to_default_home_claude_skills_when_env_unset(
        self, tmp_path, env_save_restore
    ):
        # Arrange — fake $HOME with a .claude/skills tree.
        env_save_restore.delete(_ENV)
        env_save_restore.set("HOME", str(tmp_path))
        default = tmp_path / ".claude" / "skills"
        default.mkdir(parents=True)
        # Act
        resolved = resolve_host_skills_dir()
        # Assert
        assert resolved == default


# ---------------------------------------------------------------------------
# deploy_host_skills_resolved — materialization semantics
# ---------------------------------------------------------------------------


class TestDeployHostSkillsResolved:
    def test_materializes_symlinked_skill_as_real_directory_at_dest(self, tmp_path):
        # Arrange — real skill source outside host_skills_dir; symlink to it.
        host_skills = tmp_path / "host_skills"
        proj_source = tmp_path / "proj" / "scitex_dev_skills" / "general"
        _make_real_skill_tree(
            proj_source.parent, "general", {"SKILL.md": "general body"}
        )
        _make_symlinked_skill(host_skills, "general", target=proj_source)
        dest = tmp_path / "dest_home"
        # Act
        deploy_host_skills_resolved(dest, host_skills_dir=host_skills)
        # Assert — dest entry is a real directory, NOT a symlink.
        assert (dest / ".claude" / "skills" / "general").is_dir() and not (
            dest / ".claude" / "skills" / "general"
        ).is_symlink()

    def test_dereferences_symlinks_nested_inside_resolved_skill_tree(self, tmp_path):
        # Arrange — proj_source/SKILL.md is itself a symlink to a real file.
        host_skills = tmp_path / "host_skills"
        real_file = tmp_path / "real" / "SKILL.md"
        real_file.parent.mkdir(parents=True)
        real_file.write_text("real content")
        proj_source = tmp_path / "proj" / "general"
        proj_source.mkdir(parents=True)
        (proj_source / "SKILL.md").symlink_to(real_file)
        _make_symlinked_skill(host_skills, "general", target=proj_source)
        dest = tmp_path / "dest_home"
        # Act
        deploy_host_skills_resolved(dest, host_skills_dir=host_skills)
        # Assert — the nested SKILL.md at dest is a real file with real content.
        assert (dest / ".claude" / "skills" / "general" / "SKILL.md").read_text() == (
            "real content"
        )

    def test_skips_dangling_top_level_symlink_silently_without_creating_dest(
        self, tmp_path
    ):
        # Arrange — host symlink points at a path that does not exist.
        host_skills = tmp_path / "host_skills"
        _make_symlinked_skill(
            host_skills, "broken", target=tmp_path / "no" / "such" / "path"
        )
        dest = tmp_path / "dest_home"
        # Act
        deploy_host_skills_resolved(dest, host_skills_dir=host_skills)
        # Assert — no "broken" entry materialised at dest.
        assert not (dest / ".claude" / "skills" / "broken").exists()

    def test_returns_delivered_skill_names_in_sorted_order(self, tmp_path):
        # Arrange — two skill sources, one real-dir + one symlink.
        host_skills = tmp_path / "host_skills"
        # Real on-disk dir as a direct child of host_skills.
        _make_real_skill_tree(host_skills, "zeta", {"SKILL.md": "zeta"})
        # Symlink to an outside dir.
        outside = tmp_path / "outside" / "alpha"
        _make_real_skill_tree(outside.parent, "alpha", {"SKILL.md": "alpha"})
        _make_symlinked_skill(host_skills, "alpha", target=outside)
        dest = tmp_path / "dest_home"
        # Act
        delivered = deploy_host_skills_resolved(dest, host_skills_dir=host_skills)
        # Assert — sorted names returned.
        assert delivered == ["alpha", "zeta"]

    def test_replaces_previous_destination_tree_on_repeated_call(self, tmp_path):
        # Arrange — deploy once, then change source and re-deploy.
        host_skills = tmp_path / "host_skills"
        proj_source = tmp_path / "proj" / "general"
        _make_real_skill_tree(proj_source.parent, "general", {"SKILL.md": "v1"})
        _make_symlinked_skill(host_skills, "general", target=proj_source)
        dest = tmp_path / "dest_home"
        deploy_host_skills_resolved(dest, host_skills_dir=host_skills)
        # Change the source content, re-deploy.
        (proj_source / "SKILL.md").write_text("v2")
        # Act
        deploy_host_skills_resolved(dest, host_skills_dir=host_skills)
        # Assert — destination carries v2.
        assert (
            dest / ".claude" / "skills" / "general" / "SKILL.md"
        ).read_text() == "v2"

    def test_copies_top_level_file_entry_to_dest(self, tmp_path):
        # Arrange — host_skills/SKILL.md is a regular file (e.g. user meta).
        host_skills = tmp_path / "host_skills"
        host_skills.mkdir()
        (host_skills / "SKILL.md").write_text("user-level skills meta")
        dest = tmp_path / "dest_home"
        # Act
        deploy_host_skills_resolved(dest, host_skills_dir=host_skills)
        # Assert
        assert (dest / ".claude" / "skills" / "SKILL.md").read_text() == (
            "user-level skills meta"
        )

    def test_returns_empty_list_when_host_skills_dir_missing(self, tmp_path):
        # Arrange — host_skills path does not exist.
        ghost = tmp_path / "ghost_host_skills"
        dest = tmp_path / "dest_home"
        # Act
        delivered = deploy_host_skills_resolved(dest, host_skills_dir=ghost)
        # Assert
        assert delivered == []

    def test_does_not_create_dest_skills_dir_when_no_entries_to_deliver(
        self, tmp_path
    ):
        # Arrange — host dir exists but is empty.
        host_skills = tmp_path / "host_skills"
        host_skills.mkdir()
        dest = tmp_path / "dest_home"
        # Act
        deploy_host_skills_resolved(dest, host_skills_dir=host_skills)
        # Assert — empty source must not create the dest skills tree.
        assert not (dest / ".claude" / "skills").exists()

    def test_resolves_host_dir_from_env_when_explicit_arg_omitted(
        self, tmp_path, env_save_restore
    ):
        # Arrange — env var points the resolver at our source dir.
        host_skills = tmp_path / "host_skills"
        proj_source = tmp_path / "proj" / "general"
        _make_real_skill_tree(proj_source.parent, "general", {"SKILL.md": "via env"})
        _make_symlinked_skill(host_skills, "general", target=proj_source)
        env_save_restore.set(_ENV, str(host_skills))
        dest = tmp_path / "dest_home"
        # Act — no host_skills_dir arg; resolver pulls from env.
        deploy_host_skills_resolved(dest)
        # Assert
        assert (
            dest / ".claude" / "skills" / "general" / "SKILL.md"
        ).read_text() == "via env"

    def test_resolved_tree_contains_no_dangling_symlinks(self, tmp_path):
        # Arrange — source tree carries a relative symlink to a sibling file.
        host_skills = tmp_path / "host_skills"
        proj_source = tmp_path / "proj" / "general"
        proj_source.mkdir(parents=True)
        (proj_source / "real.md").write_text("payload")
        (proj_source / "alias.md").symlink_to("real.md")  # relative target
        _make_symlinked_skill(host_skills, "general", target=proj_source)
        dest = tmp_path / "dest_home"
        # Act
        deploy_host_skills_resolved(dest, host_skills_dir=host_skills)
        # Assert — the alias is a real file at dest (not a symlink).
        assert not (dest / ".claude" / "skills" / "general" / "alias.md").is_symlink()
