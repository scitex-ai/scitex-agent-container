from __future__ import annotations

import json
import multiprocessing
import os
import re
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
    process.start()
    try:
        if not acquired.wait(timeout=10):
            raise RuntimeError("child process did not acquire the build lock")
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
