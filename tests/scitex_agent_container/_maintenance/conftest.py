"""Real git repos for the worktree-GC suite — no mocks, no fakes.

Every fixture builds an ACTUAL repository with ACTUAL ``git worktree
add``, because the thing under test is a predicate whose whole job is to
read git correctly before deleting a directory. A faked ``git status``
would test our idea of git, which is exactly the idea that would be
wrong.

Two seams keep the suite hermetic without faking anything that matters:

* ``pr_*`` — the merged-PR lookup. Injected so no test touches the
  network; a real ``gh`` call from a temp repo would be slow, flaky, and
  answer about somebody else's repo.
* ``no_cwds`` / ``unknown_cwds`` — the ``/proc`` scan, injected so a test
  is not at the mercy of whatever else is running on the box. The in-use
  test deliberately does NOT inject: it spawns a real process and uses
  the real scanner.

Worktrees are created under ``tmp_path/worktrees/`` rather than the
production ``<repo>/.worktrees/`` on purpose — the GC enumerates via
``git worktree list``, and this proves it does not secretly depend on a
directory layout.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path

import pytest

#: Enough of a clock lie to clear the 24h age gate without backdating any
#: commit: the GC takes ``now`` as a seam, so a fresh commit read from 100
#: simulated hours in the future is genuinely "old" to every leg.
HOURS_100 = 100 * 3600


def git(repo: Path | str, *args: str) -> str:
    """A real ``git -C <repo> ...``. Raises on failure — a broken fixture
    must fail loudly at build time, not silently produce a repo whose
    shape nobody checked.
    """
    res = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout.strip()


def commit(tree: Path, filename: str, text: str, message: str) -> None:
    """Write a file and really commit it in ``tree``."""
    (tree / filename).write_text(text)
    git(tree, "add", filename)
    git(tree, "commit", "-qm", message)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real git repo on ``develop`` with one commit and an identity.

    Identity is set repo-locally (never ``--global``): the suite must not
    touch the machine's git config.
    """
    root = tmp_path / "repo"
    root.mkdir()
    git(root, "init", "-q", "-b", "develop")
    git(root, "config", "user.email", "worktree-gc-test@example.invalid")
    git(root, "config", "user.name", "Worktree GC Test")
    commit(root, "README.md", "seed\n", "seed")
    return root


@pytest.fixture
def add_worktree(repo: Path, tmp_path: Path):
    """Factory: a REAL ``git worktree add`` on a fresh branch.

    ``ahead`` adds a commit that exists only on the branch, which is what
    makes it a non-ancestor of ``develop`` — i.e. what an unmerged branch
    and a squash-merged branch look like from git's side (they are
    identical locally; only GitHub can tell them apart, which is why the
    merged leg needs both checks).
    """

    def _add(name: str, *, ahead: bool = False) -> Path:
        path = tmp_path / "worktrees" / name
        branch = f"feat/{name}"
        git(repo, "worktree", "add", "-q", "-b", branch, str(path), "develop")
        if ahead:
            commit(path, f"{name}.txt", f"{name} work\n", f"{name}: work")
        return path

    return _add


@pytest.fixture
def old_now() -> float:
    """A clock 100h ahead — every fresh commit reads as older than the gate."""
    return time.time() + HOURS_100


@pytest.fixture
def pr_yes():
    """Merged-PR seam answering YES — the squash-merged branch's truth."""
    return lambda repo, branch: True


@pytest.fixture
def pr_no():
    """Merged-PR seam answering NO — GitHub knows of no merged PR."""
    return lambda repo, branch: False


@pytest.fixture
def pr_unknown():
    """Merged-PR seam that could not answer (gh missing / offline / rate-limited)."""
    return lambda repo, branch: None


@pytest.fixture
def no_cwds():
    """/proc scan seam: readable, and nothing is running in any worktree."""
    return lambda: set()


@pytest.fixture
def unknown_cwds():
    """/proc scan seam: the signal is UNAVAILABLE (never 'nothing runs')."""
    return lambda: None
