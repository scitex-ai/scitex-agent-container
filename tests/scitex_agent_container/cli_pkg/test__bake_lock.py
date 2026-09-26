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
def containers_dir(tmp_path: Path) -> Path:
    """The bake destination the lock is scoped to."""
    path = tmp_path / "containers"
    path.mkdir()
    return path


def test_the_first_bake_creates_its_pidfile(
    containers_dir: Path
) -> None:
    # Arrange
    handle = None
    # Act
    handle = acquire_bake_lock(containers_dir=containers_dir)
    # Assert
    try:
        assert handle.pid_file.is_file()
    finally:
        release_bake_lock(handle)


def test_the_first_bake_stamps_its_own_pid(
    containers_dir: Path
) -> None:
    # Arrange
    expected = str(os.getpid())
    # Act
    handle = acquire_bake_lock(containers_dir=containers_dir)
    # Assert
    try:
        assert handle.pid_file.read_text().strip() == expected
    finally:
        release_bake_lock(handle)


def test_a_second_bake_into_the_same_dir_is_refused(
    containers_dir: Path
) -> None:
    """The whole point: the second concurrent pull must not start."""
    # Arrange
    first = acquire_bake_lock(containers_dir=containers_dir)
    # Act
    # Assert
    try:
        with pytest.raises(BakeAlreadyRunningError):
            acquire_bake_lock(containers_dir=containers_dir)
    finally:
        release_bake_lock(first)


def test_the_refusal_names_the_holding_pid(
    containers_dir: Path
) -> None:
    """`kill <pid>` must be actionable without lsof."""
    # Arrange
    first = acquire_bake_lock(containers_dir=containers_dir)
    message = ""
    # Act
    try:
        acquire_bake_lock(containers_dir=containers_dir)
    except BakeAlreadyRunningError as exc:
        message = str(exc)
    finally:
        release_bake_lock(first)
    # Assert
    assert str(os.getpid()) in message


def test_the_refusal_says_it_is_declining_not_failing(
    containers_dir: Path
) -> None:
    """A supervised caller must not read this as a crash."""
    # Arrange
    first = acquire_bake_lock(containers_dir=containers_dir)
    message = ""
    # Act
    try:
        acquire_bake_lock(containers_dir=containers_dir)
    except BakeAlreadyRunningError as exc:
        message = str(exc)
    finally:
        release_bake_lock(first)
    # Assert
    assert "declining" in message


def test_a_bake_into_a_different_containers_dir_is_allowed(tmp_path: Path) -> None:
    """Scoped per containers dir — unrelated bakes must not block."""
    # Arrange
    one = tmp_path / "containers-one"
    one.mkdir()
    two = tmp_path / "containers-two"
    two.mkdir()
    first = acquire_bake_lock(containers_dir=one)
    # Act
    second = acquire_bake_lock(containers_dir=two)
    # Assert
    try:
        assert second.pid_file != first.pid_file
    finally:
        release_bake_lock(second)
        release_bake_lock(first)


def test_the_lock_is_reacquirable_after_release(
    containers_dir: Path
) -> None:
    """A finished bake must not jam the next one."""
    # Arrange
    first = acquire_bake_lock(containers_dir=containers_dir)
    release_bake_lock(first)
    # Act
    second = acquire_bake_lock(containers_dir=containers_dir)
    # Assert
    try:
        assert second.pid_file.read_text().strip() == str(os.getpid())
    finally:
        release_bake_lock(second)


def test_a_stale_pidfile_with_no_live_holder_does_not_jam(
    containers_dir: Path
) -> None:
    """A crashed bake leaves a PID behind but no flock — the next proceeds.

    The kernel releasing the flock on exit is what makes stale-lock
    reconciliation unnecessary.
    """
    # Arrange
    stale = bake_lock_path(containers_dir)
    stale.write_text("999999\n")
    # Act
    handle = acquire_bake_lock(containers_dir=containers_dir)
    # Assert
    try:
        assert handle.pid_file.read_text().strip() == str(os.getpid())
    finally:
        release_bake_lock(handle)


def test_the_lock_path_is_stable_for_one_dir(
    containers_dir: Path
) -> None:
    # Arrange
    first = bake_lock_path(containers_dir)
    # Act
    second = bake_lock_path(containers_dir)
    # Assert
    assert first == second


def test_the_lock_path_differs_between_dirs(tmp_path: Path) -> None:
    # Arrange
    one = tmp_path / "containers-one"
    one.mkdir()
    two = tmp_path / "containers-two"
    two.mkdir()
    # Act
    paths = (bake_lock_path(one), bake_lock_path(two))
    # Assert
    assert paths[0] != paths[1]


def test_the_lock_lives_INSIDE_the_containers_dir_not_in_home(
    containers_dir: Path,
) -> None:
    """Regression: a $HOME-scoped lock made CI's two matrix legs contend.

    They run concurrently on the same self-hosted runner with one shared
    home, so the second leg's bake tests DECLINED and develop went red on
    four tests. Keeping the lock beside the artifacts it guards makes a
    tmp_path containers dir isolated by construction.
    """
    # Arrange
    expected_parent = containers_dir
    # Act
    path = bake_lock_path(containers_dir)
    # Assert
    assert path.parent == expected_parent
