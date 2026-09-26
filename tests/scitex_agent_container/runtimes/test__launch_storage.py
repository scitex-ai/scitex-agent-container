"""Launch-storage path evidence and threshold policy."""

from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from scitex_agent_container.runtimes._launch_storage import (
    REFUSE_ROOT_FREE_BYTES,
    WARN_FREE_BYTES,
    LaunchStorageSpaceError,
    assess_launch_path,
    launch_storage_identity,
    verify_launch_storage,
)


def test_identity_records_explicit_overlay_and_workdir() -> None:
    # Arrange
    argv = [
        "apptainer",
        "exec",
        "--overlay=/operator/overlay",
        "--workdir",
        "/scratch/sac/agents/alpha/apptainer-workdir",
        "image.sif",
    ]
    # Act
    identity = launch_storage_identity(argv)
    # Assert
    assert identity == {
        "overlay": "/operator/overlay",
        "apptainer_workdir": "/scratch/sac/agents/alpha/apptainer-workdir",
    }


def test_policy_warns_below_twenty_gib(caplog: pytest.LogCaptureFixture) -> None:
    # Arrange
    free = WARN_FREE_BYTES - 1
    # Act
    with caplog.at_level(logging.WARNING):
        assess_launch_path(
            label="overlay",
            path="/scratch/overlay",
            backing=Path("/scratch"),
            free=free,
            root_backed=False,
        )
    # Assert
    assert "launch storage warning" in caplog.text


def test_policy_refuses_root_below_five_gib() -> None:
    # Arrange
    free = REFUSE_ROOT_FREE_BYTES - 1
    # Act
    ctx = pytest.raises(LaunchStorageSpaceError)
    # Assert
    with ctx:
        assess_launch_path(
            label="overlay",
            path="/home/agent-overlay",
            backing=Path("/"),
            free=free,
            root_backed=True,
        )


def test_policy_allows_non_root_below_five_gib_with_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Arrange
    free = REFUSE_ROOT_FREE_BYTES - 1
    # Act
    with caplog.at_level(logging.WARNING):
        result = assess_launch_path(
            label="overlay",
            path="/scratch/overlay",
            backing=Path("/scratch"),
            free=free,
            root_backed=False,
        )
    # Assert
    assert result is None


def test_preflight_uses_available_bytes_from_statvfs(tmp_path: Path) -> None:
    # Arrange — a real existing target with deterministic filesystem facts.
    overlay = tmp_path / "overlay"
    overlay.mkdir()

    def stat_fn(path: object) -> SimpleNamespace:
        return SimpleNamespace(st_dev=7)

    def statvfs_fn(path: object) -> SimpleNamespace:
        return SimpleNamespace(f_bavail=1, f_frsize=REFUSE_ROOT_FREE_BYTES - 1)

    # Act
    ctx = pytest.raises(LaunchStorageSpaceError)
    # Assert
    with ctx:
        verify_launch_storage(
            ["apptainer", "exec", "--overlay", str(overlay), "image.sif"],
            stat_fn=stat_fn,
            statvfs_fn=statvfs_fn,
        )
