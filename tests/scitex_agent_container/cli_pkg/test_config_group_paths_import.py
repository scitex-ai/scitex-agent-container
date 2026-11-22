"""Real config_group CLI path controls, one observable outcome per test."""

from tests.scitex_agent_container.cli_pkg._config_path_cases import (
    command, native_case as native_case, paths as paths,
)

def test_import_reads_selected_native_source(native_case):
    # Arrange
    case = native_case
    for name, root in case.roots.items():
        root.mkdir()
        (root / "CLAUDE.md").write_text(name + "\n")
    flags = ["--claude-dir", str(case.target)] if case.selection == "explicit" else []
    dest = case.home / "imported"
    # Act
    command(case, "import", *flags, "--to", str(dest), "--yes")
    # Assert
    assert (dest / "AGENTS.md").read_text() == case.selection + "\n"


def test_import_preserves_all_native_inputs(native_case):
    # Arrange
    case = native_case
    for name, root in case.roots.items():
        root.mkdir()
        (root / "CLAUDE.md").write_text(name + "\n")
    flags = ["--claude-dir", str(case.target)] if case.selection == "explicit" else []
    dest = case.home / "imported"
    # Act
    command(case, "import", *flags, "--to", str(dest), "--yes")
    # Assert
    assert {name: (root / "CLAUDE.md").read_text() for name, root in case.roots.items()} == {name: name + "\n" for name in case.roots}



def test_import_reports_selected_source_success(native_case):
    # Arrange
    case = native_case
    for name, root in case.roots.items():
        root.mkdir()
        (root / "CLAUDE.md").write_text(name + "\n")
    flags = ["--claude-dir", str(case.target)] if case.selection == "explicit" else []
    # Act
    result = command(case, "import", *flags, "--to", str(case.home / "imported"), "--yes")
    # Assert
    assert result.exit_code == 0
