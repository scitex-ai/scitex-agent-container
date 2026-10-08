"""Default-path tests for the in-process worktree policy engine.

No external policy executable is involved anywhere here: every test calls
the lifecycle with no ``cli_path`` (or the engine directly) against real
temporary Git repositories. A regression that reintroduces the
``~/.dotfiles`` CLI dependency fails these tests on any host without it.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Iterator

import pytest

from scitex_agent_container._lifecycle import _worktree_policy_engine as engine
from scitex_agent_container._lifecycle._worktree_policy import (
    WorktreePolicyError,
    enforce_task_worktree_policy,
)
from scitex_agent_container._lifecycle._worktree_restore import (
    restore_owned_task_worktree,
)
from scitex_agent_container.config import AgentConfig


@pytest.fixture
def runtime_dir(tmp_path: Path) -> Iterator[Path]:
    """Isolated real runtime directory with environment restoration."""
    key = "SCITEX_AGENT_CONTAINER_RUNTIME_DIR"
    previous = os.environ.get(key)
    value = tmp_path / "runtime"
    os.environ[key] = str(value)
    try:
        yield value
    finally:
        if previous is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = previous


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _authority(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-q", "-b", "develop")
    (path / "tracked.txt").write_text("base\n", encoding="utf-8")
    _git(path, "add", "tracked.txt")
    _git(
        path,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-qm",
        "base",
    )
    return path


def _config(workdir: Path, *, name: str = "engine-test") -> AgentConfig:
    return AgentConfig(name=name, kind="Agent", workdir=str(workdir))


def _captured_error(call) -> str:
    try:
        call()
    except WorktreePolicyError as exc:
        return str(exc)
    return ""


def test_bundled_projections_match_bundled_manifest() -> None:
    record = engine.check_projections()
    assert record["decision"] == "current"


def test_projection_hash_covers_all_harness_files() -> None:
    assert engine.projection_hash() == engine.check_projections()["projection_sha256"]


def test_default_enforce_provisions_owned_worktree(tmp_path, runtime_dir) -> None:
    repo = _authority(tmp_path / "repo")
    proof = enforce_task_worktree_policy(_config(repo))
    assert proof is not None
    assert proof.worktree_action == "create"


def test_default_enforce_resolves_deterministic_branch(tmp_path, runtime_dir) -> None:
    repo = _authority(tmp_path / "repo")
    proof = enforce_task_worktree_policy(_config(repo))
    assert proof is not None
    assert proof.branch == "feature/sac-engine-test-bbf48bdd"


def test_default_enforce_points_workdir_into_worktrees(tmp_path, runtime_dir) -> None:
    repo = _authority(tmp_path / "repo")
    config = _config(repo)
    enforce_task_worktree_policy(config)
    assert Path(config.workdir).parent == repo / ".worktrees"


def test_default_enforce_proof_carries_manifest_identity(tmp_path, runtime_dir) -> None:
    repo = _authority(tmp_path / "repo")
    proof = enforce_task_worktree_policy(_config(repo))
    assert proof is not None
    assert (proof.policy_sha256, proof.projection_sha256) == (
        engine.check_projections()["policy_sha256"],
        engine.check_projections()["projection_sha256"],
    )


def test_default_enforce_refuses_dirty_authority(tmp_path, runtime_dir) -> None:
    repo = _authority(tmp_path / "repo")
    (repo / "tracked.txt").write_text("dirty\n", encoding="utf-8")
    message = _captured_error(lambda: enforce_task_worktree_policy(_config(repo)))
    assert "dirty" in message


def test_engine_denies_edit_intent_on_authority(tmp_path: Path) -> None:
    repo = _authority(tmp_path / "repo")
    message = _captured_error(lambda: engine.assert_context(repo, "edit"))
    assert "read-only" in message


def test_engine_allows_read_intent_on_authority(tmp_path: Path) -> None:
    repo = _authority(tmp_path / "repo")
    assert engine.assert_context(repo, "read")["decision"] == "allow"


def test_engine_authorizes_planned_worktree_add(tmp_path: Path) -> None:
    repo = _authority(tmp_path / "repo")
    command = "git worktree add -b feature/thing .worktrees/sac-thing feature/thing"
    assert engine.check_shell(command, repo)["decision"] == "allow"


def test_engine_denies_commit_on_authority(tmp_path: Path) -> None:
    repo = _authority(tmp_path / "repo")
    message = _captured_error(lambda: engine.check_shell("git commit -m x", repo))
    assert "read-only" in message


def test_default_restore_dry_run_approves_exact_checkout(tmp_path, runtime_dir) -> None:
    repo = _authority(tmp_path / "repo")
    config = _config(repo)
    proof = enforce_task_worktree_policy(config)
    assert proof is not None
    tip = _git(repo, "rev-parse", proof.branch)
    _git(repo, "worktree", "remove", "--force", proof.resolved_workdir)
    receipt = restore_owned_task_worktree(config, expected_tip=tip)["receipt"]
    assert receipt["operation"] == "restore-owned-worktree"


def test_default_restore_apply_recreates_checkout(tmp_path, runtime_dir) -> None:
    repo = _authority(tmp_path / "repo")
    config = _config(repo)
    proof = enforce_task_worktree_policy(config)
    assert proof is not None
    tip = _git(repo, "rev-parse", proof.branch)
    _git(repo, "worktree", "remove", "--force", proof.resolved_workdir)
    first = restore_owned_task_worktree(config, expected_tip=tip)
    applied = restore_owned_task_worktree(
        config, expected_tip=tip, apply=True, receipt_sha256=first["receipt_sha256"]
    )
    assert applied["verified"] is True


def test_default_enforce_resolves_policy_id(tmp_path, runtime_dir) -> None:
    repo = _authority(tmp_path / "repo")
    proof = enforce_task_worktree_policy(_config(repo))
    assert proof is not None
    assert proof.policy_id == "scitex.worktree.v1"
