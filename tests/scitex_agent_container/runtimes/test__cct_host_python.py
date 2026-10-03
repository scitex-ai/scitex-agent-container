"""CCT's host child must use the interpreter bound into the selected launch."""

from types import SimpleNamespace

import pytest

from scitex_agent_container.runtimes._cct_host_python import (
    CctHostPythonBind,
    CctHostPythonError,
    capture_cct_host_python_bind,
    resolve_cct_host_python,
)


def _config(name="fixture"):
    return SimpleNamespace(name=name)


def _interpreter(root):
    python = root / "venv-agent/bin/python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nexit 0\n")
    python.chmod(0o700)
    return python


def test_actual_first_launch_bind_selects_host_interpreter(tmp_path):
    # Arrange
    selected = _interpreter(tmp_path / "explicit-private")
    other = _interpreter(tmp_path / "default-private")
    config = _config()
    binding = capture_cct_host_python_bind(
        config,
        [
            "apptainer",
            "exec",
            "--bind",
            f"{selected.parents[2]}:/uvwork:rw",
            "--bind",
            f"{other.parents[2]}:/uvwork:rw",
        ],
    )
    # Act
    actual = resolve_cct_host_python(config, "/uvwork/venv-agent/bin/python", binding)
    # Assert
    assert actual == str(selected)


def test_missing_bind_refuses_instead_of_guessing_scratch(tmp_path):
    # Arrange
    binding = capture_cct_host_python_bind(_config(), ["apptainer", "exec"])
    # Act
    # Assert
    with pytest.raises(CctHostPythonError, match="actual launch"):
        resolve_cct_host_python(_config(), "/uvwork/venv-agent/bin/python", binding)


def test_foreign_agent_binding_refuses(tmp_path):
    # Arrange
    root = tmp_path / "private"
    _interpreter(root)
    binding = CctHostPythonBind(agent="other", host_uvwork=root)
    # Act
    # Assert
    with pytest.raises(CctHostPythonError, match="another agent"):
        resolve_cct_host_python(_config(), "/uvwork/venv-agent/bin/python", binding)


@pytest.mark.parametrize("source", ["relative", "/scratch/../other", ""])
def test_unsafe_launch_bind_source_refuses(source):
    # Arrange
    argv = ["apptainer", "exec", "--bind", f"{source}:/uvwork:rw"]
    # Act
    # Assert
    with pytest.raises(CctHostPythonError, match="absolute"):
        capture_cct_host_python_bind(_config(), argv)


@pytest.mark.parametrize("shape", ["absent", "nonexecutable", "directory"])
def test_unavailable_interpreter_refuses(tmp_path, shape):
    # Arrange
    root = tmp_path / "private"
    python = root / "venv-agent/bin/python"
    python.parent.mkdir(parents=True)
    preparations = {
        "absent": lambda: None,
        "directory": python.mkdir,
        "nonexecutable": lambda: python.write_text("not executable"),
    }
    preparations[shape]()
    binding = CctHostPythonBind(agent="fixture", host_uvwork=root)
    # Act
    # Assert
    with pytest.raises(CctHostPythonError, match="executable"):
        resolve_cct_host_python(_config(), "/uvwork/venv-agent/bin/python", binding)


def test_absent_declaration_preserves_existing_environment():
    # Arrange
    config = _config()
    # Act
    actual = resolve_cct_host_python(config, None, None)
    # Assert
    assert actual is None


def test_host_literal_is_preserved_without_namespace_guess():
    # Arrange
    declared = "/declared/bin/python"
    # Act
    actual = resolve_cct_host_python(_config(), declared, None)
    # Assert
    assert actual == declared


@pytest.mark.parametrize(
    "declared", ["/uvwork/../foreign/bin/python", "/uvwork/other/bin/python"]
)
def test_other_container_interpreter_is_not_silently_translated(declared):
    # Arrange
    config = _config()
    # Act
    # Assert
    with pytest.raises(CctHostPythonError, match="contracted"):
        resolve_cct_host_python(config, declared, None)
