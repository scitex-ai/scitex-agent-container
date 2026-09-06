"""Tests for the bake-remote single-instance lock.

The incident these encode: a second `bake-remote` started while one was
mid-transfer, resumed from the incumbent's partial, wrote its own temp,
and drove scitex-compute-03 from 17G free to 3.8G with three concurrent
pulls of one artifact.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from scitex_agent_container.cli_pkg._bake_lock import (
    BakeAlreadyRunningError,
    acquire_bake_lock,
    bake_lock_path,
    release_bake_lock,
)


@pytest.fixture
def lock_dir(tmp_path: Path) -> Path:
    """Directory holding the pidfile; the caller owns creating it."""
    path = tmp_path / "runtime"
    path.mkdir()
    return path


@pytest.fixture
def containers_dir(tmp_path: Path) -> Path:
    """The bake destination the lock is scoped to."""
    path = tmp_path / "containers"
    path.mkdir()
    return path


def test_the_first_bake_creates_its_pidfile(
    lock_dir: Path, containers_dir: Path
) -> None:
    # Arrange
    handle = None
    # Act
    handle = acquire_bake_lock(containers_dir=containers_dir, lock_dir=lock_dir)
    # Assert
    try:
        assert handle.pid_file.is_file()
    finally:
        release_bake_lock(handle)


def test_the_first_bake_stamps_its_own_pid(
    lock_dir: Path, containers_dir: Path
) -> None:
    # Arrange
    expected = str(os.getpid())
    # Act
    handle = acquire_bake_lock(containers_dir=containers_dir, lock_dir=lock_dir)
    # Assert
    try:
        assert handle.pid_file.read_text().strip() == expected
    finally:
        release_bake_lock(handle)


def test_a_second_bake_into_the_same_dir_is_refused(
    lock_dir: Path, containers_dir: Path
) -> None:
    """The whole point: the second concurrent pull must not start."""
    # Arrange
    first = acquire_bake_lock(containers_dir=containers_dir, lock_dir=lock_dir)
    # Act
    # Assert
    try:
        with pytest.raises(BakeAlreadyRunningError):
            acquire_bake_lock(containers_dir=containers_dir, lock_dir=lock_dir)
    finally:
        release_bake_lock(first)


def test_the_refusal_names_the_holding_pid(
    lock_dir: Path, containers_dir: Path
) -> None:
    """`kill <pid>` must be actionable without lsof."""
    # Arrange
    first = acquire_bake_lock(containers_dir=containers_dir, lock_dir=lock_dir)
    message = ""
    # Act
    try:
        acquire_bake_lock(containers_dir=containers_dir, lock_dir=lock_dir)
    except BakeAlreadyRunningError as exc:
        message = str(exc)
    finally:
        release_bake_lock(first)
    # Assert
    assert str(os.getpid()) in message


def test_the_refusal_says_it_is_declining_not_failing(
    lock_dir: Path, containers_dir: Path
) -> None:
    """A supervised caller must not read this as a crash."""
    # Arrange
    first = acquire_bake_lock(containers_dir=containers_dir, lock_dir=lock_dir)
    message = ""
    # Act
    try:
        acquire_bake_lock(containers_dir=containers_dir, lock_dir=lock_dir)
    except BakeAlreadyRunningError as exc:
        message = str(exc)
    finally:
        release_bake_lock(first)
    # Assert
    assert "declining" in message


def test_a_bake_into_a_different_containers_dir_is_allowed(
    lock_dir: Path, tmp_path: Path
) -> None:
    """Scoped per containers dir — unrelated bakes must not block."""
    # Arrange
    one = tmp_path / "containers-one"
    one.mkdir()
    two = tmp_path / "containers-two"
    two.mkdir()
    first = acquire_bake_lock(containers_dir=one, lock_dir=lock_dir)
    # Act
    second = acquire_bake_lock(containers_dir=two, lock_dir=lock_dir)
    # Assert
    try:
        assert second.pid_file != first.pid_file
    finally:
        release_bake_lock(second)
        release_bake_lock(first)


def test_the_lock_is_reacquirable_after_release(
    lock_dir: Path, containers_dir: Path
) -> None:
    """A finished bake must not jam the next one."""
    # Arrange
    first = acquire_bake_lock(containers_dir=containers_dir, lock_dir=lock_dir)
    release_bake_lock(first)
    # Act
    second = acquire_bake_lock(containers_dir=containers_dir, lock_dir=lock_dir)
    # Assert
    try:
        assert second.pid_file.read_text().strip() == str(os.getpid())
    finally:
        release_bake_lock(second)


def test_a_stale_pidfile_with_no_live_holder_does_not_jam(
    lock_dir: Path, containers_dir: Path
) -> None:
    """A crashed bake leaves a PID behind but no flock — the next proceeds.

    The kernel releasing the flock on exit is what makes stale-lock
    reconciliation unnecessary.
    """
    # Arrange
    stale = bake_lock_path(containers_dir, lock_dir)
    stale.write_text("999999\n")
    # Act
    handle = acquire_bake_lock(containers_dir=containers_dir, lock_dir=lock_dir)
    # Assert
    try:
        assert handle.pid_file.read_text().strip() == str(os.getpid())
    finally:
        release_bake_lock(handle)


def test_the_lock_path_is_stable_for_one_dir(
    lock_dir: Path, containers_dir: Path
) -> None:
    # Arrange
    first = bake_lock_path(containers_dir, lock_dir)
    # Act
    second = bake_lock_path(containers_dir, lock_dir)
    # Assert
    assert first == second


def test_the_lock_path_differs_between_dirs(lock_dir: Path, tmp_path: Path) -> None:
    # Arrange
    one = tmp_path / "containers-one"
    one.mkdir()
    two = tmp_path / "containers-two"
    two.mkdir()
    # Act
    paths = (bake_lock_path(one, lock_dir), bake_lock_path(two, lock_dir))
    # Assert
    assert paths[0] != paths[1]
