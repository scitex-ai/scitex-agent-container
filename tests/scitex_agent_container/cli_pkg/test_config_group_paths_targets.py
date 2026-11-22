"""Real config_group CLI path controls, one observable outcome per test."""

from tests.scitex_agent_container.cli_pkg._config_path_cases import (
    command, layer, native_case as native_case, paths as paths,
)

def test_user_deploy_writes_selected_native_instructions(native_case):
    # Arrange
    case = native_case
    source = layer(case.home / "source", "selected-model")
    flags = ["--source", str(source), *case.flags]
    # Act
    command(case, "deploy", *flags, "--yes")
    # Assert
    assert (case.target / "CLAUDE.md").read_text() == "selected-model\n"


def test_user_deploy_leaves_other_native_paths_absent(native_case):
    # Arrange
    case = native_case
    source = layer(case.home / "source", "selected-model")
    flags = ["--source", str(source), *case.flags]
    # Act
    command(case, "deploy", *flags, "--yes")
    # Assert
    assert {p for p in case.roots.values() if p.exists()} == {case.target}


def test_user_check_reports_selected_target_in_sync(native_case):
    # Arrange
    case = native_case
    source = layer(case.home / "source", "selected-model")
    flags = ["--source", str(source), *case.flags]
    command(case, "deploy", *flags, "--yes")
    # Act
    result = command(case, "check", *flags)
    # Assert
    assert result.exit_code == 0


def test_user_check_exits_on_selected_native_drift(native_case):
    # Arrange
    case = native_case
    source = layer(case.home / "source", "selected-model")
    flags = ["--source", str(source), *case.flags]
    command(case, "deploy", *flags, "--yes")
    (case.target / "settings.json").write_text('{"model": "drifted"}')
    # Act
    result = command(case, "check", *flags)
    # Assert
    assert result.exit_code == 1


def test_user_check_names_selected_native_drift(native_case):
    # Arrange
    case = native_case
    source = layer(case.home / "source", "selected-model")
    flags = ["--source", str(source), *case.flags]
    command(case, "deploy", *flags, "--yes")
    (case.target / "settings.json").write_text('{"model": "drifted"}')
    # Act
    result = command(case, "check", *flags)
    # Assert
    assert str(case.target) in result.output



def test_user_deploy_reports_selected_target_success(native_case):
    # Arrange
    case = native_case
    source = layer(case.home / "source", "selected-model")
    # Act
    result = command(case, "deploy", "--source", str(source), *case.flags, "--yes")
    # Assert
    assert result.exit_code == 0
