"""Tests for :mod:`scitex_agent_container._state.worktree_safety`.

Pins the ``is_safe_to_reap`` predicate that gates host-side
``git worktree prune`` against the silent-destruction mode from
lead-learnings/19 (paired with PR #369 pre-stop rescue).

Real ``tmp_path`` git repos — no mocks (PA-306). AAA layout
(STX-TQ002) + one assertion per test (STX-TQ007).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scitex_agent_container._state.worktree_safety import is_safe_to_reap

# ---------------------------------------------------------------------------
# Helpers — small wrappers around ``git`` so each test reads as AAA.
# ---------------------------------------------------------------------------


def _run(cwd: Path, *args: str) -> None:
    """Invoke git in ``cwd``; raise on failure so tests fail loudly."""
    subprocess.run(
        ["git", "-C", str(cwd), *args],
        check=True,
        capture_output=True,
    )


def _init_repo_with_develop(repo: Path) -> None:
    """Create a repo whose default branch is ``develop`` with one commit."""
    repo.mkdir(parents=True, exist_ok=True)
    _run(repo, "init", "-q", "-b", "develop")
    _run(repo, "config", "user.email", "test@example.com")
    _run(repo, "config", "user.name", "Test")
    (repo / "README").write_text("seed\n")
    _run(repo, "add", "README")
    _run(repo, "commit", "-q", "-m", "seed")


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def worktree_merged(tmp_path: Path) -> Path:
    """A worktree whose ``HEAD`` is identical to ``develop`` (clean,
    not ahead, not behind). The most common safe-to-reap shape:
    a feature branch that was already squash-merged back to
    ``develop`` so ``develop..HEAD`` is empty."""
    repo = tmp_path / "repo"
    _init_repo_with_develop(repo)
    # A feature branch at the same commit as develop: clean +
    # not ahead → safe to reap.
    _run(repo, "checkout", "-q", "-b", "feature/merged")
    return repo


@pytest.fixture
def worktree_dirty(tmp_path: Path) -> Path:
    """A worktree whose ``HEAD`` matches ``develop`` but the working
    tree has an uncommitted change (porcelain non-empty). This is
    exactly the lead-learnings/19 loss-of-work shape: reaping would
    destroy the dirty edit."""
    repo = tmp_path / "repo"
    _init_repo_with_develop(repo)
    _run(repo, "checkout", "-q", "-b", "feature/dirty")
    # Untracked file makes porcelain non-empty.
    (repo / "scratch.txt").write_text("uncommitted work\n")
    return repo


@pytest.fixture
def worktree_ahead(tmp_path: Path) -> Path:
    """A worktree whose ``HEAD`` is ahead of ``develop`` by one commit
    (clean tree, but unmerged work). Reaping would discard that
    commit unless it was pushed elsewhere; the predicate must
    refuse."""
    repo = tmp_path / "repo"
    _init_repo_with_develop(repo)
    _run(repo, "checkout", "-q", "-b", "feature/ahead")
    (repo / "new.txt").write_text("committed but unmerged\n")
    _run(repo, "add", "new.txt")
    _run(repo, "commit", "-q", "-m", "ahead of develop")
    return repo


@pytest.fixture
def not_a_worktree(tmp_path: Path) -> Path:
    """A plain directory with no ``.git`` entry — fails the very
    first guard. Exercises the "predicate must not crash on a
    random path" contract."""
    plain = tmp_path / "plain"
    plain.mkdir()
    (plain / "hello.txt").write_text("not a worktree\n")
    return plain


# ---------------------------------------------------------------------------
# Safe-to-reap (the only True case)
# ---------------------------------------------------------------------------


def test_safe_when_clean_and_not_ahead_of_develop(
    worktree_merged: Path,
) -> None:
    # Arrange — repo fixture: feature branch == develop, clean tree.
    # Act
    result = is_safe_to_reap(worktree_merged)
    # Assert
    assert result is True


# ---------------------------------------------------------------------------
# Refuse: dirty tree (lead-learnings/19 loss-of-work shape)
# ---------------------------------------------------------------------------


def test_unsafe_when_porcelain_non_empty(worktree_dirty: Path) -> None:
    # Arrange — repo fixture has an untracked file.
    # Act
    result = is_safe_to_reap(worktree_dirty)
    # Assert
    assert result is False


# ---------------------------------------------------------------------------
# Refuse: HEAD ahead of develop (unmerged commits would be lost)
# ---------------------------------------------------------------------------


def test_unsafe_when_head_is_ahead_of_develop(
    worktree_ahead: Path,
) -> None:
    # Arrange — repo fixture has one commit not on develop.
    # Act
    result = is_safe_to_reap(worktree_ahead)
    # Assert
    assert result is False


# ---------------------------------------------------------------------------
# Refuse: not a worktree at all (no .git entry)
# ---------------------------------------------------------------------------


def test_unsafe_when_path_is_not_a_worktree(not_a_worktree: Path) -> None:
    # Arrange — plain directory, no .git entry.
    # Act
    result = is_safe_to_reap(not_a_worktree)
    # Assert
    assert result is False


# ---------------------------------------------------------------------------
# Refuse: nonexistent path (defensive — the janitor may race a
# concurrent removal). Must not raise; must return False.
# ---------------------------------------------------------------------------


def test_unsafe_when_path_does_not_exist(tmp_path: Path) -> None:
    # Arrange — a path that was never created.
    missing = tmp_path / "ghost"
    # Act
    result = is_safe_to_reap(missing)
    # Assert
    assert result is False
