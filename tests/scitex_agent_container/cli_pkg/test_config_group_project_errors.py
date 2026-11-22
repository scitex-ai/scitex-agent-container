"""Project config CLI boundaries outside Git, exercised with real owned paths."""

import pytest

from scitex_agent_container.cli_pkg.config_group import config_group
from tests.scitex_agent_container.cli_pkg._config_path_cases import layer, paths as paths


@pytest.fixture(params=[("show", "none"), ("show", "source"),
                        ("deploy", "none"), ("deploy", "source"),
                        ("deploy", "target"), ("check", "none"),
                        ("check", "source"), ("check", "target")])
def missing_project_case(paths, request):
    runner, home = paths
    verb, provided = request.param
    source = layer(home / "source", "explicit-project")
    target = home / "target"
    flags = {"none": [], "source": ["--source", str(source)],
             "target": ["--target", str(target)]}[provided]
    return runner, [verb, "--scope", "project", *flags], target


def test_project_missing_root_reports_usage_error(missing_project_case):
    # Arrange
    runner, args, _ = missing_project_case
    # Act
    result = runner.invoke(config_group, args)
    # Assert
    assert result.exit_code == 2


def test_project_missing_root_explains_explicit_path_pair(missing_project_case):
    # Arrange
    runner, args, _ = missing_project_case
    # Act
    result = runner.invoke(config_group, args)
    # Assert
    assert "--source and --target" in result.output


def test_project_missing_root_creates_no_native_target(missing_project_case):
    # Arrange
    runner, args, target = missing_project_case
    # Act
    runner.invoke(config_group, args)
    # Assert
    assert not target.exists()


def test_project_show_rejects_unsupported_target_option(paths):
    # Arrange
    runner, home = paths
    source = layer(home / "source", "explicit-project")
    # Act
    result = runner.invoke(config_group, ["show", "--scope", "project",
                           "--source", str(source), "--target", str(home / "target")],
                           standalone_mode=False)
    # Assert
    assert result.exception.option_name == "--target"
