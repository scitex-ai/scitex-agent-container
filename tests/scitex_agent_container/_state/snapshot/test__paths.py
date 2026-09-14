"""Regression tests for first-use snapshot cache initialization."""

from __future__ import annotations

import stat
import threading
import time
from pathlib import Path

import pytest
from scitex_config._ecosystem import local_state as _local_state

from scitex_agent_container._state.snapshot import _paths
from scitex_agent_container._state.snapshot._io import take_snapshot
from scitex_agent_container._state.snapshot._lock import _snapshot_lock


@pytest.fixture
def default_cache_path(tmp_path: Path, monkeypatch, env_save_restore) -> Path:
    """Route the non-override local-state resolver to a missing temp path."""
    env_save_restore.delete("SAC_CACHE_DIR")
    env_save_restore.delete("SCITEX_AGENT_CONTAINER_CACHE_DIR")
    cache = tmp_path / "runtime" / "cache"
    monkeypatch.setattr(_local_state, "runtime_path", lambda *_parts: cache)
    return cache


@pytest.fixture
def isolated_path(tmp_path: Path, env_save_restore) -> Path:
    """Keep snapshot probes away from host binaries and live sessions."""
    empty = tmp_path / "empty-bin"
    empty.mkdir()
    env_save_restore.set("PATH", str(empty))
    return empty


def test_default_cache_is_created_privately_before_lock_use(
    default_cache_path: Path,
) -> None:
    # Arrange: neither the runtime directory nor cache directory exists.
    # Act
    with _snapshot_lock("alpha"):
        observed_mode = stat.S_IMODE(default_cache_path.stat().st_mode)
    # Assert
    assert (default_cache_path.is_dir(), observed_mode) == (True, 0o700)


def test_take_snapshot_succeeds_when_default_cache_tree_is_absent(
    default_cache_path: Path, isolated_path
) -> None:
    # Arrange: default_cache_path does not exist and PATH has no live probes.
    # Act
    snapshot = take_snapshot("alpha")
    # Assert
    assert (
        snapshot["agent"],
        (default_cache_path / "alpha.latest.json").is_file(),
    ) == ("alpha", True)


def test_existing_cache_permissions_are_made_private(
    default_cache_path: Path,
) -> None:
    # Arrange: simulate an earlier creation under a permissive umask.
    default_cache_path.mkdir(parents=True, mode=0o777)
    default_cache_path.chmod(0o777)
    # Act
    resolved = _paths.cache_dir()
    # Assert
    assert stat.S_IMODE(resolved.stat().st_mode) == 0o700


def test_concurrent_first_lock_use_creates_once_and_serializes(
    default_cache_path: Path,
) -> None:
    # Arrange: all contenders resolve the same absent default cache directory.
    barrier = threading.Barrier(8)
    inside: list[int] = []
    overlap: list[int] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            barrier.wait()
            with _snapshot_lock("alpha"):
                inside.append(1)
                if len(inside) > 1:
                    overlap.append(len(inside))
                time.sleep(0.01)
                inside.pop()
        except BaseException as exc:
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(barrier.parties)]
    # Act
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    # Assert
    assert (errors, overlap, default_cache_path.is_dir()) == ([], [], True)
