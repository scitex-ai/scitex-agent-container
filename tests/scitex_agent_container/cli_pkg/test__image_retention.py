"""Tests for :mod:`scitex_agent_container.cli_pkg._image_retention`.

Conventions: AAA markers, one assertion per test, no mocks — real files
under ``tmp_path`` with explicit mtimes.
"""

from __future__ import annotations

import os

from scitex_agent_container.cli_pkg._image_retention import (
    retire_superseded_builds,
)


def _dated_sif(artifact_dir, name, mtime):
    path = artifact_dir / name
    path.write_bytes(b"sif")
    os.utime(path, (mtime, mtime))
    return path


def test_archives_all_but_newest_two(tmp_path):
    # Arrange — four timestamped builds, oldest first.
    artifact = tmp_path / "sac-base"
    artifact.mkdir()
    for i, stem in enumerate(["a", "b", "c", "d"]):
        _dated_sif(artifact, f"sac-base-2026-1023-00000{i}.sif", 1000 + i)
    # Act
    moved = retire_superseded_builds(artifact, "base")
    # Assert
    assert sorted(p.name for p in moved) == [
        "sac-base-2026-1023-000000.sif",
        "sac-base-2026-1023-000001.sif",
    ]


def test_stable_boot_symlink_is_never_touched(tmp_path):
    # Arrange — stable link points at the newest build.
    artifact = tmp_path / "sac-scitex"
    artifact.mkdir()
    for i in range(3):
        _dated_sif(artifact, f"sac-scitex-2026-1023-00000{i}.sif", 1000 + i)
    link = artifact / "sac-scitex.sif"
    link.symlink_to("sac-scitex-2026-1023-000002.sif")
    # Act
    retire_superseded_builds(artifact, "scitex")
    # Assert
    assert link.is_symlink()


def test_missing_artifact_dir_returns_empty(tmp_path):
    # Arrange — nothing built yet for this layer.
    # Act
    moved = retire_superseded_builds(tmp_path / "sac-base", "base")
    # Assert
    assert moved == []


def test_fewer_builds_than_keep_returns_empty(tmp_path):
    # Arrange — single build, nothing superseded.
    artifact = tmp_path / "sac-base"
    artifact.mkdir()
    _dated_sif(artifact, "sac-base-2026-1023-000000.sif", 1000)
    # Act
    moved = retire_superseded_builds(artifact, "base")
    # Assert
    assert moved == []


def test_report_helper_archives_superseded_builds(tmp_path):
    # Arrange — the CLI wire-in for both build success paths.
    from scitex_agent_container.cli_pkg.image_group import (
        _report_retired_builds,
    )

    artifact = tmp_path / "sac-base"
    artifact.mkdir()
    for i in range(3):
        _dated_sif(artifact, f"sac-base-2026-1023-00000{i}.sif", 1000 + i)
    # Act
    _report_retired_builds(tmp_path, "base")
    # Assert
    assert [p.name for p in sorted((artifact / "archive").iterdir())] == [
        "sac-base-2026-1023-000000.sif"
    ]
