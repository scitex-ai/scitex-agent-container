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


def test_a_first_bake_acquires_the_lock(tmp_path: Path) -> None:
    lock_dir = tmp_path / "runtime"
    lock_dir.mkdir()
    containers = tmp_path / "containers"
    containers.mkdir()

    handle = acquire_bake_lock(containers_dir=containers, lock_dir=lock_dir)
    try:
        assert handle.pid_file.is_file()
        assert handle.pid_file.read_text().strip() == str(os.getpid())
    finally:
        release_bake_lock(handle)


def test_a_second_bake_into_the_same_dir_is_REFUSED(tmp_path: Path) -> None:
    """The whole point: the second concurrent pull must not start."""
    lock_dir = tmp_path / "runtime"
    lock_dir.mkdir()
    containers = tmp_path / "containers"
    containers.mkdir()

    first = acquire_bake_lock(containers_dir=containers, lock_dir=lock_dir)
    try:
        with pytest.raises(BakeAlreadyRunningError) as excinfo:
            acquire_bake_lock(containers_dir=containers, lock_dir=lock_dir)
        message = str(excinfo.value)
        # The refusal must be actionable: name the holder and the file.
        assert str(os.getpid()) in message
        assert str(first.pid_file) in message
        assert "declining" in message
    finally:
        release_bake_lock(first)


def test_a_bake_into_a_DIFFERENT_containers_dir_is_allowed(tmp_path: Path) -> None:
    """Scoped per containers dir — unrelated bakes must not block."""
    lock_dir = tmp_path / "runtime"
    lock_dir.mkdir()
    one = tmp_path / "containers-one"
    one.mkdir()
    two = tmp_path / "containers-two"
    two.mkdir()

    first = acquire_bake_lock(containers_dir=one, lock_dir=lock_dir)
    try:
        second = acquire_bake_lock(containers_dir=two, lock_dir=lock_dir)
        release_bake_lock(second)
    finally:
        release_bake_lock(first)


def test_the_lock_is_reacquirable_after_release(tmp_path: Path) -> None:
    """A finished bake must not jam the next one."""
    lock_dir = tmp_path / "runtime"
    lock_dir.mkdir()
    containers = tmp_path / "containers"
    containers.mkdir()

    first = acquire_bake_lock(containers_dir=containers, lock_dir=lock_dir)
    release_bake_lock(first)
    second = acquire_bake_lock(containers_dir=containers, lock_dir=lock_dir)
    release_bake_lock(second)


def test_a_stale_pidfile_with_no_live_holder_does_not_jam(tmp_path: Path) -> None:
    """A crashed bake leaves a PID behind but no flock — the next must proceed.

    This is the case that would otherwise require stale-lock
    reconciliation; the kernel releasing the flock on exit is what makes
    that unnecessary.
    """
    lock_dir = tmp_path / "runtime"
    lock_dir.mkdir()
    containers = tmp_path / "containers"
    containers.mkdir()

    stale = bake_lock_path(containers, lock_dir)
    stale.write_text("999999\n")  # a PID nothing holds a flock for

    handle = acquire_bake_lock(containers_dir=containers, lock_dir=lock_dir)
    try:
        assert handle.pid_file.read_text().strip() == str(os.getpid())
    finally:
        release_bake_lock(handle)


def test_the_lock_path_is_stable_and_dir_scoped(tmp_path: Path) -> None:
    lock_dir = tmp_path / "runtime"
    one = tmp_path / "containers-one"
    one.mkdir()
    two = tmp_path / "containers-two"
    two.mkdir()

    assert bake_lock_path(one, lock_dir) == bake_lock_path(one, lock_dir)
    assert bake_lock_path(one, lock_dir) != bake_lock_path(two, lock_dir)
