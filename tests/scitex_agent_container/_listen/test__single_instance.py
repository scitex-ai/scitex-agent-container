"""Single-instance guard for ``sac listen`` (operator task #26 sub (1)).

Background: today the host's ``sac listen`` was found DOWN with nothing
restarting it, and starting a second instance while one already holds
``127.0.0.1:7878`` causes a hard EADDRINUSE crash in uvicorn. The
crash is loud (good) but the operator has no diagnostic about which
process holds the port — they get a Python traceback, not a clean
message naming the holding PID.

Fix: a flock-backed pidfile guard. The flock is the source of truth
(atomic, kernel-released on process death — survives SIGKILL); the
pidfile holds the diagnostic. On conflict, the caller gets a
:class:`ListenAlreadyRunningError` naming the holding PID + the
remedy.

No-mocks (PA-306): real ``os.open`` / ``fcntl.flock`` against a real
tmp-path lock dir. Tests inject the lock dir via a fixture rather
than touching ``$HOME``. Each test: AAA markers (TQ002), one
assertion (TQ007), 3+-word name.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Iterator

import pytest
from scitex_agent_container._listen._single_instance import (
    ListenAlreadyRunningError,
    acquire_listen_lock,
    release_listen_lock,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def lock_dir(tmp_path: Path) -> Path:
    """Isolated lock dir; each test gets a fresh one."""
    d = tmp_path / "runtime"
    d.mkdir()
    return d


# ---------------------------------------------------------------------------
# Acquire — happy path
# ---------------------------------------------------------------------------


def test_acquire_returns_open_fd_for_the_lock(lock_dir: Path) -> None:
    # Arrange
    port = 7878
    # Act
    handle = acquire_listen_lock(port=port, lock_dir=lock_dir)
    # Assert
    try:
        assert handle.fd >= 0
    finally:
        release_listen_lock(handle)


def test_acquire_writes_current_pid_into_pidfile(lock_dir: Path) -> None:
    # Arrange — first acquire wins.
    port = 7878
    # Act
    handle = acquire_listen_lock(port=port, lock_dir=lock_dir)
    try:
        body = (lock_dir / f"listen-{port}.pid").read_text().strip()
    finally:
        release_listen_lock(handle)
    # Assert
    assert int(body) == os.getpid()


def test_acquire_uses_port_specific_lock_path(lock_dir: Path) -> None:
    # Arrange — two different ports must not collide; both can acquire.
    handle1 = acquire_listen_lock(port=7878, lock_dir=lock_dir)
    # Act
    handle2 = acquire_listen_lock(port=7879, lock_dir=lock_dir)
    # Assert
    try:
        assert (lock_dir / "listen-7878.pid").is_file() and (
            lock_dir / "listen-7879.pid"
        ).is_file()
    finally:
        release_listen_lock(handle1)
        release_listen_lock(handle2)


# ---------------------------------------------------------------------------
# Conflict — second acquire fails loudly with the holder's PID
# ---------------------------------------------------------------------------


def test_second_acquire_raises_listen_already_running_error(
    lock_dir: Path,
) -> None:
    # Arrange — first acquire holds the flock; second must fail loud.
    port = 7878
    h1 = acquire_listen_lock(port=port, lock_dir=lock_dir)
    raised = False
    # Act
    try:
        h2 = acquire_listen_lock(port=port, lock_dir=lock_dir)
        release_listen_lock(h2)  # defensive — should never reach here
    except ListenAlreadyRunningError:
        raised = True
    finally:
        release_listen_lock(h1)
    # Assert
    assert raised is True


def test_conflict_error_message_names_holding_pid(lock_dir: Path) -> None:
    # Arrange — the deny path's value to the operator is naming the
    # process that is blocking them. The error message must carry the
    # holding PID so `kill <pid>` is actionable without grep.
    port = 7878
    h1 = acquire_listen_lock(port=port, lock_dir=lock_dir)
    captured_msg = ""
    # Act
    try:
        try:
            h2 = acquire_listen_lock(port=port, lock_dir=lock_dir)
            release_listen_lock(h2)
        except ListenAlreadyRunningError as exc:
            captured_msg = str(exc)
    finally:
        release_listen_lock(h1)
    # Assert
    assert str(os.getpid()) in captured_msg


def test_conflict_error_message_names_lock_path(lock_dir: Path) -> None:
    # Arrange — operator needs to find the lock file on disk to
    # diagnose / clear stale state. Error message MUST name it.
    port = 7878
    h1 = acquire_listen_lock(port=port, lock_dir=lock_dir)
    captured_msg = ""
    # Act
    try:
        try:
            h2 = acquire_listen_lock(port=port, lock_dir=lock_dir)
            release_listen_lock(h2)
        except ListenAlreadyRunningError as exc:
            captured_msg = str(exc)
    finally:
        release_listen_lock(h1)
    # Assert
    assert f"listen-{port}.pid" in captured_msg


# ---------------------------------------------------------------------------
# Release — clean re-acquire after release
# ---------------------------------------------------------------------------


def test_release_lets_subsequent_acquire_succeed(lock_dir: Path) -> None:
    # Arrange — first acquire + clean release; the next acquire must
    # NOT raise ListenAlreadyRunningError (the flock is gone).
    port = 7878
    h1 = acquire_listen_lock(port=port, lock_dir=lock_dir)
    release_listen_lock(h1)
    # Act
    h2 = acquire_listen_lock(port=port, lock_dir=lock_dir)
    # Assert
    try:
        assert h2.fd >= 0
    finally:
        release_listen_lock(h2)


# ---------------------------------------------------------------------------
# Stale pidfile — kernel-released flock after dead-process exit
# ---------------------------------------------------------------------------


def test_stale_pidfile_with_no_flock_holder_can_be_reacquired(
    lock_dir: Path,
) -> None:
    # Arrange — simulate a process that crashed without releasing the
    # pidfile content (kernel released the flock on exit, but the
    # pidfile still names the dead PID). A fresh listen must acquire
    # cleanly — the flock is the source of truth, not the pidfile
    # text.
    port = 7878
    stale_pid_file = lock_dir / f"listen-{port}.pid"
    stale_pid_file.write_text("999999999\n")  # implausibly-high PID
    # Act — no live holder → must succeed.
    handle = acquire_listen_lock(port=port, lock_dir=lock_dir)
    # Assert
    try:
        assert handle.fd >= 0
    finally:
        release_listen_lock(handle)
