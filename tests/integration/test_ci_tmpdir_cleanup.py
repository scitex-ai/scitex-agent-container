"""The CI scratch directory must clean itself up — and the sweep must be safe.

WHY THIS EXISTS. ``.github/ci/run-in-sif.sh`` exports a per-leg scratch dir
``/tmp/ci-scitex_agent_container-<run_id>-<attempt>-<pyver>`` and, before this
guard, only ever ``rm -rf``'d it at START. That start-time cleanup can never
remove the thing that accumulates: the name it cleans is the name it is about to
use, and every new run carries a new ``GITHUB_RUN_ID``.

MEASURED 2026-08-09 on scitex-compute-04: 153 orphaned directories, 1.8-2.2G
each, ~290G total, root filesystem at 393G/393G with 0 bytes free. Every writing
test then failed with ``fatal: failed to write commit object`` on EVERY pull
request regardless of its diff — which reads as a shared-runner fault and is not
one — and ``sac listen`` began returning HTTP 500 because it could not write its
audit log.

TWO HALVES, and they guard different things:

* The STATIC assertions pin the three properties that make the sweep safe. They
  are the point of this file: a future edit that drops the age gate, drops the
  current-dir exclusion, or widens the name glob turns a cleanup into a weapon
  aimed at a concurrent matrix leg. Text assertions are weak evidence that code
  WORKS and strong evidence that a specific safety property has not been
  deleted, which is what is wanted here.
* The BEHAVIOURAL test executes the sweep semantics against a sandbox root so
  the ``find`` predicates are exercised for real. It MIRRORS the command rather
  than invoking the script, because running the script itself requires the CI
  SIF; the static half is what keeps the mirror honest.
"""

from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / ".github" / "ci" / "run-in-sif.sh"
_GLOB = "ci-scitex_agent_container-*"


@pytest.fixture(scope="module")
def script_text() -> str:
    return _SCRIPT.read_text(encoding="utf-8")


def test_ci_script_exists():
    # Arrange
    path = _SCRIPT
    # Act
    present = path.is_file()
    # Assert
    assert present, f"{path} is missing — the CI entrypoint moved"


def test_scratch_dir_is_removed_on_exit(script_text):
    # Arrange
    needle = 'trap \'rm -rf "$TMPDIR"\' EXIT'
    # Act
    present = needle in script_text
    # Assert
    assert present, (
        "run-in-sif.sh must remove its own scratch dir on EXIT. Without it each "
        "CI leg leaks ~2G and the runner filesystem fills (measured: 290G)."
    )


def test_sibling_sweep_is_age_gated(script_text):
    # Arrange
    needle = "-mmin"
    # Act
    present = needle in script_text
    # Assert
    assert present, (
        "the sibling sweep MUST be age-gated: a concurrent matrix leg on the "
        "same runner owns a sibling dir that is minutes old and must survive."
    )


def test_sibling_sweep_excludes_the_current_scratch_dir(script_text):
    # Arrange
    needle = '! -path "$TMPDIR"'
    # Act
    present = needle in script_text
    # Assert
    assert present, (
        "the sweep must exclude $TMPDIR explicitly — deleting the running "
        "leg's own scratch mid-run is a self-inflicted test failure."
    )


def test_sibling_sweep_is_scoped_to_this_projects_dirs(script_text):
    # Arrange
    needle = "-name 'ci-scitex_agent_container-*'"
    # Act
    present = needle in script_text
    # Assert
    assert present, (
        "the sweep must match only this project's scratch dirs; a wider glob "
        "would reap other tenants' data from a shared /tmp."
    )


def _sweep(root: Path, current: Path, age_min: int = 360) -> None:
    """Run the sweep's find(1) semantics against ``root``."""
    subprocess.run(
        [
            "find", str(root), "-maxdepth", "1", "-type", "d",
            "-name", _GLOB,
            "-mmin", f"+{age_min}",
            "!", "-path", str(current),
            "-exec", "rm", "-rf", "{}", "+",
        ],
        check=False,
        capture_output=True,
    )


@pytest.fixture
def sweep_sandbox(tmp_path):
    """Four dirs: stale-ours, fresh-ours, current, stale-not-ours."""
    stale = tmp_path / "ci-scitex_agent_container-111-0-3.12"
    fresh = tmp_path / "ci-scitex_agent_container-222-0-3.13"
    current = tmp_path / "ci-scitex_agent_container-999-0-3.12"
    other = tmp_path / "unrelated-scratch"
    for d in (stale, fresh, current, other):
        d.mkdir()
    old = time.time() - 10 * 3600
    os.utime(stale, (old, old))
    os.utime(other, (old, old))
    return {
        "root": tmp_path, "stale": stale, "fresh": fresh,
        "current": current, "other": other,
    }


def test_sweep_reaps_a_stale_sibling(sweep_sandbox):
    # Arrange
    box = sweep_sandbox
    # Act
    _sweep(box["root"], box["current"])
    # Assert
    assert not box["stale"].exists()


def test_sweep_spares_a_concurrent_matrix_leg(sweep_sandbox):
    # Arrange
    box = sweep_sandbox
    # Act
    _sweep(box["root"], box["current"])
    # Assert
    assert box["fresh"].exists()


def test_sweep_spares_the_current_scratch_dir(sweep_sandbox):
    # Arrange
    box = sweep_sandbox
    # Act
    _sweep(box["root"], box["current"])
    # Assert
    assert box["current"].exists()


def test_sweep_spares_directories_that_are_not_ours(sweep_sandbox):
    # Arrange
    box = sweep_sandbox
    # Act
    _sweep(box["root"], box["current"])
    # Assert
    assert box["other"].exists()
