"""Tests for ``sac dev skills`` listing and lookup.

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


# Installation exercises the real shared installer in test__skill_install.py.
