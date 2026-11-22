"""Real config_group CLI path controls, one observable outcome per test."""

import json
from pathlib import Path

from tests.scitex_agent_container.cli_pkg._config_path_cases import (
    command, paths as paths, project_case as project_case,
)

def test_project_show_selects_nearest_root(project_case):
    # Arrange
    case = project_case
    # Act
    result = command(case, "show", "--scope", "project", "--json")
    # Assert
    assert Path(json.loads(result.output)["root"]) == case.source


def test_project_deploy_instructions(project_case):
    # Arrange
    case = project_case
    # Act
    command(case, "deploy", "--scope", "project", "--yes")
    # Assert
    assert (case.target / "CLAUDE.md").read_text() == "project-model\n"


def test_project_deploy_ignores_native_user_override(project_case):
    # Arrange
    case = project_case
    # Act
    command(case, "deploy", "--scope", "project", "--yes")
    # Assert
    assert not case.decoy.exists()


def test_project_deploy_preserves_user_source(project_case):
    # Arrange
    case = project_case
    # Act
    command(case, "deploy", "--scope", "project", "--yes")
    # Assert
    assert (case.user / "AGENTS.md").read_text() == "user-model\n"


def test_project_check_reports_selected_target_in_sync(project_case):
    # Arrange
    case = project_case
    command(case, "deploy", "--scope", "project", "--yes")
    # Act
    result = command(case, "check", "--scope", "project")
    # Assert
    assert result.exit_code == 0



def test_project_deploy_reports_selected_scope_success(project_case):
    # Arrange
    case = project_case
    # Act
    result = command(case, "deploy", "--scope", "project", "--yes")
    # Assert
    assert result.exit_code == 0
