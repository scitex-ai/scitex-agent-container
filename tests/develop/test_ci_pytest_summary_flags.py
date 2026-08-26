#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""CI's short test summary must list FAILURES, not only skips.

2026-08-26. ``-rs`` was added to CI's pytest invocation so that skip reasons
would print: 308 PostgreSQL tests had been skipping on two of three runners for
want of a writable database, every one of those runs reported green, and
establishing it took reconstructing ``392 - 84 = 308`` by arithmetic from a
9.9 MB log because nothing in the output said which tests skipped or why.

That flag fixed one blind spot and opened its neighbour the same day. pytest's
``-r`` REPLACES the reported-character set rather than adding to it. The
default is ``fE``, so ``-rs`` means "skips ONLY" and DELETES the FAILED lines
from the short summary. Run 32943760311 reported ``2 failed, 17626 passed``
while ``grep FAILED`` over the entire 445 KB job log returned NOTHING, and the
two failing test ids had to be recovered from ``____ test ____`` traceback
headers instead.

A failure nobody can see is the same defect as a skip nobody can see, so the
reasoning that added ``s`` is the reasoning that requires keeping ``f`` and
``E``.

These tests hold that in place. One reads the flag out of the REAL CI script.
The others drive REAL pytest against a REAL failing test and prove the flag in
that script actually produces a FAILED line -- with a control proving the same
harness reports its ABSENCE under the old ``-rs``, because a gate that cannot
fail is not a gate.
"""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_CI_SCRIPT = _REPO_ROOT / ".github" / "ci" / "run-in-sif.sh"

# The characters whose meaning this test defends:
#   f -> FAILED lines in the short summary   (deleted by a bare -rs)
#   E -> ERROR lines for collection/fixture failures
#   s -> SKIPPED lines with their reasons    (the 2026-08-26 addition)
_REQUIRED_REPORT_CHARS = ("f", "E", "s")

# What a summary that reports failures looks like, and what it looks like when
# the characters have been trimmed back.
_FAILED_MARKER = "FAILED"
_SKIPPED_MARKER = "SKIPPED"


def _ci_pytest_report_flag() -> str:
    """Return the ``-r<chars>`` argument CI passes to pytest, e.g. ``rfEs``.

    Read out of the real script rather than duplicated here, so trimming the
    flag in ``run-in-sif.sh`` turns these tests red instead of leaving a
    description that agrees with itself.
    """
    text = _CI_SCRIPT.read_text()
    invocation = [
        line
        for line in text.splitlines()
        if "python -m pytest tests/" in line and not line.lstrip().startswith("#")
    ]
    assert len(invocation) == 1, (
        f"expected exactly one uncommented pytest invocation in {_CI_SCRIPT}, "
        f"found {len(invocation)}: {invocation!r}"
    )
    found = re.findall(r"(?<!\S)-(r[a-zA-Z]+)(?!\S)", invocation[0])
    assert len(found) == 1, (
        f"expected exactly one -r flag in CI's pytest invocation, found {found!r} "
        f"in: {invocation[0].strip()!r}"
    )
    return found[0]


def _run_pytest_with(report_flag: str, workdir: Path) -> str:
    """Drive REAL pytest over a REAL failing test and return its output.

    ``-c`` points at an ini inside ``workdir`` so the repo's own configuration
    (and its ``required_plugins``) cannot influence the result, and so rootdir
    discovery cannot wander back into the repository.
    """
    ini = workdir / "pytest.ini"
    ini.write_text("[pytest]\n")
    (workdir / "test_subject.py").write_text(
        "import pytest\n"
        "\n"
        "def test_that_fails():\n"
        "    assert False\n"
        "\n"
        '@pytest.mark.skip(reason="deliberately skipped")\n'
        "def test_that_skips():\n"
        "    pass\n"
    )
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            f"-{report_flag}",
            "-c",
            str(ini),
            "-p",
            "no:cacheprovider",
            str(workdir / "test_subject.py"),
        ],
        capture_output=True,
        text=True,
        cwd=str(workdir),
    )
    output = proc.stdout + proc.stderr
    # A run that never executed the subject proves nothing about the flag.
    assert "1 failed" in output, (
        f"the subject test did not run as expected under -{report_flag}; "
        f"output was:\n{output}"
    )
    return output


def test_ci_pytest_invocation_reports_failures_errors_and_skips() -> None:
    """The flag in the real CI script carries f, E and s."""
    # Arrange
    flag = _ci_pytest_report_flag()
    # Act
    missing = [char for char in _REQUIRED_REPORT_CHARS if char not in flag[1:]]
    # Assert
    assert not missing, (
        f"CI's pytest invocation uses -{flag}, which drops {missing!r}. "
        "pytest's -r REPLACES the default 'fE' rather than adding to it, so "
        "asking only for skips deletes the FAILED lines from the summary "
        "(run 32943760311: '2 failed' with zero greppable FAILED lines)."
    )


def test_the_flag_ci_actually_uses_produces_a_failed_line(tmp_path: Path) -> None:
    """Exercise the real flag against a real failure, not just its spelling."""
    # Arrange
    flag = _ci_pytest_report_flag()
    # Act
    output = _run_pytest_with(flag, tmp_path)
    # Assert
    assert _FAILED_MARKER in output, (
        "CI's own report flag did not produce a FAILED line in the short "
        f"summary; output was:\n{output}"
    )


def test_the_flag_ci_actually_uses_still_produces_a_skipped_line(
    tmp_path: Path,
) -> None:
    """The 2026-08-26 skip-visibility fix must survive this change."""
    # Arrange
    flag = _ci_pytest_report_flag()
    # Act
    output = _run_pytest_with(flag, tmp_path)
    # Assert
    assert _SKIPPED_MARKER in output, (
        "CI's own report flag did not produce a SKIPPED line, so the "
        f"skip-visibility fix has been lost; output was:\n{output}"
    )


def test_the_old_flag_still_reported_skips(tmp_path: Path) -> None:
    """Half of the control: ``-rs`` was never wrong about skips."""
    # Arrange
    old_flag = "rs"
    # Act
    output = _run_pytest_with(old_flag, tmp_path)
    # Assert
    assert _SKIPPED_MARKER in output, (
        f"-rs should still report skips; output was:\n{output}"
    )


def test_the_old_flag_hides_failures_so_this_gate_can_fail(tmp_path: Path) -> None:
    """The control proper: under ``-rs`` the FAILED line really is absent.

    Without this, the tests above could pass on a pytest that always prints
    FAILED regardless of the flag -- in which case they would be describing a
    property nothing enforces. This proves the harness can tell them apart.
    """
    # Arrange
    old_flag = "rs"
    # Act
    output = _run_pytest_with(old_flag, tmp_path)
    # Assert
    assert _FAILED_MARKER not in output, (
        "-rs unexpectedly produced a FAILED line, so this control no longer "
        "distinguishes the two flags and the gate above proves nothing. "
        f"pytest {pytest.__version__} output was:\n{output}"
    )
