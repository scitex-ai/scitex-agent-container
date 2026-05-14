"""Tests for ``sac skills`` group — list / get / install."""

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


def test_list_human(fake_skills_root):
    runner = CliRunner()
    result = runner.invoke(skills_group, ["list"])
    assert result.exit_code == 0, result.output
    assert "01_alpha" in result.output
    assert "02_beta" in result.output


def test_list_json(fake_skills_root):
    runner = CliRunner()
    result = runner.invoke(skills_group, ["list", "--json"])
    assert result.exit_code == 0, result.output
    data = json.loads(result.output)
    stems = sorted(d["name"] for d in data)
    assert stems == ["01_alpha", "02_beta"]


def test_list_empty(tmp_path):
    saved = sg._skills_root
    sg._skills_root = lambda: tmp_path / "missing"  # type: ignore[assignment]
    try:
        runner = CliRunner()
        result = runner.invoke(skills_group, ["list"])
    finally:
        sg._skills_root = saved  # type: ignore[assignment]
    assert result.exit_code != 0
    assert "no skills" in result.output


def test_get_by_stem(fake_skills_root):
    runner = CliRunner()
    result = runner.invoke(skills_group, ["get", "01_alpha"])
    assert result.exit_code == 0
    assert "alpha skill" in result.output


def test_get_with_md_suffix(fake_skills_root):
    runner = CliRunner()
    result = runner.invoke(skills_group, ["get", "01_alpha.md"])
    assert result.exit_code == 0
    assert "alpha skill" in result.output


def test_get_json(fake_skills_root):
    runner = CliRunner()
    result = runner.invoke(skills_group, ["get", "02_beta", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.output)
    assert payload["name"] == "02_beta"
    assert "beta" in payload["content"]


def test_get_not_found(fake_skills_root):
    runner = CliRunner()
    result = runner.invoke(skills_group, ["get", "does_not_exist"])
    assert result.exit_code != 0
    assert "skill not found" in result.output
    assert "available:" in result.output


def test_install_dry_run(fake_skills_root, tmp_path):
    runner = CliRunner()
    result = runner.invoke(skills_group, ["install", "--dry-run"])
    assert result.exit_code == 0
    assert "would symlink" in result.output


def test_install_dry_run_with_claude_symlink(fake_skills_root):
    runner = CliRunner()
    result = runner.invoke(
        skills_group, ["install", "--dry-run", "--claude-symlink", "--no-link"]
    )
    assert result.exit_code == 0
    assert "would copy" in result.output
    assert "would symlink" in result.output  # the claude-symlink line


def test_install_symlinks_real(fake_skills_root, tmp_path):
    runner = CliRunner()
    dest = tmp_path / "dest"
    result = runner.invoke(skills_group, ["install", "--dest", str(dest)])
    assert result.exit_code == 0, result.output
    target = dest / "scitex-agent-container"
    assert target.is_symlink()
    assert (target / "01_alpha.md").is_file()


def test_install_no_link_copies(fake_skills_root, tmp_path):
    runner = CliRunner()
    dest = tmp_path / "dest"
    result = runner.invoke(skills_group, ["install", "--dest", str(dest), "--no-link"])
    assert result.exit_code == 0, result.output
    target = dest / "scitex-agent-container"
    assert target.is_dir()
    assert not target.is_symlink()
    assert (target / "01_alpha.md").is_file()


def test_install_replaces_existing_symlink(fake_skills_root, tmp_path):
    runner = CliRunner()
    dest = tmp_path / "dest"
    dest.mkdir()
    target = dest / "scitex-agent-container"
    # Pre-create a stale symlink to verify it's removed first.
    target.symlink_to(tmp_path / "nonexistent")
    result = runner.invoke(skills_group, ["install", "--dest", str(dest)])
    assert result.exit_code == 0, result.output
    assert target.is_symlink()
    assert (target / "01_alpha.md").is_file()


def test_install_replaces_existing_dir(fake_skills_root, tmp_path):
    runner = CliRunner()
    dest = tmp_path / "dest"
    target = dest / "scitex-agent-container"
    target.mkdir(parents=True)
    (target / "leftover.txt").write_text("old")
    result = runner.invoke(skills_group, ["install", "--dest", str(dest), "--no-link"])
    assert result.exit_code == 0, result.output
    assert not (target / "leftover.txt").exists()
    assert (target / "01_alpha.md").is_file()


def test_install_claude_symlink_real(fake_skills_root, tmp_path, sandbox_home):
    runner = CliRunner()
    dest = tmp_path / "dest"
    result = runner.invoke(
        skills_group,
        ["install", "--dest", str(dest), "--claude-symlink"],
    )
    assert result.exit_code == 0, result.output
    link = sandbox_home / "home" / ".claude" / "skills" / "scitex"
    assert link.is_symlink()


def test_install_claude_symlink_skips_when_exists_nonlink(
    fake_skills_root, tmp_path, sandbox_home
):
    runner = CliRunner()
    dest = tmp_path / "dest"
    link = sandbox_home / "home" / ".claude" / "skills" / "scitex"
    link.parent.mkdir(parents=True)
    link.mkdir()  # exists as a real dir, not a symlink
    result = runner.invoke(
        skills_group,
        ["install", "--dest", str(dest), "--claude-symlink"],
    )
    assert result.exit_code == 0, result.output
    assert "skipping" in result.output


def test_install_no_skills_dir(tmp_path):
    saved = sg._skills_root
    sg._skills_root = lambda: tmp_path / "missing"  # type: ignore[assignment]
    try:
        runner = CliRunner()
        result = runner.invoke(skills_group, ["install"])
    finally:
        sg._skills_root = saved  # type: ignore[assignment]
    assert result.exit_code != 0
    assert "no skills directory" in result.output
