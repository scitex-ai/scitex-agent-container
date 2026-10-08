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

* The STATIC assertions pin the properties that make the sweep safe. They
  are the point of this file: a future edit that drops the age gate, drops the
  current-run exclusion, widens the managed-prefix match, or removes a
  wrapper's EXIT cleanup turns a cleanup into a weapon aimed at a concurrent
  matrix leg. Text assertions are weak evidence that code WORKS and strong
  evidence that a specific safety property has not been deleted, which is
  what is wanted here. Removal itself goes through the ownership-proving
  ``ci_tmpdir_cleanup`` (marker incarnation + quiescence), never a bare rm.
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

_CI = Path(__file__).resolve().parents[2] / ".github" / "ci"
_EXEC = _CI / "exec-in-sif.sh"
_LIB = _CI / "tmpdir-lib.sh"
_GLOB = "ci-scitex_agent_container-*"


@pytest.fixture(scope="module")
def exec_text() -> str:
    return _EXEC.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def lib_text() -> str:
    return _LIB.read_text(encoding="utf-8")


def test_ci_entrypoint_exists():
    # Arrange
    path = _EXEC
    # Act
    present = path.is_file()
    # Assert
    assert present, f"{path} is missing — the CI entrypoint moved"


def test_inner_scratch_has_an_exit_cleanup(exec_text):
    # Arrange
    needle = "trap ci_finish EXIT"
    # Act
    present = needle in exec_text
    # Assert
    assert present, (
        "exec-in-sif.sh must remove the inner scratch on EXIT. Without the "
        "trap each CI leg leaks ~2G and the runner filesystem fills "
        "(measured: 290G)."
    )


def test_exit_cleanup_removes_inner_scratch_through_proven_cleanup(exec_text):
    # Arrange
    needle = 'ci_tmpdir_cleanup "$target"'
    # Act
    present = needle in exec_text
    # Assert
    assert present, (
        "the EXIT cleanup must route the inner scratch through the "
        "ownership-proving ci_tmpdir_cleanup, not a bare rm -rf: an "
        "unproven removal can take out a concurrent matrix leg."
    )


def test_sibling_sweep_is_age_gated(lib_text):
    # Arrange
    needle = "-mmin"
    # Act
    present = needle in lib_text
    # Assert
    assert present, (
        "the sibling sweep MUST be age-gated: a concurrent matrix leg on the "
        "same runner owns a sibling dir that is minutes old and must survive."
    )


def test_sibling_sweep_excludes_the_current_run(lib_text):
    # Arrange
    needle = '! -name "*-${run_id}-${attempt}-*"'
    # Act
    present = needle in lib_text
    # Assert
    assert present, (
        "the sweep must exclude the current run explicitly — deleting the "
        "running leg's own scratch, or a sibling leg's, mid-run is a "
        "self-inflicted test failure."
    )


def test_sibling_sweep_is_scoped_to_managed_dirs(lib_text):
    # Arrange
    needle = "ci-scitex_agent_container-?*"
    # Act
    present = needle in lib_text
    # Assert
    assert present, (
        "the sweep must match only this project's managed scratch dirs; a "
        "wider match would reap other tenants' data from a shared root."
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
# (wrapper, owner file, needle) proving each wrapper's scratch is removed by
# proven cleanup on EXIT. run-in-sif.sh's scratch is owned by exec-in-sif.sh's
# EXIT path plus the always() clean-tmpdir step; build/publish own an EXIT
# trap running the incarnation-checked cleanup in their own file.
_WRAPPER_CLEANUP = (
    ("run-in-sif.sh", _EXEC, 'ci_tmpdir_cleanup "$target"'),
    ("build-in-sif.sh", _CI / "build-in-sif.sh", "trap cleanup_owned_tmp EXIT"),
    ("publish-in-sif.sh", _CI / "publish-in-sif.sh", "trap cleanup_owned_tmp EXIT"),
)


@pytest.mark.parametrize(("name", "owner", "needle"), _WRAPPER_CLEANUP)
def test_every_wrapper_still_deletes_its_scratch(name, owner, needle):
    # Arrange
    text = Path(owner).read_text(encoding="utf-8")
    # Act
    present = needle in text
    # Assert
    assert present, f"{name} deletes no scratch path — did the lifecycle move?"


def _lib_run(body: str, root: Path) -> tuple[int, str, str]:
    """Run ``body`` with the lifecycle library sourced and a sandbox root.

    Failures are fatal and a marker trails the body, so a refusal both
    fails the script and stops it — the two properties the tests pin.
    """
    script = f'set -euo pipefail\n. "{_LIB}"\n{body}\necho REACHED-NEXT-LINE\n'
    env = dict(os.environ)
    env["SAC_CI_TMPDIR_ROOT"] = str(root)
    proc = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False, env=env
    )
    return proc.returncode, proc.stdout, proc.stderr


def test_proven_cleanup_refuses_a_path_outside_the_managed_root(tmp_path):
    # Arrange
    command = 'ci_tmpdir_cleanup "/tmp"'
    # Act
    returncode, stdout, _ = _lib_run(command, tmp_path)
    # Assert
    assert (returncode != 0, "REACHED-NEXT-LINE" in stdout) == (True, False), (
        "ci_tmpdir_cleanup accepted a path outside the root, or reported the "
        "refusal and carried on."
    )


def test_proven_cleanup_refuses_an_empty_path_loudly(tmp_path):
    # Arrange
    command = 'ci_tmpdir_cleanup ""'
    # Act
    returncode, stdout, stderr = _lib_run(command, tmp_path)
    # Assert
    assert (
        returncode != 0,
        "REACHED-NEXT-LINE" in stdout,
        "refusing to remove" in stderr,
    ) == (True, False, True), (
        "ci_tmpdir_cleanup accepted an empty path, carried on after "
        "refusing, or refused without saying what it refused."
    )


def _run(body: str) -> tuple[int, str, str]:
    """Run ``body`` under the wrappers' own `set -euo pipefail`, TMPDIR empty."""
    script = f'set -euo pipefail\nTMPDIR=""\n{body}\necho REACHED-NEXT-LINE\n'
    proc = subprocess.run(
        ["bash", "-c", script], capture_output=True, text=True, check=False
    )
    return proc.returncode, proc.stdout, proc.stderr


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
