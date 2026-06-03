"""Tests for :mod:`scitex_agent_container._workdir_audit`.

Real I/O against tmp fixture trees. No mocks. AAA markers + one assert
per test (PA-307 STX-TQ002/TQ007). The audit is a pure function over
the filesystem; we exercise it against real ``Path`` trees and assert
on the structured result.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container._workdir_audit import (
    WorkdirClaudeAudit,
    audit_workdir_claude,
    bloat_subdir_threshold_files,
    to_dict,
    warn_threshold_bytes,
    warn_threshold_files,
)

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def _populate_subdir(parent: Path, rel: str, file_count: int) -> None:
    """Create ``rel`` under ``parent`` with ``file_count`` 1-byte files."""
    target = parent / rel
    target.mkdir(parents=True, exist_ok=True)
    for i in range(file_count):
        (target / f"f{i}").write_bytes(b"x")


@pytest.fixture
def healthy_workdir(tmp_path: Path) -> Path:
    """Small `.claude/` tree well under every threshold."""
    claude = tmp_path / ".claude"
    claude.mkdir()
    _populate_subdir(claude, "hooks", 10)
    _populate_subdir(claude, "skills", 5)
    return tmp_path


@pytest.fixture
def bloated_workdir(tmp_path: Path) -> Path:
    """`.claude/` tree mirroring the orochi failure mode:
    big `worktrees` AND big `hooks/pre-tool-use/.pending`.
    """
    claude = tmp_path / ".claude"
    claude.mkdir()
    _populate_subdir(claude, "hooks", 10)
    _populate_subdir(claude, "skills", 5)
    _populate_subdir(claude, "worktrees", 1_500)
    _populate_subdir(claude, "hooks/pre-tool-use/.pending", 1_200)
    return tmp_path


@pytest.fixture(autouse=True)
def _clean_env(env_save_restore) -> None:
    """Each test starts with default thresholds."""
    env_save_restore.delete("SAC_WORKDIR_CLAUDE_WARN_BYTES")
    env_save_restore.delete("SAC_WORKDIR_CLAUDE_WARN_FILES")
    env_save_restore.delete("SAC_WORKDIR_CLAUDE_BLOAT_SUBDIR_FILES")


# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------


def test_warn_threshold_bytes_defaults_to_ten_mib() -> None:
    # Arrange
    expected = 10 * 1024 * 1024
    # Act
    actual = warn_threshold_bytes()
    # Assert
    assert actual == expected


def test_warn_threshold_files_defaults_to_five_thousand() -> None:
    # Arrange
    expected = 5_000
    # Act
    actual = warn_threshold_files()
    # Assert
    assert actual == expected


def test_bloat_subdir_threshold_files_defaults_to_one_thousand() -> None:
    # Arrange
    expected = 1_000
    # Act
    actual = bloat_subdir_threshold_files()
    # Assert
    assert actual == expected


@pytest.mark.parametrize(
    "env_name,getter,value",
    [
        ("SAC_WORKDIR_CLAUDE_WARN_BYTES", warn_threshold_bytes, 4242),
        ("SAC_WORKDIR_CLAUDE_WARN_FILES", warn_threshold_files, 314),
        (
            "SAC_WORKDIR_CLAUDE_BLOAT_SUBDIR_FILES",
            bloat_subdir_threshold_files,
            27,
        ),
    ],
)
def test_threshold_env_override(env_save_restore, env_name, getter, value) -> None:
    # Arrange
    env_save_restore.set(env_name, str(value))
    # Act
    actual = getter()
    # Assert
    assert actual == value


@pytest.mark.parametrize(
    "env_name,getter,default",
    [
        ("SAC_WORKDIR_CLAUDE_WARN_BYTES", warn_threshold_bytes, 10 * 1024 * 1024),
        ("SAC_WORKDIR_CLAUDE_WARN_FILES", warn_threshold_files, 5_000),
        (
            "SAC_WORKDIR_CLAUDE_BLOAT_SUBDIR_FILES",
            bloat_subdir_threshold_files,
            1_000,
        ),
    ],
)
def test_threshold_env_garbage_falls_back_to_default(
    env_save_restore, env_name, getter, default
) -> None:
    # Arrange — garbage value should NOT silently zero the threshold.
    env_save_restore.set(env_name, "not-a-number")
    # Act
    actual = getter()
    # Assert
    assert actual == default


@pytest.mark.parametrize(
    "env_name,getter,default",
    [
        ("SAC_WORKDIR_CLAUDE_WARN_BYTES", warn_threshold_bytes, 10 * 1024 * 1024),
        ("SAC_WORKDIR_CLAUDE_WARN_FILES", warn_threshold_files, 5_000),
    ],
)
def test_threshold_env_zero_falls_back_to_default(
    env_save_restore, env_name, getter, default
) -> None:
    # Arrange — zero would defeat the protection; treat as garbage.
    env_save_restore.set(env_name, "0")
    # Act
    actual = getter()
    # Assert
    assert actual == default


# ---------------------------------------------------------------------------
# audit_workdir_claude — happy paths
# ---------------------------------------------------------------------------


def test_audit_returns_dataclass(healthy_workdir: Path) -> None:
    # Arrange
    workdir = healthy_workdir
    # Act
    result = audit_workdir_claude(workdir)
    # Assert
    assert isinstance(result, WorkdirClaudeAudit)


def test_audit_counts_files_in_healthy_tree(healthy_workdir: Path) -> None:
    # Arrange — fixture has 10 + 5 = 15 files.
    expected = 15
    # Act
    result = audit_workdir_claude(healthy_workdir)
    # Assert
    assert result.files == expected


def test_audit_sums_bytes_in_healthy_tree(healthy_workdir: Path) -> None:
    # Arrange — fixture has 15 × 1-byte files = 15 bytes.
    expected = 15
    # Act
    result = audit_workdir_claude(healthy_workdir)
    # Assert
    assert result.bytes == expected


def test_audit_healthy_tree_does_not_exceed_files(healthy_workdir: Path) -> None:
    # Arrange
    workdir = healthy_workdir
    # Act
    result = audit_workdir_claude(workdir)
    # Assert
    assert result.exceeded_files is False


def test_audit_healthy_tree_does_not_exceed_bytes(healthy_workdir: Path) -> None:
    # Arrange
    workdir = healthy_workdir
    # Act
    result = audit_workdir_claude(workdir)
    # Assert
    assert result.exceeded_bytes is False


def test_audit_healthy_tree_has_no_bloat_sources(healthy_workdir: Path) -> None:
    # Arrange
    workdir = healthy_workdir
    # Act
    result = audit_workdir_claude(workdir)
    # Assert
    assert result.bloat_sources == ()


# ---------------------------------------------------------------------------
# audit_workdir_claude — bloat detection
# ---------------------------------------------------------------------------


def test_audit_bloated_tree_files_exceed_threshold(
    bloated_workdir: Path,
) -> None:
    # Arrange — fixture has 10 + 5 + 1500 + 1200 = 2715 files; threshold 5k.
    # Lower the threshold so the assertion makes sense at fixture-scale.
    import os

    os.environ["SAC_WORKDIR_CLAUDE_WARN_FILES"] = "2000"
    try:
        # Act
        result = audit_workdir_claude(bloated_workdir)
    finally:
        del os.environ["SAC_WORKDIR_CLAUDE_WARN_FILES"]
    # Assert
    assert result.exceeded_files is True


def test_audit_bloated_tree_lists_worktrees_as_bloat_source(
    bloated_workdir: Path,
) -> None:
    # Arrange — worktrees subdir has 1500 > 1000 default subdir threshold.
    # Act
    result = audit_workdir_claude(bloated_workdir)
    # Assert
    assert any(s.rel_path == "worktrees" for s in result.bloat_sources)


def test_audit_bloated_tree_lists_pending_as_bloat_source(
    bloated_workdir: Path,
) -> None:
    # Arrange — pending subdir has 1200 > 1000 default subdir threshold.
    # Act
    result = audit_workdir_claude(bloated_workdir)
    # Assert
    assert any(
        s.rel_path == "hooks/pre-tool-use/.pending" for s in result.bloat_sources
    )


def test_audit_bloat_sources_sorted_desc_by_files(bloated_workdir: Path) -> None:
    # Arrange — worktrees has 1500, pending has 1200. Worst-first ordering.
    # Act
    result = audit_workdir_claude(bloated_workdir)
    files_counts = [s.files for s in result.bloat_sources]
    # Assert
    assert files_counts == sorted(files_counts, reverse=True)


def test_audit_subdir_under_bloat_threshold_not_listed(tmp_path: Path) -> None:
    # Arrange — populate worktrees with fewer files than the default 1000
    # bloat-subdir threshold; it should NOT show up in bloat_sources.
    claude = tmp_path / ".claude"
    claude.mkdir()
    _populate_subdir(claude, "worktrees", 50)
    # Act
    result = audit_workdir_claude(tmp_path)
    # Assert
    assert result.bloat_sources == ()


def test_audit_custom_probed_subdirs_only_reports_listed(
    bloated_workdir: Path,
) -> None:
    # Arrange — restrict probing to just worktrees; pending must NOT
    # appear even though it is over the per-subdir threshold.
    probed = ("worktrees",)
    # Act
    result = audit_workdir_claude(bloated_workdir, probed_subdirs=probed)
    rel_paths = {s.rel_path for s in result.bloat_sources}
    # Assert
    assert rel_paths == {"worktrees"}


# ---------------------------------------------------------------------------
# audit_workdir_claude — missing / empty inputs
# ---------------------------------------------------------------------------


def test_audit_none_workdir_returns_missing(_clean_env=None) -> None:
    # Arrange
    workdir: None = None
    # Act
    result = audit_workdir_claude(workdir)
    # Assert
    assert result.missing is True


def test_audit_empty_workdir_returns_missing(_clean_env=None) -> None:
    # Arrange
    workdir = ""
    # Act
    result = audit_workdir_claude(workdir)
    # Assert
    assert result.missing is True


def test_audit_workdir_without_claude_subdir_returns_missing(
    tmp_path: Path,
) -> None:
    # Arrange — tmp_path has no .claude/ child.
    workdir = tmp_path
    # Act
    result = audit_workdir_claude(workdir)
    # Assert
    assert result.missing is True


def test_audit_missing_tree_zero_files(tmp_path: Path) -> None:
    # Arrange
    workdir = tmp_path
    # Act
    result = audit_workdir_claude(workdir)
    # Assert
    assert result.files == 0


def test_audit_missing_tree_does_not_exceed_files(tmp_path: Path) -> None:
    # Arrange
    workdir = tmp_path
    # Act
    result = audit_workdir_claude(workdir)
    # Assert
    assert result.exceeded_files is False


# ---------------------------------------------------------------------------
# audit_workdir_claude — symlinks not followed
# ---------------------------------------------------------------------------


def test_audit_does_not_follow_symlinks(tmp_path: Path) -> None:
    # Arrange — link `.claude/skills` → an outside tree of 100 files.
    outside = tmp_path / "outside"
    outside.mkdir()
    for i in range(100):
        (outside / f"f{i}").write_bytes(b"x")
    claude = tmp_path / "workdir" / ".claude"
    claude.mkdir(parents=True)
    (claude / "skills").symlink_to(outside, target_is_directory=True)
    # Act
    result = audit_workdir_claude(tmp_path / "workdir")
    # Assert — the 100 outside files MUST NOT contribute to the count.
    assert result.files == 0


# ---------------------------------------------------------------------------
# to_dict projection (status JSON / external consumers)
# ---------------------------------------------------------------------------


def test_to_dict_round_trips_workdir(healthy_workdir: Path) -> None:
    # Arrange
    audit = audit_workdir_claude(healthy_workdir)
    # Act
    d = to_dict(audit)
    # Assert
    assert d["workdir"] == str(healthy_workdir)


def test_to_dict_round_trips_files(healthy_workdir: Path) -> None:
    # Arrange
    audit = audit_workdir_claude(healthy_workdir)
    # Act
    d = to_dict(audit)
    # Assert
    assert d["files"] == audit.files


def test_to_dict_bloat_sources_are_dicts(bloated_workdir: Path) -> None:
    # Arrange
    audit = audit_workdir_claude(bloated_workdir)
    # Act
    d = to_dict(audit)
    # Assert
    assert all(isinstance(s, dict) for s in d["bloat_sources"])


def test_to_dict_bloat_sources_have_rel_path(bloated_workdir: Path) -> None:
    # Arrange
    audit = audit_workdir_claude(bloated_workdir)
    # Act
    d = to_dict(audit)
    # Assert
    assert all("rel_path" in s for s in d["bloat_sources"])


def test_to_dict_carries_missing_flag(tmp_path: Path) -> None:
    # Arrange
    audit = audit_workdir_claude(tmp_path)
    # Act
    d = to_dict(audit)
    # Assert
    assert d["missing"] is True


def test_to_dict_carries_threshold_files() -> None:
    # Arrange
    audit = audit_workdir_claude(None)
    # Act
    d = to_dict(audit)
    # Assert
    assert d["threshold_files"] == 5_000
