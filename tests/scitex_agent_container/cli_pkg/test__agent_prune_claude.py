"""Tests for ``sac agents prune-claude`` (F-CS8 maintenance, 2026-06-03).

Real I/O against a tmp_path filesystem. The git-aware worktrees path uses
real ``git`` invocations against tmp repos initialised inline — no mocks.
Pending-record path uses real file mtime manipulation. AAA markers + one
assert per test.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg._agent_prune_claude import (
    PrunePlan,
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
                "spec": {"runtime": "apptainer", "workdir": str(workdir)},
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
    parsed = json.loads(result.output)
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
