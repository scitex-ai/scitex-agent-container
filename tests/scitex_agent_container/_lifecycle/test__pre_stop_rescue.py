"""Tests for ``_lifecycle/_pre_stop_rescue`` — fleet-default rescue pass.

Operator priority (lead a2a ``efa48850daf248ed9fe3ae5232677b2b``): make
restart cheap. Walks the agent's worktrees, commits dirty changes,
pushes to non-protected branches, falls back to diff-tarballs when
push isn't possible — all bounded by a 60s grace timeout so the
rescue can never wedge a restart.

STX-TQ002 AAA + STX-TQ007 one-assert. No mocks — real ``tmp_path``
git repos exercised through real ``git`` invocations.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from scitex_agent_container._lifecycle._pre_stop_rescue import (
    RESCUE_DIR_NAME,
    is_protected_branch,
    rescue_worktree,
    rescue_worktrees_for_agent,
)

# ---------------------------------------------------------------------------
# Fixtures — real git environments, no monkeypatch
# ---------------------------------------------------------------------------


@pytest.fixture
def git_env_save_restore(tmp_path: Path):
    """Make git invocations deterministic + sandboxed.

    Pins commit author + committer so the test repos don't depend on
    the runner's global ``~/.gitconfig`` (CI runners often have none).
    Re-points ``HOME`` to ``tmp_path`` so any per-user git state lands
    in the tmp dir and is wiped with the test. Yields the prior
    environment values restored on teardown — explicit save/restore,
    NO ``monkeypatch`` fixture (PA-306 §3).
    """
    keys = [
        "HOME",
        "GIT_AUTHOR_NAME",
        "GIT_AUTHOR_EMAIL",
        "GIT_COMMITTER_NAME",
        "GIT_COMMITTER_EMAIL",
    ]
    saved = {k: os.environ.get(k) for k in keys}
    os.environ["HOME"] = str(tmp_path)
    for k, v in [
        ("GIT_AUTHOR_NAME", "Rescue Tester"),
        ("GIT_AUTHOR_EMAIL", "rescue@example.invalid"),
        ("GIT_COMMITTER_NAME", "Rescue Tester"),
        ("GIT_COMMITTER_EMAIL", "rescue@example.invalid"),
    ]:
        os.environ[k] = v
    try:
        yield
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def _git(args: list[str], *, cwd: Path) -> None:
    """Run ``git <args>`` synchronously; raise on non-zero so tests fail loud."""
    subprocess.check_call(["git", *args], cwd=str(cwd))


def _init_repo(root: Path, *, branch: str = "feature/topic") -> Path:
    """Create a fresh git repo at ``root`` on ``branch`` with one commit."""
    root.mkdir(parents=True, exist_ok=True)
    _git(["init", "--initial-branch=" + branch, "--quiet"], cwd=root)
    (root / "README.md").write_text("# tmp\n")
    _git(["add", "README.md"], cwd=root)
    _git(["commit", "-m", "init", "--quiet"], cwd=root)
    return root


def _make_dirty(root: Path) -> None:
    """Append a dirty change so ``git status --porcelain`` reports it."""
    (root / "README.md").write_text("# tmp\n# rescue-target\n")


# ---------------------------------------------------------------------------
# is_protected_branch — denylist policy
# ---------------------------------------------------------------------------


def test_is_protected_branch_main_blocks_push():
    # Arrange + Act + Assert (constant policy — single boolean answer)
    assert is_protected_branch("main") is True


def test_is_protected_branch_master_blocks_push():
    assert is_protected_branch("master") is True


def test_is_protected_branch_release_prefix_blocks_push():
    assert is_protected_branch("release/2.0") is True


def test_is_protected_branch_feature_branch_allows_push():
    assert is_protected_branch("feature/topic") is False


def test_is_protected_branch_release_notes_is_not_protected():
    # Arrange — guard the prefix-match against false positives on names
    # that just begin with the same letters but don't share the ``/``.
    assert is_protected_branch("release-notes") is False


def test_is_protected_branch_empty_branch_is_protected():
    # Arrange — detached HEAD / unknown branch returns "" from the
    # branch query; refuse to push the unclassifiable.
    assert is_protected_branch("") is True


# ---------------------------------------------------------------------------
# rescue_worktree — single repo, no remote → tarball fallback
# ---------------------------------------------------------------------------


def test_rescue_worktree_commits_dirty_changes(
    git_env_save_restore, tmp_path: Path
) -> None:
    # Arrange
    repo = _init_repo(tmp_path / "repo")
    _make_dirty(repo)
    rescue_root = tmp_path / "state" / RESCUE_DIR_NAME
    # Act
    result = rescue_worktree(
        repo,
        agent_name="agent-x",
        timestamp="20260613T000000Z",
        rescue_root=rescue_root,
        timeout=10.0,
    )
    # Assert
    assert result["committed"] is True


def test_rescue_worktree_writes_tarball_when_no_remote(
    git_env_save_restore, tmp_path: Path
) -> None:
    # Arrange — the repo has no ``origin`` remote configured, so push
    # MUST fail and the diff-tarball fallback MUST land.
    repo = _init_repo(tmp_path / "repo")
    _make_dirty(repo)
    rescue_root = tmp_path / "state" / RESCUE_DIR_NAME
    # Act
    result = rescue_worktree(
        repo,
        agent_name="agent-x",
        timestamp="20260613T000000Z",
        rescue_root=rescue_root,
        timeout=10.0,
    )
    # Assert
    assert isinstance(result.get("tarball"), Path) and Path(result["tarball"]).is_file()


def test_rescue_worktree_protected_branch_skips_push(
    git_env_save_restore, tmp_path: Path
) -> None:
    # Arrange — branch=main is denylisted; commit lands but push doesn't.
    repo = _init_repo(tmp_path / "repo", branch="main")
    _make_dirty(repo)
    rescue_root = tmp_path / "state" / RESCUE_DIR_NAME
    # Act
    result = rescue_worktree(
        repo,
        agent_name="agent-x",
        timestamp="20260613T000000Z",
        rescue_root=rescue_root,
        timeout=10.0,
    )
    # Assert
    assert result["pushed"] is False and result["protected"] is True


def test_rescue_worktree_protected_branch_still_commits(
    git_env_save_restore, tmp_path: Path
) -> None:
    # Arrange
    repo = _init_repo(tmp_path / "repo", branch="main")
    _make_dirty(repo)
    rescue_root = tmp_path / "state" / RESCUE_DIR_NAME
    # Act
    result = rescue_worktree(
        repo,
        agent_name="agent-x",
        timestamp="20260613T000000Z",
        rescue_root=rescue_root,
        timeout=10.0,
    )
    # Assert
    assert result["committed"] is True


def test_rescue_worktree_clean_worktree_no_op(
    git_env_save_restore, tmp_path: Path
) -> None:
    # Arrange — clean repo, nothing to rescue.
    repo = _init_repo(tmp_path / "repo")
    rescue_root = tmp_path / "state" / RESCUE_DIR_NAME
    # Act
    result = rescue_worktree(
        repo,
        agent_name="agent-x",
        timestamp="20260613T000000Z",
        rescue_root=rescue_root,
        timeout=10.0,
    )
    # Assert
    assert (
        result["committed"] is False
        and result["pushed"] is False
        and result["tarball"] is None
    )


def test_rescue_worktree_non_repo_returns_error(tmp_path: Path) -> None:
    # Arrange — plain directory, NOT a git repo.
    plain = tmp_path / "not-a-repo"
    plain.mkdir()
    rescue_root = tmp_path / "state" / RESCUE_DIR_NAME
    # Act
    result = rescue_worktree(
        plain,
        agent_name="agent-x",
        timestamp="20260613T000000Z",
        rescue_root=rescue_root,
        timeout=10.0,
    )
    # Assert
    assert result["error"] == "not a git worktree"


# ---------------------------------------------------------------------------
# rescue_worktrees_for_agent — walks .worktrees/* + worktrees/*
# ---------------------------------------------------------------------------


def test_rescue_for_agent_finds_dotworktrees_subagent(
    git_env_save_restore, tmp_path: Path
) -> None:
    # Arrange — primary workdir is clean; a .worktrees/agent-foo sibling
    # carries the dirty work that restart would otherwise destroy.
    workdir = _init_repo(tmp_path / "wd")
    sub = _init_repo(workdir / ".worktrees" / "agent-foo")
    _make_dirty(sub)
    state_dir = tmp_path / "state"
    # Act
    results = rescue_worktrees_for_agent(
        agent_name="parent",
        workdir=workdir,
        state_dir=state_dir,
    )
    # Assert — the subagent worktree's result records a commit.
    sub_results = [r for r in results if Path(r["path"]).samefile(sub)]
    assert sub_results and sub_results[0]["committed"] is True


def test_rescue_for_agent_finds_legacy_worktrees_dir(
    git_env_save_restore, tmp_path: Path
) -> None:
    # Arrange — legacy worktrees/legacy-feat carries the dirty work.
    workdir = _init_repo(tmp_path / "wd")
    legacy = _init_repo(workdir / "worktrees" / "legacy-feat")
    _make_dirty(legacy)
    state_dir = tmp_path / "state"
    # Act
    results = rescue_worktrees_for_agent(
        agent_name="parent",
        workdir=workdir,
        state_dir=state_dir,
    )
    # Assert
    legacy_results = [r for r in results if Path(r["path"]).samefile(legacy)]
    assert legacy_results and legacy_results[0]["committed"] is True


def test_rescue_for_agent_clean_workdir_returns_no_op_entries(
    git_env_save_restore, tmp_path: Path
) -> None:
    # Arrange — single clean repo, no subagent worktrees.
    workdir = _init_repo(tmp_path / "wd")
    state_dir = tmp_path / "state"
    # Act
    results = rescue_worktrees_for_agent(
        agent_name="parent",
        workdir=workdir,
        state_dir=state_dir,
    )
    # Assert — at least one entry (the primary workdir) and none committed.
    committed = [r for r in results if r["committed"]]
    assert committed == []


def test_rescue_for_agent_respects_grace_budget(
    git_env_save_restore, tmp_path: Path
) -> None:
    # Arrange — tiny grace budget guarantees the loop's deadline check
    # short-circuits before scanning every worktree, recording the
    # ``grace budget elapsed`` skip path.
    workdir = _init_repo(tmp_path / "wd")
    for i in range(3):
        sub = _init_repo(workdir / ".worktrees" / f"agent-{i}")
        _make_dirty(sub)
    state_dir = tmp_path / "state"
    # Act
    results = rescue_worktrees_for_agent(
        agent_name="parent",
        workdir=workdir,
        state_dir=state_dir,
        grace_seconds=0.0,  # impossible budget → first deadline check fires
    )
    # Assert — at least one result carries the budget-elapsed marker.
    elapsed = [r for r in results if r.get("error") == "grace budget elapsed"]
    assert elapsed != []


def test_rescue_for_agent_rescue_dir_named_correctly(
    git_env_save_restore, tmp_path: Path
) -> None:
    # Arrange — make sure the rescue dir lands under ``state_dir/rescue/``
    # (the documented operator path so they can find the diff-tarballs).
    workdir = _init_repo(tmp_path / "wd")
    _make_dirty(workdir)
    state_dir = tmp_path / "state"
    # Act
    rescue_worktrees_for_agent(
        agent_name="parent",
        workdir=workdir,
        state_dir=state_dir,
    )
    # Assert
    assert (state_dir / RESCUE_DIR_NAME).is_dir()
