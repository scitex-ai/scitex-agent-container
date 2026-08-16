"""Tests for ``sac agents prune-claude`` (F-CS8 maintenance, 2026-06-03).

Real I/O against a tmp_path filesystem. The git-aware worktrees path uses
real ``git`` invocations against tmp repos initialised inline — no mocks.
Pending-record path uses real file mtime manipulation. AAA markers + one
assert per test.
"""

from __future__ import annotations

from tests.scitex_agent_container._helpers.explicit_spec import explicit_spec

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

# Module-level capability probe: the FD-held predicate is defence-in-
# depth and relies on ``lsof``. When ``lsof`` is missing the predicate
# falls through by design, so the test below SKIPS rather than fails —
# moved to a decorator so the function body keeps a single assertion
# (an in-body ``pytest.skip(...)`` call counts toward TQ007).
_LSOF_AVAILABLE = shutil.which("lsof") is not None

from scitex_agent_container.cli_pkg._agent_prune_claude import (
    apply_plan,
    plan_prune,
    prune_claude,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _set_mtime_days_ago(path: Path, days: float) -> None:
    """Set both atime + mtime to ``days`` days in the past."""
    target = time.time() - days * 86400
    os.utime(path, (target, target))


def _populate_pending(workdir: Path, count: int, age_days: float) -> list[Path]:
    """Drop ``count`` ``toolu_*.json`` records with mtime ``age_days`` old."""
    pending = workdir / ".claude" / "hooks" / "pre-tool-use" / ".pending"
    pending.mkdir(parents=True, exist_ok=True)
    paths = []
    for i in range(count):
        p = pending / f"toolu_{i}.json"
        p.write_text(json.dumps({"stage": f"x{i}"}))
        _set_mtime_days_ago(p, age_days)
        paths.append(p)
    return paths


def _init_git_repo(workdir: Path) -> None:
    """Initialise a minimal git repo with develop+main branches at HEAD."""
    subprocess.run(["git", "init", "-q"], cwd=workdir, check=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"], cwd=workdir, check=True
    )
    subprocess.run(["git", "config", "user.name", "Test"], cwd=workdir, check=True)
    # remote.origin.{url,fetch} are needed so `git rev-parse @{u}` and
    # `git log @{u}..HEAD` (used by the unpushed-commits predicate)
    # resolve against the simulated origin/* refs we plant below. The
    # URL is dummy — we never actually fetch — but git's @{u}
    # machinery refuses to resolve without a configured remote.
    subprocess.run(
        ["git", "config", "remote.origin.url", "file:///nonexistent"],
        cwd=workdir,
        check=True,
    )
    subprocess.run(
        [
            "git",
            "config",
            "remote.origin.fetch",
            "+refs/heads/*:refs/remotes/origin/*",
        ],
        cwd=workdir,
        check=True,
    )
    (workdir / "README.md").write_text("seed")
    subprocess.run(["git", "add", "."], cwd=workdir, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "seed"], cwd=workdir, check=True)
    # Create the "remote" refs the planner checks against.
    for ref in ("origin/develop", "origin/main"):
        subprocess.run(
            ["git", "update-ref", f"refs/remotes/{ref}", "HEAD"],
            cwd=workdir,
            check=True,
        )


def _make_merged_worktree(workdir: Path, name: str) -> Path:
    """Create a worktree whose branch's HEAD == origin/develop (= merged)."""
    branch = f"merged-{name}"
    target = workdir / ".claude" / "worktrees" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", "-b", branch, str(target)],
        cwd=workdir,
        check=True,
    )
    return target


def _make_unmerged_worktree(workdir: Path, name: str) -> Path:
    """Create a worktree on a branch with a NEW commit (= not merged)."""
    target = _make_merged_worktree(workdir, name)
    # Add a commit so its branch's HEAD is NOT an ancestor of develop/main.
    (target / "novel.txt").write_text("novel")
    subprocess.run(["git", "add", "novel.txt"], cwd=target, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "novel"], cwd=target, check=True)
    return target


def _lock_worktree(workdir: Path, name: str) -> None:
    target = workdir / ".claude" / "worktrees" / name
    subprocess.run(["git", "worktree", "lock", str(target)], cwd=workdir, check=True)


# ---------------------------------------------------------------------------
# plan_prune — pending records
# ---------------------------------------------------------------------------


def test_plan_pending_picks_records_older_than_threshold(tmp_path: Path):
    # Arrange — 3 records aged 10 days; threshold 7 days.
    workdir = tmp_path / "wd"
    workdir.mkdir()
    _populate_pending(workdir, count=3, age_days=10)
    # Act
    plan = plan_prune(workdir, pending_age_days=7)
    # Assert
    assert len(plan.pending) == 3


def test_plan_pending_skips_records_younger_than_threshold(tmp_path: Path):
    # Arrange — 3 records aged 2 days; threshold 7 days.
    workdir = tmp_path / "wd"
    workdir.mkdir()
    _populate_pending(workdir, count=3, age_days=2)
    # Act
    plan = plan_prune(workdir, pending_age_days=7)
    # Assert
    assert plan.pending == ()


def test_plan_pending_records_include_file_path(tmp_path: Path):
    # Arrange
    workdir = tmp_path / "wd"
    workdir.mkdir()
    paths = _populate_pending(workdir, count=1, age_days=30)
    # Act
    plan = plan_prune(workdir, pending_age_days=7)
    # Assert
    assert plan.pending[0].path == str(paths[0])


def test_plan_returns_empty_when_pending_dir_missing(tmp_path: Path):
    # Arrange — no .claude/ tree at all.
    workdir = tmp_path / "wd"
    workdir.mkdir()
    # Act
    plan = plan_prune(workdir, pending_age_days=7)
    # Assert
    assert plan.pending == ()


# ---------------------------------------------------------------------------
# plan_prune — worktrees
# ---------------------------------------------------------------------------


def test_plan_worktrees_picks_merged_unlocked(tmp_path: Path):
    # Arrange
    workdir = tmp_path / "wd"
    workdir.mkdir()
    _init_git_repo(workdir)
    _make_merged_worktree(workdir, "agent-merged")
    # Act
    plan = plan_prune(workdir, pending_age_days=7)
    paths = {Path(e.path).name for e in plan.worktrees}
    # Assert
    assert "agent-merged" in paths


def test_plan_worktrees_skips_unmerged(tmp_path: Path):
    # Arrange
    workdir = tmp_path / "wd"
    workdir.mkdir()
    _init_git_repo(workdir)
    _make_unmerged_worktree(workdir, "agent-unmerged")
    # Act
    plan = plan_prune(workdir, pending_age_days=7)
    paths = {Path(e.path).name for e in plan.worktrees}
    # Assert
    assert "agent-unmerged" not in paths


def test_plan_worktrees_skips_locked(tmp_path: Path):
    # Arrange
    workdir = tmp_path / "wd"
    workdir.mkdir()
    _init_git_repo(workdir)
    _make_merged_worktree(workdir, "agent-locked")
    _lock_worktree(workdir, "agent-locked")
    # Act
    plan = plan_prune(workdir, pending_age_days=7)
    paths = {Path(e.path).name for e in plan.worktrees}
    # Assert
    assert "agent-locked" not in paths


def test_plan_worktrees_records_skip_reason_for_unmerged(tmp_path: Path):
    # Arrange
    workdir = tmp_path / "wd"
    workdir.mkdir()
    _init_git_repo(workdir)
    _make_unmerged_worktree(workdir, "agent-unmerged-2")
    # Act
    plan = plan_prune(workdir, pending_age_days=7)
    skip_reasons = [
        s.reason for s in plan.skipped if Path(s.path).name == "agent-unmerged-2"
    ]
    # Assert
    assert any("not merged" in r for r in skip_reasons)


def test_plan_worktrees_records_skip_reason_for_locked(tmp_path: Path):
    # Arrange
    workdir = tmp_path / "wd"
    workdir.mkdir()
    _init_git_repo(workdir)
    _make_merged_worktree(workdir, "agent-locked-2")
    _lock_worktree(workdir, "agent-locked-2")
    # Act
    plan = plan_prune(workdir, pending_age_days=7)
    skip_reasons = [
        s.reason for s in plan.skipped if Path(s.path).name == "agent-locked-2"
    ]
    # Assert
    assert any("locked" in r for r in skip_reasons)


# ---------------------------------------------------------------------------
# plan_prune — safety predicates (lead 2026-06-06 after clew incident)
#
# Three predicates inserted BEFORE the merged-into-base check so a
# worktree with uncommitted / unpushed / in-use work is preserved
# even when its branch tip is fully merged. Each test exercises one
# predicate in isolation against a worktree that would otherwise be
# eligible (merged + unlocked + branched), confirming the new check
# moves it to the skip list with a recognizable reason.
# ---------------------------------------------------------------------------


def test_plan_worktrees_skips_uncommitted_unstaged_changes(tmp_path: Path):
    # Arrange — merged worktree with an uncommitted unstaged edit.
    workdir = tmp_path / "wd"
    workdir.mkdir()
    _init_git_repo(workdir)
    target = _make_merged_worktree(workdir, "agent-uncommitted-unstaged")
    (target / "README.md").write_text("dirty work-in-progress\n")  # unstaged edit
    # Act
    plan = plan_prune(workdir, pending_age_days=7)
    candidate_names = {Path(e.path).name for e in plan.worktrees}
    # Assert
    assert "agent-uncommitted-unstaged" not in candidate_names


def test_plan_worktrees_records_skip_reason_for_uncommitted(tmp_path: Path):
    # Arrange
    workdir = tmp_path / "wd"
    workdir.mkdir()
    _init_git_repo(workdir)
    target = _make_merged_worktree(workdir, "agent-uncommitted-reason")
    (target / "README.md").write_text("dirty\n")
    # Act
    plan = plan_prune(workdir, pending_age_days=7)
    reasons = [
        s.reason
        for s in plan.skipped
        if Path(s.path).name == "agent-uncommitted-reason"
    ]
    # Assert
    assert any("uncommitted" in r for r in reasons)


def test_plan_worktrees_skips_uncommitted_staged_changes(tmp_path: Path):
    # Arrange — merged worktree with a staged-but-uncommitted edit.
    workdir = tmp_path / "wd"
    workdir.mkdir()
    _init_git_repo(workdir)
    target = _make_merged_worktree(workdir, "agent-uncommitted-staged")
    (target / "README.md").write_text("staged work\n")
    subprocess.run(["git", "add", "README.md"], cwd=target, check=True)
    # Act
    plan = plan_prune(workdir, pending_age_days=7)
    candidate_names = {Path(e.path).name for e in plan.worktrees}
    # Assert
    assert "agent-uncommitted-staged" not in candidate_names


def test_plan_worktrees_skips_unpushed_commits(tmp_path: Path):
    # Arrange — merged worktree with a clean tree but an upstream-
    # tracking branch whose HEAD is one commit AHEAD of the recorded
    # upstream. We set the upstream by hand via update-ref so we don't
    # need a real remote in the test repo.
    workdir = tmp_path / "wd"
    workdir.mkdir()
    _init_git_repo(workdir)
    target = _make_merged_worktree(workdir, "agent-unpushed")
    # Find the worktree's actual branch name (the wrapper picks
    # ``merged-<name>``) and bind it to origin/<branch> as upstream.
    branch = subprocess.run(
        ["git", "-C", str(target), "rev-parse", "--abbrev-ref", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        [
            "git",
            "-C",
            str(workdir),
            "update-ref",
            f"refs/remotes/origin/{branch}",
            "HEAD",
        ],
        check=True,
    )
    # ``git branch --set-upstream-to=origin/<branch>`` validates that the
    # upstream ref points at a "real" branch (it walks `git remote show
    # origin` style state, not just the local refs/remotes/ entry). With
    # only `git update-ref` above, that validation rejects the tracking
    # setup. Write the branch.<name>.{remote,merge} config directly —
    # `git log @{u}..HEAD` consults exactly those two config keys to
    # resolve `@{u}`, which is all the predicate under test depends on.
    subprocess.run(
        ["git", "-C", str(target), "config", f"branch.{branch}.remote", "origin"],
        check=True,
    )
    subprocess.run(
        [
            "git",
            "-C",
            str(target),
            "config",
            f"branch.{branch}.merge",
            f"refs/heads/{branch}",
        ],
        check=True,
    )
    # Now commit a local change so HEAD is ahead of the upstream ref.
    (target / "local-only.txt").write_text("ahead of upstream\n")
    subprocess.run(["git", "add", "local-only.txt"], cwd=target, check=True)
    subprocess.run(
        ["git", "commit", "-q", "-m", "local-only commit"], cwd=target, check=True
    )
    # The local commit means the branch tip is no longer an ancestor
    # of origin/develop, so the existing merged-into-base check would
    # also skip it. Re-point origin/develop at the new HEAD so the
    # merged check PASSES — that way only the unpushed predicate can
    # be responsible for the skip we assert below.
    head = subprocess.run(
        ["git", "-C", str(target), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    subprocess.run(
        ["git", "-C", str(workdir), "update-ref", "refs/remotes/origin/develop", head],
        check=True,
    )
    # Act
    plan = plan_prune(workdir, pending_age_days=7)
    reasons = [s.reason for s in plan.skipped if Path(s.path).name == "agent-unpushed"]
    # Assert
    assert any("unpushed" in r for r in reasons)


@pytest.mark.skipif(
    not _LSOF_AVAILABLE,
    reason="lsof not available on this host; the FD-held predicate falls through",
)
def test_plan_worktrees_skips_when_process_holds_fd(tmp_path: Path):
    # Arrange — merged worktree with a Python-held file open under it.
    # The lsof capability gate lives on the decorator above so the body
    # keeps a single assertion (in-body ``pytest.skip(...)`` counts as
    # an assertion under STX-TQ007).
    workdir = tmp_path / "wd"
    workdir.mkdir()
    _init_git_repo(workdir)
    target = _make_merged_worktree(workdir, "agent-fd-held")
    held_file = target / "README.md"
    with held_file.open("rb") as _fp:  # keeps an fd open across plan_prune
        # Act
        plan = plan_prune(workdir, pending_age_days=7)
    candidate_names = {Path(e.path).name for e in plan.worktrees}
    # Assert
    assert "agent-fd-held" not in candidate_names


# ---------------------------------------------------------------------------
# plan_prune — `removeprefix` regression guard (2026-06-04)
# ---------------------------------------------------------------------------


def test_plan_picks_merged_worktree_whose_branch_starts_with_strip_char(
    tmp_path: Path,
):
    """Regression guard for the ``str.lstrip("refs/heads/")`` bug. The
    old code stripped any LEADING character that appeared in the set
    {r,e,f,s,/,h,a,d} — so a branch like ``sibling-feature`` lost its
    leading ``s``, became ``ibling-feature``, ``git merge-base
    --is-ancestor`` rejected the unknown ref, and the reaper silently
    skipped an already-merged worktree. We now use ``removeprefix``;
    this test pins that fix by creating a worktree on a branch named
    ``sibling-`` and asserting the planner picks it as merged."""
    # Arrange
    workdir = tmp_path / "wd"
    workdir.mkdir()
    _init_git_repo(workdir)
    # Branch name deliberately starts with 's' so the old lstrip bug
    # would have mangled it. The wrapper creates the worktree with
    # branch ``merged-agent-strip-victim`` — also leading-'m', also
    # OK; force the branch to start with 's' by renaming after.
    target = workdir / ".claude" / "worktrees" / "agent-strip-victim"
    target.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "worktree", "add", "-b", "strip-target", str(target)],
        cwd=workdir,
        check=True,
    )
    # Act
    plan = plan_prune(workdir, pending_age_days=7)
    paths = {Path(e.path).name for e in plan.worktrees}
    # Assert
    assert "agent-strip-victim" in paths


# ---------------------------------------------------------------------------
# apply_plan — moves to parked dir, no delete
# ---------------------------------------------------------------------------


def test_apply_moves_pending_records_into_parked_dir(tmp_path: Path):
    # Arrange
    workdir = tmp_path / "wd"
    workdir.mkdir()
    _populate_pending(workdir, count=2, age_days=10)
    plan = plan_prune(workdir, pending_age_days=7)
    # Act
    result = apply_plan(plan)
    # Assert
    assert result["moved"]["pending"] == 2


def test_apply_preserves_parked_data_not_delete(tmp_path: Path):
    # Arrange
    workdir = tmp_path / "wd"
    workdir.mkdir()
    _populate_pending(workdir, count=1, age_days=10)
    plan = plan_prune(workdir, pending_age_days=7)
    # Act
    result = apply_plan(plan)
    parked_dir = Path(result["parked_paths"][0])
    parked_files = list(parked_dir.iterdir())
    # Assert
    assert len(parked_files) == 1


def test_apply_returns_zero_moves_for_empty_plan(tmp_path: Path):
    # Arrange — no pending, no worktrees.
    workdir = tmp_path / "wd"
    workdir.mkdir()
    plan = plan_prune(workdir, pending_age_days=7)
    # Act
    result = apply_plan(plan)
    # Assert
    assert result["moved"] == {"pending": 0, "worktrees": 0}


# ---------------------------------------------------------------------------
# CLI command — dry-run, JSON, --apply
# ---------------------------------------------------------------------------


@pytest.fixture
def _registered(tmp_path: Path, env_save_restore):
    """Register a real agent pointing at a tmp workdir."""
    import yaml

    home = tmp_path / "home"
    home.mkdir()
    env_save_restore.set("HOME", str(home))

    workdir = tmp_path / "wd"
    workdir.mkdir()

    name = "prune-target"
    agents_dir = home / ".scitex" / "agent-container" / "agents" / name
    agents_dir.mkdir(parents=True)
    spec_path = agents_dir / "spec.yaml"
    spec_path.write_text(
        yaml.safe_dump(
            {
                "apiVersion": "scitex-agent-container/v3",
                "kind": "Agent",
                "spec": explicit_spec({
                    "runtime": "apptainer",
                    "host": "${HOSTNAME}",
                    "workdir": str(workdir),
                    "apptainer": {"image": "/x.sif", "binds": []},
                    "claude": {"model": "sonnet"},
                    "health": {"enabled": True, "interval": 60},
                    "restart": {"policy": "on-failure", "max_retries": 3},
                }),
            }
        )
    )

    from scitex_agent_container._state.registry import Registry

    Registry().add(name=name, config_path=str(spec_path), screen_name=name)
    return name, workdir


def test_cli_dry_run_does_not_move_files(tmp_path: Path, _registered):
    # Arrange
    name, workdir = _registered
    _populate_pending(workdir, count=2, age_days=10)
    runner = CliRunner()
    # Act
    runner.invoke(prune_claude, [name])
    # Assert
    survivors = list(
        (workdir / ".claude" / "hooks" / "pre-tool-use" / ".pending").iterdir()
    )
    assert len(survivors) == 2


def test_cli_apply_moves_files(tmp_path: Path, _registered):
    # Arrange
    name, workdir = _registered
    _populate_pending(workdir, count=2, age_days=10)
    runner = CliRunner()
    # Act
    runner.invoke(prune_claude, [name, "--apply"])
    # Assert
    survivors = list(
        (workdir / ".claude" / "hooks" / "pre-tool-use" / ".pending").iterdir()
    )
    assert survivors == []


def test_cli_json_output_is_valid_json(tmp_path: Path, _registered):
    # Arrange
    name, workdir = _registered
    _populate_pending(workdir, count=1, age_days=10)
    runner = CliRunner()
    # Act
    result = runner.invoke(prune_claude, [name, "--json"])
    parsed = json.loads(result.stdout)
    # Assert
    assert "pending" in parsed


def test_cli_unknown_agent_exits_nonzero(_registered):
    # Arrange — unknown name.
    runner = CliRunner()
    # Act
    result = runner.invoke(prune_claude, ["nope-not-registered"])
    # Assert
    assert result.exit_code == 2


def test_cli_empty_plan_emits_no_candidates_message(tmp_path: Path, _registered):
    # Arrange — workdir has no .claude/ at all.
    name, _ = _registered
    runner = CliRunner()
    # Act
    result = runner.invoke(prune_claude, [name])
    # Assert
    assert "No prune candidates found" in result.output
