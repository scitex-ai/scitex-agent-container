"""The completion installer must not write into a git-tracked rc file.

MEASURED 2026-08-20, found by the dotfiles agent while building the fleet git
sync. On every fleet host `~/.bashrc` is a SYMLINK:

    /home/ywatanabe/.bashrc -> /home/ywatanabe/.dotfiles/src/.bashrc

so appending to it modified a TRACKED file in the dotfiles repo. Four hosts
carried the same "local edit" — not four humans, one installer. Those checkouts
were permanently dirty, an ff-only pull would not apply, and that was a direct
contributor to the fleet running five different dotfiles heads.

No-mocks (PA-306): every test builds a REAL git repository on disk. The property
under test is how a SYMLINK into a repo is treated, and a fake cannot exhibit
that — it is exactly the thing the production code got wrong.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from scitex_agent_container.cli_pkg._completion_install import _tracked_in_a_git_repo


def _repo_with(tmp_path: Path, *, filename: str, tracked: bool) -> Path:
    """A real git repo containing ``filename``, committed or not."""
    repo = tmp_path / "repo"
    repo.mkdir()
    run = lambda *a: subprocess.run(  # noqa: E731 - local, one line, one use
        ["git", "-C", str(repo), *a], capture_output=True, check=True
    )
    run("init", "-q")
    run("config", "user.email", "t@example.invalid")
    run("config", "user.name", "t")
    (repo / filename).write_text("# rc\n")
    if tracked:
        run("add", filename)
        run("commit", "-qm", "add rc")
    return repo


def test_a_tracked_file_reports_its_repo_root(tmp_path: Path) -> None:
    # Arrange
    repo = _repo_with(tmp_path, filename=".bashrc", tracked=True)
    # Act
    found = _tracked_in_a_git_repo(repo / ".bashrc")
    # Assert
    assert found == repo


def test_a_symlink_into_a_repo_reports_the_repo_root(tmp_path: Path) -> None:
    """THE ACTUAL BUG SHAPE — the file opened is not the file named.

    Falsifiable, and it is the one that matters: drop the `.resolve()` from the
    implementation and this goes RED while every other test here stays green,
    because only this one names the file from outside the repo.
    """
    # Arrange
    repo = _repo_with(tmp_path, filename=".bashrc", tracked=True)
    link = tmp_path / "home-bashrc"
    link.symlink_to(repo / ".bashrc")
    # Act
    found = _tracked_in_a_git_repo(link)
    # Assert
    assert found == repo


def test_an_untracked_file_in_a_repo_is_not_refused(tmp_path: Path) -> None:
    """Being INSIDE a repo is not the condition — being tracked is.

    A user whose rc file merely sits in a repo directory, unversioned, has
    nothing to lose from an append, and refusing there would be a false alarm
    that sends them looking for a commit that does not exist.
    """
    # Arrange
    repo = _repo_with(tmp_path, filename=".bashrc", tracked=False)
    # Act
    found = _tracked_in_a_git_repo(repo / ".bashrc")
    # Assert
    assert found is None


def test_a_file_outside_any_repo_is_not_refused(tmp_path: Path) -> None:
    # Arrange
    plain = tmp_path / ".bashrc"
    plain.write_text("# rc\n")
    # Act
    found = _tracked_in_a_git_repo(plain)
    # Assert
    assert found is None


def test_a_missing_file_is_not_refused(tmp_path: Path) -> None:
    """The installer creates the rc file when absent; that path must stay open."""
    # Arrange
    absent = tmp_path / "never-created"
    # Act
    found = _tracked_in_a_git_repo(absent)
    # Assert
    assert found is None
