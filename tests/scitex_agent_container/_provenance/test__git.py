"""Tests for reading a git HEAD sha without shelling out.

Real git repositories, built with real ``git`` commands in ``tmp_path``.
The subprocess calls are ARRANGE — they construct the fixture on disk. The
code under test never forks a process; that is the entire point of it
(``git rev-parse`` costs ~89 ms, reading ``.git`` costs ~0.44 ms).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scitex_agent_container._provenance._git import (
    git_dir_for,
    head_sha,
    repo_root_for_package,
)


def _git(repo: Path, *args: str) -> str:
    """Run a real git command against ``repo`` (fixture construction only)."""
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A real git checkout with exactly one commit."""
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "file.txt").write_text("hello\n")
    _git(root, "add", "file.txt")
    _git(root, "commit", "-q", "-m", "initial")
    return root


class TestHeadSha:
    def test_reads_the_sha_at_head(self, repo: Path):
        # Arrange
        expected = _git(repo, "rev-parse", "HEAD")

        # Act
        found = head_sha(repo)

        # Assert
        assert found == expected

    def test_reads_the_sha_from_packed_refs(self, repo: Path):
        # Arrange — `git pack-refs` removes the loose ref file, so the
        # sha is only reachable through packed-refs.
        expected = _git(repo, "rev-parse", "HEAD")
        _git(repo, "pack-refs", "--all")

        # Act
        found = head_sha(repo)

        # Assert
        assert found == expected

    def test_reads_the_sha_from_a_detached_head(self, repo: Path):
        # Arrange — HEAD then holds the sha directly, not a ref.
        expected = _git(repo, "rev-parse", "HEAD")
        _git(repo, "checkout", "-q", "--detach")

        # Act
        found = head_sha(repo)

        # Assert
        assert found == expected

    def test_reads_the_sha_from_a_linked_worktree(self, repo: Path, tmp_path: Path):
        # Arrange — a `git worktree`'s .git is a FILE holding `gitdir:`,
        # which is exactly how agents check this repo out.
        linked = tmp_path / "linked"
        _git(repo, "worktree", "add", "-q", "-b", "topic", str(linked))
        expected = _git(linked, "rev-parse", "HEAD")

        # Act
        found = head_sha(linked)

        # Assert
        assert found == expected

    def test_returns_none_outside_a_checkout(self, tmp_path: Path):
        # Arrange
        plain = tmp_path / "not-a-repo"
        plain.mkdir()

        # Act
        found = head_sha(plain)

        # Assert
        assert found is None


class TestGitDirFor:
    def test_resolves_the_gitdir_of_a_linked_worktree(self, repo: Path, tmp_path: Path):
        # Arrange
        linked = tmp_path / "linked"
        _git(repo, "worktree", "add", "-q", "-b", "topic", str(linked))

        # Act
        found = git_dir_for(linked)

        # Assert
        assert found is not None and found.is_dir()


class TestRepoRootForPackage:
    def test_finds_the_root_of_a_src_layout_checkout(self, repo: Path):
        # Arrange
        package = repo / "src" / "scitex_agent_container"
        package.mkdir(parents=True)

        # Act
        found = repo_root_for_package(package)

        # Assert
        assert found == repo

    def test_ignores_a_package_that_is_not_under_src(self, repo: Path):
        # Arrange — a wheel unpacked into a dir that merely happens to sit
        # inside a git repo must NOT be read as a source checkout, or it
        # would report a commit unrelated to the installed code.
        package = repo / "site-packages" / "scitex_agent_container"
        package.mkdir(parents=True)

        # Act
        found = repo_root_for_package(package)

        # Assert
        assert found is None

    def test_ignores_a_src_layout_that_is_not_a_checkout(self, tmp_path: Path):
        # Arrange
        package = tmp_path / "elsewhere" / "src" / "scitex_agent_container"
        package.mkdir(parents=True)

        # Act
        found = repo_root_for_package(package)

        # Assert
        assert found is None

# EOF
