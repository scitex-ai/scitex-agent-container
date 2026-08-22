"""Tests for ``sac skills`` group — list / get / install.

PA-306 no-mocks: every test exercises real production collaborators.

Seam strategy
-------------
``_skills_root()`` reads from a fixed package path with no env-var or
config injection seam. To avoid touching the real bundled directory we
swap the module-level callable with a tmp-dir-returning lambda inside
fixtures and restore the original in teardown. This is NOT
``pytest.MonkeyPatch`` or ``unittest.mock`` — it is a real attribute
mutation against the actual production module, the same honest
save/restore pattern used elsewhere in PA-306 when no production seam
exists.
"""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg import skills_group as sg
from scitex_agent_container.cli_pkg.skills_group import skills_group


@pytest.fixture(autouse=True)
def sandbox_home(tmp_path):
    """Sandbox $HOME so install commands never touch the real one. PA-306."""
    import os

    saved = os.environ.get("HOME")
    home = tmp_path / "home"
    home.mkdir()
    os.environ["HOME"] = str(home)
    try:
        yield tmp_path
    finally:
        if saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved


@pytest.fixture
def fake_skills_root(tmp_path):
    """Make _skills_root() point to a tmp dir with two .md files."""
    root = tmp_path / "skills_src"
    root.mkdir()
    (root / "01_alpha.md").write_text("# alpha skill\nbody\n", encoding="utf-8")
    sub = root / "nested"
    sub.mkdir()
    (sub / "02_beta.md").write_text("# beta\n", encoding="utf-8")
    saved = sg._skills_root
    sg._skills_root = lambda: root  # type: ignore[assignment]
    try:
        yield root
    finally:
        sg._skills_root = saved  # type: ignore[assignment]


@pytest.fixture
def empty_skills_root(tmp_path):
    """Make _skills_root() point to a non-existent dir."""
    saved = sg._skills_root
    missing = tmp_path / "missing"
    sg._skills_root = lambda: missing  # type: ignore[assignment]
    try:
        yield missing
    finally:
        sg._skills_root = saved  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_human_readable_exits_zero(fake_skills_root):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(skills_group, ["list"])
    # Assert
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize("stem", ["01_alpha", "02_beta"])
def test_list_human_readable_lists_each_skill(fake_skills_root, stem):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(skills_group, ["list"])
    # Assert
    assert stem in result.output


def test_list_json_output_exits_zero(fake_skills_root):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(skills_group, ["list", "--json"])
    # Assert
    assert result.exit_code == 0, result.output


def test_list_json_output_contains_all_stems(fake_skills_root):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(skills_group, ["list", "--json"])
    data = json.loads(result.stdout)
    stems = sorted(d["name"] for d in data)
    # Assert
    assert stems == ["01_alpha", "02_beta"]


def test_list_empty_directory_exits_nonzero(empty_skills_root):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(skills_group, ["list"])
    # Assert
    assert result.exit_code != 0


def test_list_empty_directory_reports_no_skills(empty_skills_root):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(skills_group, ["list"])
    # Assert
    assert "no skills" in result.output


# ---------------------------------------------------------------------------
# get
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["01_alpha", "01_alpha.md"])
def test_get_by_stem_exits_zero(fake_skills_root, name):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(skills_group, ["get", name])
    # Assert
    assert result.exit_code == 0


@pytest.mark.parametrize("name", ["01_alpha", "01_alpha.md"])
def test_get_by_stem_returns_content(fake_skills_root, name):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(skills_group, ["get", name])
    # Assert
    assert "alpha skill" in result.output


def test_get_json_payload_exits_zero(fake_skills_root):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(skills_group, ["get", "02_beta", "--json"])
    # Assert
    assert result.exit_code == 0


def test_get_json_payload_carries_name(fake_skills_root):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(skills_group, ["get", "02_beta", "--json"])
    payload = json.loads(result.stdout)
    # Assert
    assert payload["name"] == "02_beta"


def test_get_json_payload_carries_content(fake_skills_root):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(skills_group, ["get", "02_beta", "--json"])
    payload = json.loads(result.stdout)
    # Assert
    assert "beta" in payload["content"]


def test_get_unknown_name_exits_nonzero(fake_skills_root):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(skills_group, ["get", "does_not_exist"])
    # Assert
    assert result.exit_code != 0


@pytest.mark.parametrize("fragment", ["skill not found", "available:"])
def test_get_unknown_name_reports_error(fake_skills_root, fragment):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(skills_group, ["get", "does_not_exist"])
    # Assert
    assert fragment in result.output


# ---------------------------------------------------------------------------
# install — dry-run
# ---------------------------------------------------------------------------


def test_install_dry_run_exits_zero(fake_skills_root, tmp_path):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(skills_group, ["install", "--dry-run"])
    # Assert
    assert result.exit_code == 0


def test_install_dry_run_announces_symlink(fake_skills_root, tmp_path):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(skills_group, ["install", "--dry-run"])
    # Assert
    assert "would symlink" in result.output


def test_install_dry_run_with_claude_symlink_exits_zero(fake_skills_root):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(
        skills_group, ["install", "--dry-run", "--claude-symlink", "--no-link"]
    )
    # Assert
    assert result.exit_code == 0


@pytest.mark.parametrize("fragment", ["would copy", "would symlink"])
def test_install_dry_run_with_claude_symlink_announces(fake_skills_root, fragment):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(
        skills_group, ["install", "--dry-run", "--claude-symlink", "--no-link"]
    )
    # Assert
    assert fragment in result.output


# ---------------------------------------------------------------------------
# install — real filesystem
# ---------------------------------------------------------------------------


def test_install_symlinks_real_exits_zero(fake_skills_root, tmp_path):
    # Arrange
    runner = CliRunner()
    dest = tmp_path / "dest"
    # Act
    result = runner.invoke(skills_group, ["install", "--dest", str(dest)])
    # Assert
    assert result.exit_code == 0, result.output


def test_install_symlinks_real_creates_symlink(fake_skills_root, tmp_path):
    # Arrange
    runner = CliRunner()
    dest = tmp_path / "dest"
    # Act
    runner.invoke(skills_group, ["install", "--dest", str(dest)])
    target = dest / "scitex-agent-container"
    # Assert
    assert target.is_symlink()


def test_install_symlinks_real_exposes_skill_files(fake_skills_root, tmp_path):
    # Arrange
    runner = CliRunner()
    dest = tmp_path / "dest"
    # Act
    runner.invoke(skills_group, ["install", "--dest", str(dest)])
    target = dest / "scitex-agent-container"
    # Assert
    assert (target / "01_alpha.md").is_file()


def test_install_no_link_copies_exits_zero(fake_skills_root, tmp_path):
    # Arrange
    runner = CliRunner()
    dest = tmp_path / "dest"
    # Act
    result = runner.invoke(skills_group, ["install", "--dest", str(dest), "--no-link"])
    # Assert
    assert result.exit_code == 0, result.output


def test_install_no_link_copies_creates_real_directory(fake_skills_root, tmp_path):
    # Arrange
    runner = CliRunner()
    dest = tmp_path / "dest"
    # Act
    runner.invoke(skills_group, ["install", "--dest", str(dest), "--no-link"])
    target = dest / "scitex-agent-container"
    # Assert
    assert target.is_dir()


def test_install_no_link_copies_is_not_a_symlink(fake_skills_root, tmp_path):
    # Arrange
    runner = CliRunner()
    dest = tmp_path / "dest"
    # Act
    runner.invoke(skills_group, ["install", "--dest", str(dest), "--no-link"])
    target = dest / "scitex-agent-container"
    # Assert
    assert not target.is_symlink()


def test_install_no_link_copies_includes_skill_files(fake_skills_root, tmp_path):
    # Arrange
    runner = CliRunner()
    dest = tmp_path / "dest"
    # Act
    runner.invoke(skills_group, ["install", "--dest", str(dest), "--no-link"])
    target = dest / "scitex-agent-container"
    # Assert
    assert (target / "01_alpha.md").is_file()


def test_install_replaces_existing_symlink_exits_zero(fake_skills_root, tmp_path):
    # Arrange
    runner = CliRunner()
    dest = tmp_path / "dest"
    dest.mkdir()
    target = dest / "scitex-agent-container"
    target.symlink_to(tmp_path / "nonexistent")
    # Act
    result = runner.invoke(skills_group, ["install", "--dest", str(dest)])
    # Assert
    assert result.exit_code == 0, result.output


def test_install_replaces_existing_symlink_is_symlink(fake_skills_root, tmp_path):
    # Arrange
    runner = CliRunner()
    dest = tmp_path / "dest"
    dest.mkdir()
    target = dest / "scitex-agent-container"
    target.symlink_to(tmp_path / "nonexistent")
    # Act
    runner.invoke(skills_group, ["install", "--dest", str(dest)])
    # Assert
    assert target.is_symlink()


def test_install_replaces_existing_symlink_exposes_skill(fake_skills_root, tmp_path):
    # Arrange
    runner = CliRunner()
    dest = tmp_path / "dest"
    dest.mkdir()
    target = dest / "scitex-agent-container"
    target.symlink_to(tmp_path / "nonexistent")
    # Act
    runner.invoke(skills_group, ["install", "--dest", str(dest)])
    # Assert
    assert (target / "01_alpha.md").is_file()


def test_install_replaces_existing_dir_exits_zero(fake_skills_root, tmp_path):
    # Arrange
    runner = CliRunner()
    dest = tmp_path / "dest"
    target = dest / "scitex-agent-container"
    target.mkdir(parents=True)
    (target / "leftover.txt").write_text("old")
    # Act
    result = runner.invoke(skills_group, ["install", "--dest", str(dest), "--no-link"])
    # Assert
    assert result.exit_code == 0, result.output


def test_install_replaces_existing_dir_removes_old_files(fake_skills_root, tmp_path):
    # Arrange
    runner = CliRunner()
    dest = tmp_path / "dest"
    target = dest / "scitex-agent-container"
    target.mkdir(parents=True)
    (target / "leftover.txt").write_text("old")
    # Act
    runner.invoke(skills_group, ["install", "--dest", str(dest), "--no-link"])
    # Assert
    assert not (target / "leftover.txt").exists()


def test_install_replaces_existing_dir_writes_new_files(fake_skills_root, tmp_path):
    # Arrange
    runner = CliRunner()
    dest = tmp_path / "dest"
    target = dest / "scitex-agent-container"
    target.mkdir(parents=True)
    (target / "leftover.txt").write_text("old")
    # Act
    runner.invoke(skills_group, ["install", "--dest", str(dest), "--no-link"])
    # Assert
    assert (target / "01_alpha.md").is_file()


def test_install_claude_symlink_real_exits_zero(
    fake_skills_root, tmp_path, sandbox_home
):
    # Arrange
    runner = CliRunner()
    dest = tmp_path / "dest"
    # Act
    result = runner.invoke(
        skills_group,
        ["install", "--dest", str(dest), "--claude-symlink"],
    )
    # Assert
    assert result.exit_code == 0, result.output


def test_install_claude_symlink_real_creates_link(
    fake_skills_root, tmp_path, sandbox_home
):
    # Arrange
    runner = CliRunner()
    dest = tmp_path / "dest"
    # Act
    runner.invoke(
        skills_group,
        ["install", "--dest", str(dest), "--claude-symlink"],
    )
    link = sandbox_home / "home" / ".claude" / "skills" / "scitex"
    # Assert
    assert link.is_symlink()


def test_install_claude_symlink_skips_when_exists_nonlink_exits_zero(
    fake_skills_root, tmp_path, sandbox_home
):
    # Arrange
    runner = CliRunner()
    dest = tmp_path / "dest"
    link = sandbox_home / "home" / ".claude" / "skills" / "scitex"
    link.parent.mkdir(parents=True)
    link.mkdir()  # exists as a real dir, not a symlink
    # Act
    result = runner.invoke(
        skills_group,
        ["install", "--dest", str(dest), "--claude-symlink"],
    )
    # Assert
    assert result.exit_code == 0, result.output


def test_install_claude_symlink_skips_when_exists_nonlink_reports_skip(
    fake_skills_root, tmp_path, sandbox_home
):
    # Arrange
    runner = CliRunner()
    dest = tmp_path / "dest"
    link = sandbox_home / "home" / ".claude" / "skills" / "scitex"
    link.parent.mkdir(parents=True)
    link.mkdir()  # exists as a real dir, not a symlink
    # Act
    result = runner.invoke(
        skills_group,
        ["install", "--dest", str(dest), "--claude-symlink"],
    )
    # Assert
    assert "skipping" in result.output


def test_install_no_skills_dir_exits_nonzero(empty_skills_root):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(skills_group, ["install"])
    # Assert
    assert result.exit_code != 0


def test_install_no_skills_dir_reports_missing(empty_skills_root):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(skills_group, ["install"])
    # Assert
    assert "no skills directory" in result.output
