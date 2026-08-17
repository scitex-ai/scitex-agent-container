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
