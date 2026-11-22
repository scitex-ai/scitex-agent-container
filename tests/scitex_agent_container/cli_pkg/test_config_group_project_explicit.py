"""Explicit source/target pair permits project config without a Git root."""

import pytest

from scitex_agent_container.cli_pkg.config_group import config_group
from tests.scitex_agent_container.cli_pkg._config_path_cases import layer, paths as paths


@pytest.fixture
def explicit_project(paths):
    runner, home = paths
    source = layer(home / "source", "explicit-project")
    target = home / "target"
    flags = ["--scope", "project", "--source", str(source), "--target", str(target)]
    return runner, flags, target


def test_project_explicit_preview_succeeds_without_git(explicit_project):
    # Arrange
    runner, flags, _ = explicit_project
    # Act
    result = runner.invoke(config_group, ["deploy", *flags])
    # Assert
    assert result.exit_code == 0


def test_project_explicit_preview_creates_no_target(explicit_project):
    # Arrange
    runner, flags, target = explicit_project
    # Act
    runner.invoke(config_group, ["deploy", *flags])
    # Assert
    assert not target.exists()


def test_project_explicit_check_reports_initial_drift(explicit_project):
    # Arrange
    runner, flags, _ = explicit_project
    # Act
    result = runner.invoke(config_group, ["check", *flags])
    # Assert
    assert result.exit_code == 1


def test_project_explicit_apply_succeeds_without_git(explicit_project):
    # Arrange
    runner, flags, _ = explicit_project
    # Act
    result = runner.invoke(config_group, ["deploy", *flags, "--yes"])
    # Assert
    assert result.exit_code == 0


def test_project_explicit_apply_writes_selected_instructions(explicit_project):
    # Arrange
    runner, flags, target = explicit_project
    # Act
    runner.invoke(config_group, ["deploy", *flags, "--yes"])
    # Assert
    assert (target / "CLAUDE.md").read_text() == "explicit-project\n"


def test_project_explicit_check_reads_applied_target(explicit_project):
    # Arrange
    runner, flags, _ = explicit_project
    runner.invoke(config_group, ["deploy", *flags, "--yes"])
    # Act
    result = runner.invoke(config_group, ["check", *flags])
    # Assert
    assert result.exit_code == 0
