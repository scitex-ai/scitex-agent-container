from __future__ import annotations

import json
import multiprocessing
import os
import re
import time
from pathlib import Path

import pytest

from scitex_agent_container.cli_pkg._image_build_lock import (
    ImageBuildInProgress,
    image_build_lock,
)


def _hold_lock(artifact_dir: str, acquired, release) -> None:
    with image_build_lock(Path(artifact_dir), layer="base"):
        acquired.set()
        release.wait(timeout=10)


def test_same_layer_refuses_second_process_with_owner_details(tmp_path: Path):
    # Arrange
    artifact_dir = tmp_path / "sac-base"
    context = multiprocessing.get_context("spawn")
    acquired = context.Event()
    release = context.Event()
    process = context.Process(
        target=_hold_lock,
        args=(str(artifact_dir), acquired, release),
    )
    started = time.monotonic()
    process.start()
    try:
        if not acquired.wait(timeout=10):
            diagnostics = {
                "pid": process.pid,
                "alive": process.is_alive(),
                "exitcode": process.exitcode,
                "start_method": context.get_start_method(),
                "elapsed_seconds": time.monotonic() - started,
            }
            raise RuntimeError(
                "child process did not acquire the build lock; diagnostics: "
                + json.dumps(diagnostics, sort_keys=True)
            )
        expected = re.compile(
            rf"refusing concurrent sac image build for layer 'base'.*"
            rf"pid={process.pid}.*host=.*started_at=.*"
            rf"Wait for that build to finish",
        )

        # Act
        # Assert
        with pytest.raises(ImageBuildInProgress, match=expected):
            with image_build_lock(artifact_dir, layer="base"):
                raise RuntimeError("a concurrent same-layer build acquired the lock")
    finally:
        release.set()
        process.join(timeout=10)
        if process.is_alive():
            process.terminate()
            process.join(timeout=10)


def test_lock_is_released_after_exception(tmp_path: Path):
    # Arrange
    artifact_dir = tmp_path / "sac-base"

    # Act
    try:
        with image_build_lock(artifact_dir, layer="base"):
            raise RuntimeError("build failed")
    except RuntimeError:
        pass
    with image_build_lock(artifact_dir, layer="base"):
        pass

    # Assert
    assert (artifact_dir / ".build.lock").read_text() == ""


def test_different_layers_can_build_concurrently(tmp_path: Path):
    # Arrange
    base_dir = tmp_path / "sac-base"
    scitex_dir = tmp_path / "sac-scitex"

    # Act
    with image_build_lock(base_dir, layer="base"):
        with image_build_lock(scitex_dir, layer="scitex"):
            owners = [
                json.loads((path / ".build.lock").read_text())
                for path in (base_dir, scitex_dir)
            ]

            # Assert
            assert [owner["pid"] for owner in owners] == [os.getpid(), os.getpid()]


@pytest.fixture(params=["symlink", "hardlink", "fifo"])
def inadmissible_lock_entry(tmp_path, request):
    """Owned filesystem entries must not redirect a destructive lock write."""
    artifact = tmp_path / "artifact"
    artifact.mkdir()
    canary = tmp_path / "owned-canary"
    canary.write_text("owned contents must survive\n")
    lock = artifact / ".build.lock"
    if request.param == "symlink":
        lock.symlink_to(canary)
    elif request.param == "hardlink":
        os.link(canary, lock)
    else:
        os.mkfifo(lock)
    return artifact, canary


def test_refused_lock_entry_preserves_unrelated_file(inadmissible_lock_entry):
    # Arrange
    artifact, canary = inadmissible_lock_entry
    accepted = False

    # Act
    try:
        with image_build_lock(artifact, layer="base"):
            accepted = True
    except OSError:
        pass

    # Assert
    assert (accepted, canary.read_text()) == (False, "owned contents must survive\n")
