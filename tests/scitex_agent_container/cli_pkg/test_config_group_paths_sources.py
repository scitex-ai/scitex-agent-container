"""Real config_group CLI path controls, one observable outcome per test."""

import json
from pathlib import Path

from tests.scitex_agent_container.cli_pkg._config_path_cases import (
    command, paths as paths, source_case as source_case,
)

def test_user_show_selects_expected_source(source_case):
    # Arrange
    case = source_case
    # Act
    result = command(case, "show", *case.flags, "--json")
    # Assert
    assert Path(json.loads(result.output)["root"]) == case.root


def test_user_show_omits_project_docs(source_case):
    # Arrange
    case = source_case
    # Act
    result = command(case, "show", *case.flags, "--json")
    # Assert
    assert "PROJECT.md" not in json.loads(result.output)["docs"]


def test_user_deploy_selects_instructions(source_case):
    # Arrange
    case = source_case
    # Act
    command(case, "deploy", *case.flags, "--target", str(case.target), "--yes")
    # Assert
    assert (case.target / "CLAUDE.md").read_text() == case.label + "\n"


def test_user_deploy_selects_model(source_case):
    # Arrange
    case = source_case
    # Act
    command(case, "deploy", *case.flags, "--target", str(case.target), "--yes")
    # Assert
    assert json.loads((case.target / "settings.json").read_text())["model"] == case.label


def test_user_deploy_selects_project_destination(source_case):
    # Arrange
    case = source_case
    # Act
    command(case, "deploy", *case.flags, "--target", str(case.target), "--yes")
    # Assert
    assert not (Path.cwd() / ".claude").exists()



def test_user_deploy_reports_selected_source_success(source_case):
    # Arrange
    case = source_case
    # Act
    result = command(case, "deploy", *case.flags, "--target", str(case.target), "--yes")
    # Assert
    assert result.exit_code == 0
