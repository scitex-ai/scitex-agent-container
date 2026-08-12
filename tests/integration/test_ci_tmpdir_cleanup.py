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
from functools import lru_cache
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
    needle = "trap 'rm -rf \"${TMPDIR:?"
    # Act
    present = needle in script_text
    # Assert
    assert present, (
        "run-in-sif.sh must remove its own scratch dir on EXIT, via the guarded "
        "${TMPDIR:?} form. Without the trap each CI leg leaks ~2G and the runner "
        "filesystem fills (measured: 290G); without the guard the trap can fire "
        "on an empty path."
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


# ---------------------------------------------------------------------------
# The scratch path may never be deleted UNGUARDED.
#
# `rm -rf "$TMPDIR"` is one empty variable away from `rm -rf ""`, and that is NOT
# the harmless no-op it reads as. MEASURED on GNU coreutils 9.4: `-f` treats the
# empty operand as a nonexistent file, so the command exits 0 SILENTLY. Under
# `set -euo pipefail` nothing stops, and the script continues with TMPDIR="" —
# every later `"$TMPDIR/site"` is then `/site`, off the filesystem root.
#
# So the fix is `${TMPDIR:?}`, and the test below does not merely assert that the
# guarded spelling is present: it EXECUTES both spellings with the variable empty
# and observes the difference. The unguarded control is what makes the guarded
# case evidence rather than decoration — a guard only ever seen passing has not
# been shown to guard anything.
# ---------------------------------------------------------------------------
_WRAPPERS = ("run-in-sif.sh", "build-in-sif.sh", "publish-in-sif.sh")


def _wrapper(name: str) -> Path:
    return _SCRIPT.parent / name


@lru_cache(maxsize=None)
def _run(body: str) -> tuple[int, str, str]:
    """Run ``body`` under the wrappers' own `set -euo pipefail`, TMPDIR empty."""
    script = f'set -euo pipefail\nTMPDIR=""\n{body}\necho REACHED-NEXT-LINE\n'
    proc = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False
    )
    return proc.returncode, proc.stdout, proc.stderr


def _rm_lines(name: str) -> list[str]:
    """Every line of a wrapper that deletes the scratch path."""
    text = _wrapper(name).read_text(encoding="utf-8")
    return [
        line.strip()
        for line in text.splitlines()
        if "TMPDIR" in line and line.lstrip().startswith(("rm -rf", "trap 'rm -rf"))
    ]


# (wrapper, line) for every scratch deletion actually shipped, so each case below
# executes the REAL text of the REAL script rather than a paraphrase of it.
_DELETIONS = [(n, line) for n in _WRAPPERS for line in _rm_lines(n)]
_PLAIN = [c for c in _DELETIONS if not c[1].startswith("trap ")]


@pytest.mark.parametrize("name", _WRAPPERS)
def test_every_wrapper_still_deletes_its_scratch(name):
    # Arrange
    wrapper = name
    # Act
    lines = _rm_lines(wrapper)
    # Assert
    assert lines, f"{wrapper} deletes no scratch path — did the lifecycle move?"


@pytest.mark.parametrize(("name", "body"), _DELETIONS)
def test_every_scratch_deletion_carries_the_guard(name, body):
    # Arrange
    expected = "${TMPDIR:?"
    # Act
    guarded = expected in body
    # Assert
    assert guarded, (
        f"{name} deletes the scratch path without the guard: `{body}`. An empty "
        "TMPDIR makes `rm -rf` a SILENT success, and the script then addresses "
        "paths off the filesystem root."
    )


def test_unguarded_deletion_would_proceed_on_an_empty_path():
    """The CONTROL. Without this case the guarded tests prove nothing."""
    # Arrange
    body = 'rm -rf "$TMPDIR"'
    # Act
    returncode, stdout, _ = _run(body)
    # Assert
    assert (returncode, "REACHED-NEXT-LINE" in stdout) == (0, True), (
        'expected `rm -rf ""` to succeed silently and let the script carry on '
        f"(rc={returncode}, out={stdout!r}). If this ever fails, the platform's "
        "rm now rejects an empty operand and the hazard has changed shape — "
        "re-measure before weakening the guard."
    )


@pytest.mark.parametrize(("name", "body"), _DELETIONS)
def test_guarded_deletion_aborts_on_an_empty_path(name, body):
    # Arrange
    command = body
    # Act
    returncode, stdout, _ = _run(command)
    # Assert
    assert returncode != 0, (
        f"{name}: `{command}` did NOT abort on an empty TMPDIR "
        f"(rc=0, out={stdout!r})"
    )


@pytest.mark.parametrize(("name", "body"), _DELETIONS)
def test_guarded_deletion_names_the_variable_it_refused(name, body):
    # Arrange
    command = body
    # Act
    _, _, stderr = _run(command)
    # Assert
    assert "TMPDIR" in stderr, (
        f"{name}: `{command}` aborted without naming TMPDIR — whoever reads the "
        f"CI log needs the variable in the message. stderr={stderr!r}"
    )


@pytest.mark.parametrize(("name", "body"), _PLAIN)
def test_guarded_deletion_stops_execution(name, body):
    # Arrange
    command = body
    # Act
    _, stdout, _ = _run(command)
    # Assert
    assert "REACHED-NEXT-LINE" not in stdout, (
        f"{name}: `{command}` reported the refusal but execution continued"
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
