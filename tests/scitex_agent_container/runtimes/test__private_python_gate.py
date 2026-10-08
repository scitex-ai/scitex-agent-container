"""The requested private Python must pass in the exact union before launch."""

import sys
from types import SimpleNamespace

import pytest

from scitex_agent_container.runtimes._private_python_gate import (
    PrivatePythonGateError,
    _run_probe,
    assert_private_python_runs,
    private_python_probe_argv,
)


def test_private_python_probe_argv_preserves_image_overlay_and_first_bind():
    # Arrange
    prefix = [
        "apptainer",
        "exec",
        "--overlay",
        "owned-overlay",
        "--bind",
        "owned-private:/uvwork:rw",
        "owned-image.sif",
    ]
    # Act
    probe = private_python_probe_argv([*prefix, "old-job", "private-argument"])
    # Assert
    assert (probe[: len(prefix)], probe[len(prefix) : len(prefix) + 2]) == (
        prefix,
        ["/bin/sh", "-c"],
    )


@pytest.mark.parametrize("result", [(1, b""), (255, b""), (0, b"wrong\n")])
def test_assert_private_python_runs_refuses_missing_unknown_or_wrong_receipt(result):
    # Arrange
    config = SimpleNamespace(python_venv="/uvwork/venv-agent")
    # Act
    refusal = pytest.raises(PrivatePythonGateError, match="actual launch")
    # Assert
    with refusal:
        assert_private_python_runs(
            config, ["apptainer", "exec", "owned.sif"], runner=lambda _: result
        )


def test_assert_private_python_runs_accepts_exact_completed_probe():
    # Arrange
    config = SimpleNamespace(python_venv="/uvwork/venv-agent")
    # Act
    result = assert_private_python_runs(
        config,
        ["apptainer", "exec", "owned.sif"],
        runner=lambda _: (0, b"SAC_PRIVATE_PYTHON_READY\n"),
    )
    # Assert
    assert result is None


def test_assert_private_python_runs_refuses_unprobeable_target():
    # Arrange
    config = SimpleNamespace(python_venv="/uvwork/venv-agent")
    # Act
    refusal = pytest.raises(PrivatePythonGateError, match="no launch image")
    # Assert
    with refusal:
        assert_private_python_runs(config, ["host-job"])


def test_assert_private_python_runs_does_not_probe_unrequested_private_environment():
    # Arrange
    config = SimpleNamespace(python_venv="")
    # Act
    result = assert_private_python_runs(config, ["host-job"])
    # Assert
    assert result is None


@pytest.mark.parametrize("code", [0, 1])
def test_run_probe_reaps_actual_private_interpreter_and_keeps_exit_code(code):
    # Arrange
    argv = [
        sys.executable,
        "-I",
        "-S",
        "-c",
        f'print("SAC_PRIVATE_PYTHON_READY"); raise SystemExit({code})',
    ]
    # Act
    result = _run_probe(argv)
    # Assert
    assert result == (code, b"SAC_PRIVATE_PYTHON_READY\n")
