"""Unit tests for the sealed agent's control-plane mask launcher.

The namespace work itself needs a container with user namespaces; it is
proven by tests/integration/test_sandbox_apptainer_live.py. These cover the
argv shape and which paths get covered.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container._sandbox._hide import (
    EXIT_HIDE_FAILED,
    HIDE_SCRIPT,
    HideError,
    covered_paths,
    hide_argv,
    main,
)


def test_hide_argv_runs_the_launcher_file_isolated_with_each_hidden_path():
    # Arrange
    hidden = ["/state/a", "/venv"]
    # Act
    argv = hide_argv("/py", hidden, ["hermes", "serve"])
    # Assert
    assert argv == [
        "/py", "-I", HIDE_SCRIPT, "--hide", "/state/a", "--hide", "/venv",
        "--", "hermes", "serve",
    ]  # fmt: skip


def test_launcher_script_is_this_modules_file():
    # Arrange
    import scitex_agent_container._sandbox._hide as hide_mod

    # Act
    script = Path(HIDE_SCRIPT)
    # Assert
    assert script == Path(hide_mod.__file__).resolve()


def test_absent_hidden_path_is_skipped(tmp_path):
    # Arrange
    absent = tmp_path / "nope"
    # Act
    paths = covered_paths([str(absent)])
    # Assert
    assert paths == []


def test_existing_hidden_directory_is_covered(tmp_path):
    # Arrange
    state = tmp_path / "state"
    state.mkdir()
    # Act
    paths = covered_paths([str(state)])
    # Assert
    assert paths == [state.resolve()]


def test_symlinked_hidden_path_covers_its_target(tmp_path):
    # Arrange
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    # Act
    paths = covered_paths([str(link)])
    # Assert
    assert paths == [real.resolve()]


def test_relative_hidden_path_is_refused():
    # Arrange
    relative = "state/a"
    # Act
    # Assert
    with pytest.raises(HideError, match="absolute"):
        covered_paths([relative])


def _sac_env(root):
    (root / "bin").mkdir(parents=True)
    (root / "bin" / "sac").write_text("#!/bin/sh\n")
    package = root / "lib" / "python3.12" / "site-packages" / "scitex_agent_container"
    package.mkdir(parents=True)
    return package


def test_hidden_sac_script_also_hides_its_package(tmp_path):
    # Arrange
    package = _sac_env(tmp_path / "env")
    script = tmp_path / "env" / "bin" / "sac"
    # Act
    paths = covered_paths([str(script)])
    # Assert
    assert package.resolve() in paths


def test_pythonpath_entry_carrying_sac_is_hidden(tmp_path):
    # Arrange
    package = tmp_path / "src" / "scitex_agent_container"
    package.mkdir(parents=True)
    # Act
    paths = covered_paths([], pythonpath=str(tmp_path / "src"))
    # Assert
    assert paths == [package.resolve()]


def test_path_inside_a_covered_directory_is_dropped(tmp_path):
    # Arrange
    _sac_env(tmp_path / "env")
    env = tmp_path / "env"
    # Act
    paths = covered_paths([str(env), str(env / "bin" / "sac")])
    # Assert
    assert paths == [env.resolve()]


def test_launcher_without_a_command_is_a_usage_error():
    # Arrange
    argv = ["--hide", "/state/a"]
    # Act
    # Assert
    with pytest.raises(SystemExit, match="2"):
        main(argv)


def test_launcher_refuses_a_relative_hidden_path():
    # Arrange
    argv = ["--hide", "relative", "--", "true"]
    # Act
    rc = main(argv)
    # Assert
    assert rc == EXIT_HIDE_FAILED
