"""``sac_bin`` — the payload path a scheduled command must name.

The defect these guard against is not hypothetical. On 2026-08-20 every sac
JobSpec said a bare ``sac`` after an absolute ``/usr/bin/timeout`` head, which
takes the payload OUT of ``resolve_execstart`` (it absolutises only the first
token). On scitex-compute-01 the supervisor's PATH held no venv and all ten sac
jobs sat at exit 127 with zero successes.
"""

from __future__ import annotations

import os
import sys
import warnings
from pathlib import Path

import pytest

from scitex_agent_container._jobs._jobs_plugin import provide_jobs
from scitex_agent_container._jobs._sac_bin import sac_bin

from ._jobspec_helpers import _split_command


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


# ---------------------------------------------------------------------------
# THE POPULATION GUARD: sac_bin's resolution must REACH the rendered specs.
#
# The tests above pin `sac_bin` in isolation. These pin the thing the module
# exists for -- that every JobSpec the provider returns names the resolved
# payload rather than a bare `sac` the ambient PATH has to find.
#
# They live in this file rather than a `test__specs_payload.py` of their own
# because the repo mirrors tests onto source modules one-to-one (PS-204 §2),
# and their subject IS this module: `_sac_bin`'s whole docstring is the
# argument for why a job command may not say bare `sac`.
#
# WHY THEY TAKE THE `executable` SEAM. `sac_bin` returns the bare name and
# warns when no console script sits beside the running interpreter -- correct
# for a broken install, and also exactly what a PYTHONPATH-only CI run looks
# like. Calling `provide_jobs()` with no seam and asserting the payload is
# absolute therefore asserts a fact about the RUNNING ENVIRONMENT, not about
# the specs. MEASURED 2026-08-20: that is why this guard was red in CI on
# three unrelated PRs while every host behaved correctly.
#
# WHY NOT "absolute OR a warning fired". A warning ALWAYS fires under CI, so
# every job -- including one that hardcoded a literal `sac` -- would satisfy
# that vacuously, in the one environment where the guard actually runs.
# ---------------------------------------------------------------------------


def _payload(command: str) -> str:
    """The token after the ``/usr/bin/timeout N`` head — the binary that runs.

    Reads it through the shared ``_split_command`` rather than re-slicing the
    string here, so a change to what counts as the head reaches this file too.
    See that helper for why the payload is checked by SHAPE and never against
    ``sac_bin()``'s own return value.
    """
    return _split_command(command)[1]


def test_every_job_names_an_absolute_payload_not_a_bare_sac(tmp_path: Path) -> None:
    """The whole-fleet guard, and the one that would have caught compute-01.

    Each per-job assertion elsewhere pins ONE job, so a JobSpec added tomorrow
    with a bare ``sac`` would pass every one of them. This covers the
    population instead.
    """
    # Arrange — a tree that DOES hold a console script, so resolution is the
    # test's choice rather than the runner's accident.
    bindir = _venv_shaped(tmp_path, with_sac=True)
    # Act
    relative = [
        (job.name, job.command)
        for job in provide_jobs(executable=str(bindir / "python"))
        if not Path(_payload(job.command)).is_absolute()
    ]
    # Assert
    assert relative == [], (
        f"these commands name a payload that must be found on the ambient "
        f"PATH: {relative}. `resolve_execstart` absolutises only the FIRST "
        f"token, and that token is already `/usr/bin/timeout`, so nothing "
        f"downstream will fix this."
    )


def test_the_payload_guard_has_something_to_guard(tmp_path: Path) -> None:
    """Non-vacuity: an empty provider would pass the check above silently."""
    # Arrange
    expected_minimum = 1
    bindir = _venv_shaped(tmp_path, with_sac=True)
    # Act
    jobs = provide_jobs(executable=str(bindir / "python"))
    # Assert
    assert len(jobs) >= expected_minimum


def test_every_payload_comes_from_the_tree_the_seam_named(tmp_path: Path) -> None:
    """The seam is REACHED, not merely accepted.

    Absoluteness alone cannot tell those apart: if ``provide_jobs`` ignored
    ``executable`` and resolved against the running interpreter, the payload
    would still be absolute on a developer's machine and the guard above would
    still pass. This pins the payload to the tree the test built, so a seam
    that is accepted and never threaded down goes red — the
    accepted-but-never-applied shape that lets a flag exist while the
    behaviour behind it is absent.
    """
    # Arrange
    bindir = _venv_shaped(tmp_path, with_sac=True)
    # Act
    jobs = provide_jobs(executable=str(bindir / "python"))
    # Assert
    elsewhere = [
        (job.name, _payload(job.command))
        for job in jobs
        if _payload(job.command) != str(bindir / "sac")
    ]
    assert elsewhere == [], (
        f"these payloads did not come from the tree the seam named "
        f"({bindir / 'sac'}): {elsewhere}. `executable` was accepted but not "
        f"threaded through to `sac_bin`."
    )


def test_specs_built_without_a_console_script_warn(tmp_path: Path) -> None:
    """What does NOT go red, made to go red — half one, the noise.

    The guard above is only meaningful while the fallback it guards against
    stays AUDIBLE. If the warning ever stopped, a silently degraded fleet
    would look identical to a healthy one.
    """
    # Arrange
    bindir = _venv_shaped(tmp_path, with_sac=False)
    # Act
    executable = str(bindir / "python")
    # Assert
    with pytest.warns(RuntimeWarning):
        provide_jobs(executable=executable)


def test_specs_built_without_a_console_script_carry_the_bare_name(
    tmp_path: Path,
) -> None:
    """What does NOT go red, made to go red — half two, the value.

    The fallback returns the bare name rather than inventing a path. If it
    ever started synthesising one, the population guard would pass against a
    payload no host can execute — a green test describing a fleet at exit 127.
    """
    # Arrange
    bindir = _venv_shaped(tmp_path, with_sac=False)
    # Act
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        jobs = provide_jobs(executable=str(bindir / "python"))
    # Assert
    assert {_payload(job.command) for job in jobs} == {"sac"}


def test_the_default_seam_still_resolves_against_this_interpreter() -> None:
    """Omitting the seam must keep production behaviour, not become a no-op.

    The fleet calls ``provide_jobs()`` with no arguments and must still get the
    console script beside the interpreter that imported the plugin. Asserting
    the exact path would parameterise the test by the value under test, so this
    asserts the RELATIONSHIP: whatever the default resolves to is what the seam
    produces when handed this interpreter.
    """
    # Arrange
    interpreter = sys.executable
    # Act
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)
        defaulted = [_payload(job.command) for job in provide_jobs()]
        seamed = [_payload(job.command) for job in provide_jobs(executable=interpreter)]
    # Assert
    assert defaulted == seamed
