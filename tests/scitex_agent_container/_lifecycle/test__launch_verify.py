"""``_lifecycle._launch_verify`` — the post-launch verdict (v4 step 1).

Pins the three-valued contract that ``sac agents start`` must never
report SUCC on an unverified launch (2026-08-14 outage: Errno 98 a2a
bind collision, VenvDistributionError boot refusal, SDK 1MiB stdio
frame kill — all died server-side while start printed SUCC):

* a FRESH heartbeat from the NEW incarnation → ``verified-up``;
* a dead container process → ``verified-failed`` carrying the boot-log
  tail (the real error text) AND the file it was read from;
* window expiry with the process still standing → ``unverified`` —
  distinct from failure, never collapsed.

NO MOCKS — real state dirs (tmp_path), the real ``write_heartbeat``
writer (JSON-only shape: no name/host, so no state.db rows), and tiny
real runtime doubles implementing ``is_running`` (the same seam style
``TuiSessionRuntime`` tests use). Each test: AAA markers, one
assertion, 3+-word name.
"""

from __future__ import annotations

import os
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Iterator

import pytest

from scitex_agent_container._lifecycle._launch_verify import (
    SKIPPED,
    UNVERIFIED,
    VERIFIED_FAILED,
    VERIFIED_UP,
    VERIFY_WINDOW_ENV,
    resolve_verify_window,
    verify_launch,
)
from scitex_agent_container._runners._session_state import write_heartbeat

# ---------------------------------------------------------------------------
# Real collaborators — no mocks.
# ---------------------------------------------------------------------------


class _AliveRuntime:
    """Real runtime double whose liveness probe says the process is up."""

    def is_running(self, config) -> bool:  # noqa: ARG002 - runtime seam shape
        return True


class _DeadRuntime:
    """Real runtime double whose liveness probe says the process is gone."""

    def is_running(self, config) -> bool:  # noqa: ARG002 - runtime seam shape
        return False


def _config(name: str = "verify-x") -> SimpleNamespace:
    return SimpleNamespace(name=name)


_ERRNO98 = (
    "ERROR: [Errno 98] Address already in use: ('127.0.0.1', 8631)\n"
    "OSError: [Errno 98] error while attempting to bind on address\n"
)


@pytest.fixture
def clean_window_env() -> Iterator[None]:
    """Save/restore ``SAC_START_VERIFY_WINDOW_S`` around each test."""
    saved = os.environ.get(VERIFY_WINDOW_ENV)
    os.environ.pop(VERIFY_WINDOW_ENV, None)
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop(VERIFY_WINDOW_ENV, None)
        else:
            os.environ[VERIFY_WINDOW_ENV] = saved


# ---------------------------------------------------------------------------
# verified-up — a fresh heartbeat from the new incarnation.
# ---------------------------------------------------------------------------


def test_fresh_heartbeat_verifies_up(clean_window_env, tmp_path: Path) -> None:
    """A beat stamped at/after launch is POSITIVE evidence of boot."""
    # Arrange — launch stamp taken FIRST, then the runner's boot beat.
    launched_at = time.time()
    write_heartbeat(tmp_path, pid=4242, state="starting")
    # Act
    verdict = verify_launch(
        _config(),
        _AliveRuntime(),
        launched_at=launched_at,
        window_s=2.0,
        poll_interval_s=0.05,
        state_dir=tmp_path,
    )
    # Assert
    assert verdict.status == VERIFIED_UP


def test_verified_up_names_the_heartbeat_file(
    clean_window_env, tmp_path: Path
) -> None:
    """The evidence FILE is named so the operator can go deeper."""
    # Arrange
    launched_at = time.time()
    write_heartbeat(tmp_path, pid=4242, state="starting")
    # Act
    verdict = verify_launch(
        _config(),
        _AliveRuntime(),
        launched_at=launched_at,
        window_s=2.0,
        poll_interval_s=0.05,
        state_dir=tmp_path,
    )
    # Assert
    assert verdict.heartbeat_path == str(tmp_path / "heartbeat.json")


def test_stale_heartbeat_is_not_evidence_of_up(
    clean_window_env, tmp_path: Path
) -> None:
    """A beat from BEFORE the launch belongs to the previous incarnation."""
    # Arrange — beat stamped 100s before this launch.
    write_heartbeat(tmp_path, pid=4242, state="idle", ts=time.time() - 100.0)
    launched_at = time.time()
    # Act
    verdict = verify_launch(
        _config(),
        _AliveRuntime(),
        launched_at=launched_at,
        window_s=0.2,
        poll_interval_s=0.05,
        state_dir=tmp_path,
    )
    # Assert
    assert verdict.status == UNVERIFIED


def test_old_run_stopping_farewell_is_not_evidence(
    clean_window_env, tmp_path: Path
) -> None:
    """On a --force cycle the OLD run's ``stopping`` beat can land after
    the launch stamp — it must not vouch for the new incarnation."""
    # Arrange
    launched_at = time.time()
    write_heartbeat(tmp_path, pid=4242, state="stopping")
    # Act
    verdict = verify_launch(
        _config(),
        _AliveRuntime(),
        launched_at=launched_at,
        window_s=0.2,
        poll_interval_s=0.05,
        state_dir=tmp_path,
    )
    # Assert
    assert verdict.status == UNVERIFIED


# ---------------------------------------------------------------------------
# verified-failed — the launched process died; the error must surface.
# ---------------------------------------------------------------------------


def test_dead_process_reports_verified_failed(
    clean_window_env, tmp_path: Path
) -> None:
    """No fresh beat + dead pid = a definitive failure, not a timeout."""
    # Arrange
    launched_at = time.time()
    # Act
    verdict = verify_launch(
        _config(),
        _DeadRuntime(),
        launched_at=launched_at,
        window_s=5.0,
        poll_interval_s=0.05,
        state_dir=tmp_path,
    )
    # Assert
    assert verdict.status == VERIFIED_FAILED


def test_dead_process_surfaces_boot_log_tail(
    clean_window_env, tmp_path: Path
) -> None:
    """The real error text (the Errno 98 line) rides on the verdict."""
    # Arrange — the apptainer runtime merges the runner's stderr into
    # <state>/stdout.log; that is where the bind collision text lands.
    (tmp_path / "stdout.log").write_text(_ERRNO98)
    launched_at = time.time()
    # Act
    verdict = verify_launch(
        _config(),
        _DeadRuntime(),
        launched_at=launched_at,
        window_s=5.0,
        poll_interval_s=0.05,
        state_dir=tmp_path,
    )
    # Assert
    assert "[Errno 98]" in verdict.log_tail


def test_dead_process_names_the_boot_log_file(
    clean_window_env, tmp_path: Path
) -> None:
    """The verdict names the FILE it read so the operator can go deeper."""
    # Arrange
    (tmp_path / "stdout.log").write_text(_ERRNO98)
    launched_at = time.time()
    # Act
    verdict = verify_launch(
        _config(),
        _DeadRuntime(),
        launched_at=launched_at,
        window_s=5.0,
        poll_interval_s=0.05,
        state_dir=tmp_path,
    )
    # Assert
    assert verdict.log_path == str(tmp_path / "stdout.log")


# ---------------------------------------------------------------------------
# unverified — the third value; never collapsed into either pole.
# ---------------------------------------------------------------------------


def test_window_expiry_reports_unverified_not_failed(
    clean_window_env, tmp_path: Path
) -> None:
    """A standing process with no beat is "could not verify" — a distinct
    verdict from "verified failed" (fleet constitution: no binary
    collapse)."""
    # Arrange
    launched_at = time.time()
    # Act
    verdict = verify_launch(
        _config(),
        _AliveRuntime(),
        launched_at=launched_at,
        window_s=0.2,
        poll_interval_s=0.05,
        state_dir=tmp_path,
    )
    # Assert
    assert verdict.status == UNVERIFIED


def test_unverified_verdict_flips_the_exit_code(
    clean_window_env, tmp_path: Path
) -> None:
    """``ok`` is False for UNVERIFIED — exit-nonzero like a failure."""
    # Arrange
    launched_at = time.time()
    # Act
    verdict = verify_launch(
        _config(),
        _AliveRuntime(),
        launched_at=launched_at,
        window_s=0.2,
        poll_interval_s=0.05,
        state_dir=tmp_path,
    )
    # Assert
    assert verdict.ok is False


# ---------------------------------------------------------------------------
# window configuration — explicit arg > env var > default; 0 disables.
# ---------------------------------------------------------------------------


def test_window_zero_skips_verification(clean_window_env, tmp_path: Path) -> None:
    """``--verify-window 0`` is the documented opt-out."""
    # Arrange
    launched_at = time.time()
    # Act
    verdict = verify_launch(
        _config(),
        _AliveRuntime(),
        launched_at=launched_at,
        window_s=0.0,
        state_dir=tmp_path,
    )
    # Assert
    assert verdict.status == SKIPPED


def test_env_var_overrides_default_window(clean_window_env) -> None:
    """``SAC_START_VERIFY_WINDOW_S`` is the env transport for the window."""
    # Arrange
    os.environ[VERIFY_WINDOW_ENV] = "12.5"
    # Act
    window = resolve_verify_window()
    # Assert
    assert window == 12.5


def test_malformed_env_window_fails_loud(clean_window_env) -> None:
    """A typo'd window must not silently become the default."""
    # Arrange
    os.environ[VERIFY_WINDOW_ENV] = "ninety"
    # Act
    raiser = pytest.raises(ValueError)
    # Assert
    with raiser:
        resolve_verify_window()


# ---------------------------------------------------------------------------
# apptainer FATAL must not be swallowed (operator instruction, 2026-08-20)
# ---------------------------------------------------------------------------

_APPTAINER_FATAL = (
    "INFO:    Converting SIF file to temporary sandbox...\n"
    "FATAL:   container creation failed: mount hook function failure: "
    "mount /home/ywatanabe/absent -> /absent error: "
    "while mounting /home/ywatanabe/absent: stat /home/ywatanabe/absent: "
    "no such file or directory\n"
)


def _write_boot_log(state_dir: Path, text: str, *, mtime: float | None = None) -> Path:
    """A real boot.stderr.log, optionally back-dated to a prior launch."""
    path = state_dir / "boot.stderr.log"
    path.write_text(text, encoding="utf-8")
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def test_apptainer_fatal_outranks_a_fresh_heartbeat(
    clean_window_env, tmp_path: Path
) -> None:
    """THE DEFECT THIS FIXES, in one test.

    Both signals are present: a beat stamped after launch AND a FATAL
    saying the container was never created. Before 2026-08-20 the beat
    won and `sac agents start` printed SUCC over a dead launch. A FATAL
    is positive evidence of failure; a beat is not proof of success —
    fleet-wide, all but one of ~118 beats were written by an OBSERVER,
    not by the runner.
    """
    # Arrange
    launched_at = time.time()
    write_heartbeat(tmp_path, pid=4242, state="ready")
    _write_boot_log(tmp_path, _APPTAINER_FATAL)
    # Act
    verdict = verify_launch(
        _config(),
        _AliveRuntime(),
        launched_at=launched_at,
        window_s=2.0,
        poll_interval_s=0.05,
        state_dir=tmp_path,
    )
    # Assert
    assert verdict.status == VERIFIED_FAILED


def test_apptainer_fatal_verdict_is_not_ok(clean_window_env, tmp_path: Path) -> None:
    """It must flip the caller exit code, which is what "fail loudly" means."""
    # Arrange
    launched_at = time.time()
    write_heartbeat(tmp_path, pid=4242, state="ready")
    _write_boot_log(tmp_path, _APPTAINER_FATAL)
    # Act
    verdict = verify_launch(
        _config(),
        _AliveRuntime(),
        launched_at=launched_at,
        window_s=2.0,
        poll_interval_s=0.05,
        state_dir=tmp_path,
    )
    # Assert
    assert verdict.ok is False


def test_apptainer_fatal_evidence_quotes_the_fatal_line(
    clean_window_env, tmp_path: Path
) -> None:
    """The operator gets the CAUSE, not just a verdict word."""
    # Arrange
    launched_at = time.time()
    write_heartbeat(tmp_path, pid=4242, state="ready")
    _write_boot_log(tmp_path, _APPTAINER_FATAL)
    # Act
    verdict = verify_launch(
        _config(),
        _AliveRuntime(),
        launched_at=launched_at,
        window_s=2.0,
        poll_interval_s=0.05,
        state_dir=tmp_path,
    )
    # Assert
    assert "mount hook function failure" in verdict.evidence


def test_apptainer_fatal_names_the_log_it_was_read_from(
    clean_window_env, tmp_path: Path
) -> None:
    """Naming the FILE is what lets someone go deeper than the tail."""
    # Arrange
    launched_at = time.time()
    write_heartbeat(tmp_path, pid=4242, state="ready")
    log = _write_boot_log(tmp_path, _APPTAINER_FATAL)
    # Act
    verdict = verify_launch(
        _config(),
        _AliveRuntime(),
        launched_at=launched_at,
        window_s=2.0,
        poll_interval_s=0.05,
        state_dir=tmp_path,
    )
    # Assert
    assert verdict.log_path == str(log)


def test_a_stale_fatal_from_a_previous_launch_is_ignored(
    clean_window_env, tmp_path: Path
) -> None:
    """THE GUARD THAT KEEPS THIS FIX FROM BECOMING WORSE THAN THE DEFECT.

    boot.stderr.log is not truncated per launch, so a FATAL from a start
    that failed yesterday is still on disk today. Without the mtime
    check, every subsequent start of that agent would fail forever —
    converting silent success into permanent silent failure. Only a log
    modified at or after ``launched_at`` testifies about THIS launch.
    """
    # Arrange
    launched_at = time.time()
    _write_boot_log(tmp_path, _APPTAINER_FATAL, mtime=launched_at - 3600.0)
    write_heartbeat(tmp_path, pid=4242, state="ready")
    # Act
    verdict = verify_launch(
        _config(),
        _AliveRuntime(),
        launched_at=launched_at,
        window_s=2.0,
        poll_interval_s=0.05,
        state_dir=tmp_path,
    )
    # Assert
    assert verdict.status == VERIFIED_UP


def test_a_clean_boot_log_still_verifies_up(clean_window_env, tmp_path: Path) -> None:
    """POSITIVE CONTROL — the check must not fail launches that are fine.

    Without this, a function that returned FATAL for every boot log would
    satisfy every test above and break every start in the fleet. Measured
    2026-08-20 before shipping: 50 boot logs across four hosts, ZERO
    containing a FATAL, so this is the arm that carries the whole fleet.
    """
    # Arrange
    launched_at = time.time()
    _write_boot_log(tmp_path, "INFO:    Converting SIF file to temporary sandbox...\n")
    write_heartbeat(tmp_path, pid=4242, state="ready")
    # Act
    verdict = verify_launch(
        _config(),
        _AliveRuntime(),
        launched_at=launched_at,
        window_s=2.0,
        poll_interval_s=0.05,
        state_dir=tmp_path,
    )
    # Assert
    assert verdict.status == VERIFIED_UP
