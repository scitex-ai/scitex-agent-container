"""Tests for the P2 retraction primitive: ``retract_marker`` renames
``STARTUP_FAILED`` aside, and NEVER deletes it — the marker is the only
copy of its ``stderr_tail`` (``runtime/events/*.jsonl`` carries none).
"""

from __future__ import annotations

import json
from pathlib import Path

from scitex_agent_container._lifecycle._startup_failed import (
    MARKER_FILENAME,
    RETRACTED_MARKER_FILENAME,
    read_marker,
    retract_marker,
    write_marker,
)

_ORIGINAL_STDERR = (
    "FATAL: container creation failed: mount source /work/x doesn't exist"
)


def _seed_marker(runtime_dir: Path) -> None:
    write_marker(
        runtime_dir,
        started_at="2026-07-22T00:00:00Z",
        phase="container_creation",
        exit_code=255,
        stdout="",
        stderr=_ORIGINAL_STDERR,
    )


def test_retract_marker_returns_true_when_marker_present(tmp_path: Path) -> None:
    # Arrange
    _seed_marker(tmp_path)
    # Act
    retracted = retract_marker(tmp_path)
    # Assert
    assert retracted is True


def test_retract_marker_removes_the_original_file(tmp_path: Path) -> None:
    # Arrange
    _seed_marker(tmp_path)
    # Act
    retract_marker(tmp_path)
    # Assert
    assert not (tmp_path / MARKER_FILENAME).exists()


def test_retract_marker_creates_the_retracted_file(tmp_path: Path) -> None:
    # Arrange
    _seed_marker(tmp_path)
    # Act
    retract_marker(tmp_path)
    # Assert
    assert (tmp_path / RETRACTED_MARKER_FILENAME).is_file()


def test_retract_marker_retracted_file_preserves_stderr_tail(tmp_path: Path) -> None:
    # Arrange
    _seed_marker(tmp_path)
    retract_marker(tmp_path)
    # Act
    payload = json.loads((tmp_path / RETRACTED_MARKER_FILENAME).read_text())
    # Assert
    assert _ORIGINAL_STDERR in payload["stderr_tail"]


def test_retract_marker_retracted_file_preserves_kind(tmp_path: Path) -> None:
    # Arrange — the retracted copy is a byte-for-byte rename, not a rewrite;
    # pin a second field so a "rewrite with a subset of fields" mutation
    # would also be caught.
    _seed_marker(tmp_path)
    retract_marker(tmp_path)
    # Act
    payload = json.loads((tmp_path / RETRACTED_MARKER_FILENAME).read_text())
    # Assert
    assert payload["kind"] == "apptainer_mount_failed"


def test_read_marker_no_longer_sees_a_retracted_marker(tmp_path: Path) -> None:
    # Arrange — the wire-facing reader keys on the exact MARKER_FILENAME,
    # so a retraction must fully retract the STATUS/DELETE behaviour too.
    _seed_marker(tmp_path)
    retract_marker(tmp_path)
    # Act
    result = read_marker(tmp_path)
    # Assert
    assert result is None


def test_retract_marker_on_a_clean_dir_returns_false(tmp_path: Path) -> None:
    # Arrange — nothing on disk.
    # Act
    retracted = retract_marker(tmp_path)
    # Assert
    assert retracted is False


def test_retract_marker_on_an_already_retracted_dir_returns_false(
    tmp_path: Path,
) -> None:
    # Arrange — first retraction already happened.
    _seed_marker(tmp_path)
    retract_marker(tmp_path)
    # Act
    retracted_again = retract_marker(tmp_path)
    # Assert
    assert retracted_again is False
