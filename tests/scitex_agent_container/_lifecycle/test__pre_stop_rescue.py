"""Tests for ``_lifecycle/_pre_stop_rescue`` — fleet-default rescue pass.

Operator priority (lead a2a ``efa48850daf248ed9fe3ae5232677b2b``): make
restart cheap. Walks the agent's worktrees and commits dirty changes
LOCALLY — on a topic branch in place, on a protected branch onto a
``rescue/`` side-branch — falling back to a diff-tarball only when the
commit itself fails, all bounded by a 60s grace timeout so the rescue
can never wedge a restart.

*** The rescue NEVER pushes *** (operator ruling 2026-07-17,
「プッシュはなしじゃない？」). ``test_rescue_never_pushes_*`` below are
the regression guards: they exercise the rescue against a REAL origin
and assert nothing ever lands on it. If someone re-introduces a push,
those go RED — which is the whole point of writing them.

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


def _git_out(args: list[str], *, cwd: Path) -> str:
    """Run ``git <args>`` and return stdout (raises on non-zero — fail loud)."""
    return subprocess.check_output(["git", *args], cwd=str(cwd), text=True)


def _init_repo_with_origin(
    root: Path, remote: Path, *, branch: str = "develop"
) -> Path:
    """Create a repo on ``branch`` wired to a bare ``origin`` with ``branch`` pushed."""
    remote.mkdir(parents=True, exist_ok=True)
    _git(["init", "--bare", "--quiet"], cwd=remote)
    _init_repo(root, branch=branch)
    _git(["remote", "add", "origin", str(remote)], cwd=root)
    _git(["push", "-u", "origin", branch, "--quiet"], cwd=root)
    return root


# ---------------------------------------------------------------------------
# is_protected_branch — denylist policy
# ---------------------------------------------------------------------------


def test_is_protected_branch_main_blocks_push():
    # Arrange
    branch = "main"
    # Act
    result = is_protected_branch(branch)
    # Assert
    assert result is True


def test_is_protected_branch_master_blocks_push():
    # Arrange
    branch = "master"
    # Act
    result = is_protected_branch(branch)
    # Assert
    assert result is True


def test_is_protected_branch_release_prefix_blocks_push():
    # Arrange
    branch = "release/2.0"
    # Act
    result = is_protected_branch(branch)
    # Assert
    assert result is True


def test_is_protected_branch_feature_branch_allows_push():
    # Arrange
    branch = "feature/topic"
    # Act
    result = is_protected_branch(branch)
    # Assert
    assert result is False


def test_is_protected_branch_release_notes_is_not_protected():
    # Arrange — guard the prefix-match against false positives on names
    # that just begin with the same letters but don't share the ``/``.
    branch = "release-notes"
    # Act
    result = is_protected_branch(branch)
    # Assert
    assert result is False


def test_is_protected_branch_develop_is_protected():
    # Arrange — develop is the shared work checkout; a rescue commit here
    # diverges it from origin and breaks ff-only pull (the root-cause bug).
    branch = "develop"
    # Act
    result = is_protected_branch(branch)
    # Assert
    assert result is True


def test_is_protected_branch_empty_branch_is_protected():
    # Arrange — detached HEAD / unknown branch returns "" from the
    # branch query; refuse to push the unclassifiable.
    branch = ""
    # Act
    result = is_protected_branch(branch)
    # Assert
    assert result is True


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


def test_rescue_worktree_no_remote_still_commits_locally(
    git_env_save_restore, tmp_path: Path
) -> None:
    # Arrange — no ``origin`` remote at all. Before 2026-07-17 this
    # forced the push to fail and a diff-tarball to land. The rescue no
    # longer pushes, so a missing remote is not a failure mode: the
    # LOCAL commit is the save, and it is durable on the host bind.
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
    assert result["committed"] is True and result["tarball"] is None


def test_rescue_worktree_writes_tarball_when_commit_fails(
    git_env_save_restore, tmp_path: Path
) -> None:
    # Arrange — a real pre-commit hook that refuses. ``commit_dirty``
    # does not pass ``--no-verify``, so ``git commit`` genuinely exits
    # non-zero and the dirty tree is left UNCOMMITTED. That is the one
    # case where the tarball is the only copy of the work, and it must
    # land. (No mock: the hook is a real file git really executes.)
    repo = _init_repo(tmp_path / "repo")
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
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


def test_rescue_worktree_protected_branch_is_flagged_protected(
    git_env_save_restore, tmp_path: Path
) -> None:
    # Arrange — branch=main is denylisted.
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
    assert result["protected"] is True


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
    assert result["committed"] is False and result["tarball"] is None


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
    # Arrange — the rescue dir must land under ``state_dir/rescue/`` (the
    # documented operator path for diff-tarballs). Since the rescue no
    # longer pushes, a tarball is written ONLY when the commit itself
    # fails — so force that with a real pre-commit hook exiting non-zero,
    # which is exactly the case the operator needs to find on disk.
    workdir = _init_repo(tmp_path / "wd")
    hook = workdir / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
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


# ---------------------------------------------------------------------------
# rescue_worktree on a PROTECTED branch — route to rescue/ side-branch,
# NEVER leave a local-only commit on develop/main (root-cause fix)
# ---------------------------------------------------------------------------


def test_rescue_protected_develop_no_local_only_commit(
    git_env_save_restore, tmp_path: Path
) -> None:
    # Arrange — dirty checkout on develop wired to an origin remote.
    repo = _init_repo_with_origin(tmp_path / "repo", tmp_path / "remote.git")
    _make_dirty(repo)
    rescue_root = tmp_path / "state" / RESCUE_DIR_NAME
    # Act
    rescue_worktree(
        repo,
        agent_name="agent-x",
        timestamp="20260709T000000Z",
        rescue_root=rescue_root,
        timeout=15.0,
    )
    # Assert — develop carries NO commit that origin/develop lacks.
    ahead = _git_out(["rev-list", "origin/develop..develop"], cwd=repo)
    assert ahead.strip() == ""


def test_rescue_protected_develop_pull_ff_only_succeeds(
    git_env_save_restore, tmp_path: Path
) -> None:
    # Arrange
    repo = _init_repo_with_origin(tmp_path / "repo", tmp_path / "remote.git")
    _make_dirty(repo)
    rescue_root = tmp_path / "state" / RESCUE_DIR_NAME
    # Act
    rescue_worktree(
        repo,
        agent_name="agent-x",
        timestamp="20260709T000000Z",
        rescue_root=rescue_root,
        timeout=15.0,
    )
    # Assert — the exact operation the bug broke now succeeds (rc 0).
    rc = subprocess.call(["git", "pull", "--ff-only", "--quiet"], cwd=str(repo))
    assert rc == 0


def test_rescue_protected_develop_worktree_clean_after(
    git_env_save_restore, tmp_path: Path
) -> None:
    # Arrange
    repo = _init_repo_with_origin(tmp_path / "repo", tmp_path / "remote.git")
    _make_dirty(repo)
    rescue_root = tmp_path / "state" / RESCUE_DIR_NAME
    # Act
    rescue_worktree(
        repo,
        agent_name="agent-x",
        timestamp="20260709T000000Z",
        rescue_root=rescue_root,
        timeout=15.0,
    )
    # Assert — checkout restored to a clean tree (porcelain empty).
    porcelain = _git_out(["status", "--porcelain"], cwd=repo)
    assert porcelain.strip() == ""


def test_rescue_protected_develop_still_on_develop_after(
    git_env_save_restore, tmp_path: Path
) -> None:
    # Arrange
    repo = _init_repo_with_origin(tmp_path / "repo", tmp_path / "remote.git")
    _make_dirty(repo)
    rescue_root = tmp_path / "state" / RESCUE_DIR_NAME
    # Act
    rescue_worktree(
        repo,
        agent_name="agent-x",
        timestamp="20260709T000000Z",
        rescue_root=rescue_root,
        timeout=15.0,
    )
    # Assert — the checkout ends back on the original protected branch.
    branch = _git_out(["rev-parse", "--abbrev-ref", "HEAD"], cwd=repo)
    assert branch.strip() == "develop"


def test_rescue_protected_develop_work_preserved_on_rescue_branch(
    git_env_save_restore, tmp_path: Path
) -> None:
    # Arrange
    repo = _init_repo_with_origin(tmp_path / "repo", tmp_path / "remote.git")
    _make_dirty(repo)
    rescue_root = tmp_path / "state" / RESCUE_DIR_NAME
    # Act
    result = rescue_worktree(
        repo,
        agent_name="agent-x",
        timestamp="20260709T000000Z",
        rescue_root=rescue_root,
        timeout=15.0,
    )
    # Assert — the rescue branch's tip carries the dirty content.
    content = _git_out(["show", f"{result['rescue_branch']}:README.md"], cwd=repo)
    assert "rescue-target" in content


def test_rescue_protected_develop_rescue_branch_named_by_convention(
    git_env_save_restore, tmp_path: Path
) -> None:
    # Arrange
    repo = _init_repo_with_origin(tmp_path / "repo", tmp_path / "remote.git")
    _make_dirty(repo)
    rescue_root = tmp_path / "state" / RESCUE_DIR_NAME
    # Act
    result = rescue_worktree(
        repo,
        agent_name="agent-x",
        timestamp="20260709T000000Z",
        rescue_root=rescue_root,
        timeout=15.0,
    )
    # Assert
    assert result["rescue_branch"] == "rescue/agent-x-20260709T000000Z"


def test_rescue_never_pushes_protected_rescue_branch_to_origin(
    git_env_save_restore, tmp_path: Path
) -> None:
    # Arrange — a REAL origin is present and reachable, so a push WOULD
    # succeed if the code attempted one. This is the regression guard for
    # the operator's no-push ruling: the rescue side-branch must exist
    # locally but must NOT appear on origin.
    repo = _init_repo_with_origin(tmp_path / "repo", tmp_path / "remote.git")
    _make_dirty(repo)
    rescue_root = tmp_path / "state" / RESCUE_DIR_NAME
    # Act
    result = rescue_worktree(
        repo,
        agent_name="agent-x",
        timestamp="20260709T000000Z",
        rescue_root=rescue_root,
        timeout=15.0,
    )
    # Assert — origin does NOT carry the rescue branch (nothing pushed).
    remote_heads = _git_out(
        ["ls-remote", "--heads", "origin", str(result["rescue_branch"])],
        cwd=repo,
    )
    assert remote_heads.strip() == ""


def test_rescue_never_pushes_topic_branch_to_origin(
    git_env_save_restore, tmp_path: Path
) -> None:
    # Arrange — a topic branch with a REAL reachable origin. A foreign
    # worktree parked on a topic branch is exactly the case that got a
    # peer's work force-pushed (observed 2026-07-17). The rescue commit
    # lands locally; origin's topic branch must stay at its init tip.
    repo = _init_repo_with_origin(
        tmp_path / "repo", tmp_path / "remote.git", branch="feature/topic"
    )
    tip_before = _git_out(["ls-remote", "origin", "feature/topic"], cwd=repo).split()[0]
    _make_dirty(repo)
    rescue_root = tmp_path / "state" / RESCUE_DIR_NAME
    # Act
    rescue_worktree(
        repo,
        agent_name="agent-x",
        timestamp="20260709T000000Z",
        rescue_root=rescue_root,
        timeout=15.0,
    )
    # Assert — origin's topic branch is unmoved (the local commit never shipped).
    tip_after = _git_out(["ls-remote", "origin", "feature/topic"], cwd=repo).split()[0]
    assert tip_after == tip_before


def test_rescue_protected_develop_no_remote_commits_to_side_branch(
    git_env_save_restore, tmp_path: Path
) -> None:
    # Arrange — develop, NO origin. Since the rescue no longer pushes, a
    # missing remote is not a failure: the side-branch commit is the save
    # and no tarball is needed (the commit succeeded).
    repo = _init_repo(tmp_path / "repo", branch="develop")
    _make_dirty(repo)
    rescue_root = tmp_path / "state" / RESCUE_DIR_NAME
    # Act
    result = rescue_worktree(
        repo,
        agent_name="agent-x",
        timestamp="20260709T000000Z",
        rescue_root=rescue_root,
        timeout=15.0,
    )
    # Assert — committed on the side-branch, no tarball fallback triggered.
    assert result["committed"] is True and result["tarball"] is None


def test_rescue_protected_develop_no_remote_keeps_develop_clean(
    git_env_save_restore, tmp_path: Path
) -> None:
    # Arrange — no remote; capture develop's tip before + after.
    repo = _init_repo(tmp_path / "repo", branch="develop")
    tip_before = _git_out(["rev-parse", "develop"], cwd=repo).strip()
    _make_dirty(repo)
    rescue_root = tmp_path / "state" / RESCUE_DIR_NAME
    # Act
    rescue_worktree(
        repo,
        agent_name="agent-x",
        timestamp="20260709T000000Z",
        rescue_root=rescue_root,
        timeout=15.0,
    )
    # Assert — develop's tip is unchanged (no rescue commit landed on it).
    tip_after = _git_out(["rev-parse", "develop"], cwd=repo).strip()
    assert tip_after == tip_before


def test_rescue_protected_develop_no_remote_preserves_on_rescue_branch(
    git_env_save_restore, tmp_path: Path
) -> None:
    # Arrange — no remote: the local rescue branch is the durable copy.
    repo = _init_repo(tmp_path / "repo", branch="develop")
    _make_dirty(repo)
    rescue_root = tmp_path / "state" / RESCUE_DIR_NAME
    # Act
    result = rescue_worktree(
        repo,
        agent_name="agent-x",
        timestamp="20260709T000000Z",
        rescue_root=rescue_root,
        timeout=15.0,
    )
    # Assert — the local rescue branch still carries the dirty content.
    content = _git_out(["show", f"{result['rescue_branch']}:README.md"], cwd=repo)
    assert "rescue-target" in content


def test_rescue_nonprotected_topic_branch_commits_in_place(
    git_env_save_restore, tmp_path: Path
) -> None:
    # Arrange — a normal topic branch: the rescue commit lands IN PLACE,
    # existing behavior, no side-branch.
    repo = _init_repo(tmp_path / "repo", branch="feature/topic")
    _make_dirty(repo)
    rescue_root = tmp_path / "state" / RESCUE_DIR_NAME
    # Act
    result = rescue_worktree(
        repo,
        agent_name="agent-x",
        timestamp="20260709T000000Z",
        rescue_root=rescue_root,
        timeout=15.0,
    )
    # Assert — no rescue side-branch was created for a topic branch.
    assert result["rescue_branch"] == ""


def test_rescue_nonprotected_topic_branch_leaves_commit_on_that_branch(
    git_env_save_restore, tmp_path: Path
) -> None:
    # Arrange
    repo = _init_repo(tmp_path / "repo", branch="feature/topic")
    _make_dirty(repo)
    rescue_root = tmp_path / "state" / RESCUE_DIR_NAME
    # Act
    rescue_worktree(
        repo,
        agent_name="agent-x",
        timestamp="20260709T000000Z",
        rescue_root=rescue_root,
        timeout=15.0,
    )
    # Assert — HEAD's newest commit is the rescue autosave on feature/topic.
    subject = _git_out(["log", "-1", "--pretty=%s"], cwd=repo)
    assert subject.strip() == "rescue: pre-stop autosave agent-x@20260709T000000Z"
