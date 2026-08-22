"""An IGNORED file must keep a worktree alive, exactly like an untracked one.

WHY THIS FILE EXISTS — a data-loss bug, measured 2026-08-20 with a control.

`worktree-gc` removes a worktree that is clean + merged + idle + old enough,
and it runs UNATTENDED on a timer. `is_clean` asked `git status --porcelain`,
which reports untracked files and says nothing about ignored ones:

    worktree holding only GITIGNORED/lessons.md
      git status --porcelain              ''               -> read CLEAN
      git status --porcelain --ignored    '!! GITIGNORED/'
      git worktree remove (no --force)    rc=0             -> NOTES GONE

    CONTROL, same position, an UNTRACKED file instead
      git status --porcelain              '?? scratch.txt' -> read DIRTY
      git worktree remove (no --force)    rc=128           -> refused

So untracked content had TWO independent protections — the predicate AND git
refusing — and ignored content had NEITHER. The intuition the old code rested
on is backwards: git guards the untracked file and deletes over the ignored
one without comment.

The gap lands where this fleet keeps its notes: CLAUDE.md instructs every
agent to write `GITIGNORED/tasks/todo.md` and `GITIGNORED/tasks/lessons.md`,
and `GITIGNORED/` is ignored by name. The module's header states the trade it
means to make — "REMOVE destroys work that exists nowhere else. We pick
annoying." — and this was the one class where it silently picked the other way.

THE CONTROLS ARE THE POINT, not decoration. A test that only asserts the
ignored case would pass against a predicate that called EVERYTHING dirty,
which would disable the gc entirely and look like a fix. The untracked arm
pins the pre-existing behaviour and the empty arm pins that a genuinely empty
worktree is still collectable.

PA-306: no mocks. Real `git init`, real worktrees, real ignored files.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scitex_agent_container._maintenance._worktree_gc_predicate import is_clean


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real repo whose .gitignore covers GITIGNORED/, as every fleet repo does."""
    root = tmp_path / "repo"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], capture_output=True)
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "t")
    (root / ".gitignore").write_text("GITIGNORED/\n")
    _git(root, "add", ".gitignore")
    _git(root, "commit", "-qm", "init")
    return root


def _worktree(repo: Path, name: str) -> Path:
    path = repo.parent / name
    _git(repo, "worktree", "add", "-q", "-b", name, str(path))
    return path


def test_a_worktree_holding_only_ignored_notes_is_not_clean(repo: Path):
    """The bug: agent notes read as an empty tree and were collected."""
    # Arrange — exactly what CLAUDE.md tells agents to write.
    wt = _worktree(repo, "ignored-only")
    (wt / "GITIGNORED").mkdir()
    (wt / "GITIGNORED" / "lessons.md").write_text("hours of notes\n")
    # Act
    clean, _reason = is_clean(str(wt))
    # Assert
    assert clean is False


def test_an_untracked_file_still_keeps_the_worktree(repo: Path):
    """CONTROL — the behaviour that already worked must not regress."""
    # Arrange
    wt = _worktree(repo, "untracked-only")
    (wt / "scratch.txt").write_text("x\n")
    # Act
    clean, _reason = is_clean(str(wt))
    # Assert
    assert clean is False


def test_a_genuinely_empty_worktree_is_still_collectable(repo: Path):
    """CONTROL — the fix must not disable the gc by calling everything dirty."""
    # Arrange
    wt = _worktree(repo, "empty")
    # Act
    clean, _reason = is_clean(str(wt))
    # Assert
    assert clean is True


def test_git_itself_would_have_deleted_the_ignored_notes(repo: Path):
    """Pins WHY the predicate must catch this: git does not.

    Without this, a reader could assume `git worktree remove` refuses over any
    leftover content and treat the predicate as belt-and-braces. It refuses for
    untracked files and not for ignored ones, which is the whole asymmetry.
    """
    # Arrange
    wt = _worktree(repo, "git-would-delete")
    (wt / "GITIGNORED").mkdir()
    (wt / "GITIGNORED" / "lessons.md").write_text("hours of notes\n")
    # Act
    removed = _git(repo, "worktree", "remove", str(wt))
    # Assert
    assert removed.returncode == 0
