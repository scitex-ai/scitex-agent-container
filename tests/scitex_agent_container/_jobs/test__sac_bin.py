"""``sac_bin`` — the payload path a scheduled command must name.

The defect these guard against is not hypothetical. On 2026-08-20 every sac
JobSpec said a bare ``sac`` after an absolute ``/usr/bin/timeout`` head, which
takes the payload OUT of ``resolve_execstart`` (it absolutises only the first
token). On scitex-compute-01 the supervisor's PATH held no venv and all ten sac
jobs sat at exit 127 with zero successes.
"""

from __future__ import annotations

import os
import warnings
from pathlib import Path

import pytest

from scitex_agent_container._jobs._sac_bin import sac_bin


def _venv_shaped(tmp_path: Path, *, with_sac: bool) -> Path:
    """A real venv-shaped tree on disk: ``bin/python`` symlinked OUT of it.

    Not a mock — the property under test is how a SYMLINK is treated, so the
    test must build a real one. This mirrors how uv (the mandated installer)
    lays a venv out.
    """
    bindir = tmp_path / "bin"
    bindir.mkdir(parents=True)
    (bindir / "python").symlink_to(Path(os.path.realpath("/usr/bin/python3")))
    if with_sac:
        (bindir / "sac").write_text("#!/bin/sh\nexit 0\n")
    return bindir


def test_returns_the_console_script_beside_the_interpreter(tmp_path: Path) -> None:
    # Arrange
    bindir = _venv_shaped(tmp_path, with_sac=True)
    # Act
    resolved = sac_bin(executable=str(bindir / "python"))
    # Assert
    assert resolved == str(bindir / "sac")


def test_does_not_follow_the_interpreter_symlink_out_of_the_venv(tmp_path: Path) -> None:
    """The property scitex-dev's ``resolve_execstart`` docstring insists on.

    Falsifiable, and that is the point: change the implementation to
    ``Path(executable).resolve().parent`` and this goes RED, because the
    resolved parent is ``/usr/bin`` — a directory that structurally cannot hold
    this venv's console scripts.
    """
    # Arrange
    bindir = _venv_shaped(tmp_path, with_sac=True)
    # Act
    resolved = sac_bin(executable=str(bindir / "python"))
    # Assert
    assert not resolved.startswith("/usr/bin/")


def test_absent_console_script_warns(tmp_path: Path) -> None:
    """A silent fallback is how the original defect stayed invisible."""
    # Arrange
    bindir = _venv_shaped(tmp_path, with_sac=False)
    # Act
    executable = str(bindir / "python")
    # Assert
    with pytest.warns(RuntimeWarning, match="no `sac` console script"):
        sac_bin(executable=executable)


def test_absent_console_script_still_yields_a_usable_command(tmp_path: Path) -> None:
    """The warning does not replace a return value — the job must still render."""
    # Arrange
    bindir = _venv_shaped(tmp_path, with_sac=False)
    # Act
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        resolved = sac_bin(executable=str(bindir / "python"))
    # Assert
    assert resolved == "sac"


def test_resolves_against_this_interpreter_by_default() -> None:
    """No argument means ``sys.executable`` — the supervisor's own interpreter.

    Either an absolute sibling, or the loud bare-name fallback. What it must
    never be is a relative path WITH a directory in it, which would depend on
    the child's working directory as well as its PATH.
    """
    # Arrange
    acceptable_bare = "sac"
    # Act
    resolved = sac_bin()
    # Assert
    assert resolved == acceptable_bare or Path(resolved).is_absolute()
