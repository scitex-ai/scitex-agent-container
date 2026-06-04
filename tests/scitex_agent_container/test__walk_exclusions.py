"""Tests for the shared heavy-walk exclusion predicate.

Covers ``_walk_exclusions.is_excluded_walk_dir``, the in-place
``prune_walk_dirnames`` helper for ``os.walk``, and the ``copytree_ignore``
factory used by ``shutil.copytree``. Real ``os.walk`` and
``shutil.copytree`` against tmp_path trees — no mocks. AAA layout, one
assert per test.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from scitex_agent_container._walk_exclusions import (
    copytree_ignore,
    is_excluded_walk_dir,
    prune_walk_dirnames,
)

# ---------------------------------------------------------------------------
# Predicate
# ---------------------------------------------------------------------------


def test_is_excluded_walk_dir_matches_worktrees_exactly():
    # Arrange — none needed
    # Act
    result = is_excluded_walk_dir("worktrees")
    # Assert
    assert result is True


def test_is_excluded_walk_dir_does_not_match_substring():
    """Match is BASENAME-exact, not substring — ``worktrees-archive``
    and ``old-worktrees`` must NOT be excluded."""
    # Arrange — none needed
    # Act
    excluded = {
        is_excluded_walk_dir("worktrees-archive"),
        is_excluded_walk_dir("old-worktrees"),
        is_excluded_walk_dir(".worktrees"),
    }
    # Assert
    assert excluded == {False}


def test_is_excluded_walk_dir_does_not_match_unrelated_names():
    # Arrange — none needed
    # Act
    result = is_excluded_walk_dir("skills")
    # Assert
    assert result is False


# ---------------------------------------------------------------------------
# prune_walk_dirnames — in-place mutation that os.walk picks up
# ---------------------------------------------------------------------------


def test_prune_walk_dirnames_removes_excluded_entry_in_place():
    # Arrange
    dirnames = ["hooks", "worktrees", "skills"]
    # Act
    prune_walk_dirnames(dirnames)
    # Assert
    assert dirnames == ["hooks", "skills"]


def test_prune_walk_dirnames_noop_when_no_excluded_entries():
    # Arrange
    dirnames = ["hooks", "skills", "commands"]
    # Act
    prune_walk_dirnames(dirnames)
    # Assert
    assert dirnames == ["hooks", "skills", "commands"]


def test_prune_walk_dirnames_actually_prevents_os_walk_from_descending(
    tmp_path: Path,
):
    """End-to-end against real os.walk: a worktrees/ subdir is NOT
    descended into when the prune helper is used."""
    # Arrange — tree with `worktrees/agent-x/secret.txt` + `skills/keep.txt`
    (tmp_path / "worktrees" / "agent-x").mkdir(parents=True)
    (tmp_path / "worktrees" / "agent-x" / "secret.txt").write_text("nope")
    (tmp_path / "skills").mkdir()
    (tmp_path / "skills" / "keep.txt").write_text("ok")
    visited_files: list[str] = []
    # Act
    for dirpath, dirnames, filenames in os.walk(tmp_path):
        prune_walk_dirnames(dirnames)
        for fname in filenames:
            visited_files.append(fname)
    # Assert
    assert visited_files == ["keep.txt"]


# ---------------------------------------------------------------------------
# copytree_ignore — factory for shutil.copytree
# ---------------------------------------------------------------------------


def test_copytree_ignore_skips_worktrees_subtree(tmp_path: Path):
    """copytree with the factory's ignore callable does NOT copy any
    ``worktrees/`` subtree present in the source."""
    # Arrange
    src = tmp_path / "src"
    (src / "skills").mkdir(parents=True)
    (src / "skills" / "a.md").write_text("a")
    (src / "worktrees" / "agent-x").mkdir(parents=True)
    (src / "worktrees" / "agent-x" / "big.bin").write_text("BIG")
    dst = tmp_path / "dst"
    # Act
    shutil.copytree(src, dst, symlinks=False, ignore=copytree_ignore())
    # Assert
    assert (dst / "skills" / "a.md").is_file() and not (dst / "worktrees").exists()


def test_copytree_ignore_skips_nested_worktrees_subtree(tmp_path: Path):
    """The exclusion is BASENAME-keyed and position-agnostic: a
    ``worktrees/`` nested deeper in the source is also pruned."""
    # Arrange
    src = tmp_path / "src"
    (src / "skills" / "deep" / "worktrees" / "x").mkdir(parents=True)
    (src / "skills" / "deep" / "worktrees" / "x" / "big.bin").write_text("BIG")
    (src / "skills" / "deep" / "kept.txt").write_text("ok")
    dst = tmp_path / "dst"
    # Act
    shutil.copytree(src, dst, symlinks=False, ignore=copytree_ignore())
    # Assert
    assert (dst / "skills" / "deep" / "kept.txt").is_file() and not (
        dst / "skills" / "deep" / "worktrees"
    ).exists()


def test_copytree_ignore_preserves_other_dirs(tmp_path: Path):
    """Non-excluded siblings of a pruned ``worktrees/`` are copied."""
    # Arrange
    src = tmp_path / "src"
    (src / "worktrees" / "x").mkdir(parents=True)
    (src / "worktrees" / "x" / "big.bin").write_text("BIG")
    (src / "hooks").mkdir()
    (src / "hooks" / "h.sh").write_text("#!/bin/sh\n")
    (src / "skills").mkdir()
    (src / "skills" / "s.md").write_text("ok")
    dst = tmp_path / "dst"
    # Act
    shutil.copytree(src, dst, symlinks=False, ignore=copytree_ignore())
    # Assert
    assert (dst / "hooks" / "h.sh").is_file() and (dst / "skills" / "s.md").is_file()
