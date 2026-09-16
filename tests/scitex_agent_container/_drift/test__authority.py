"""Real-git tests for the fail-closed lifecycle authority boundary."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scitex_agent_container._drift import (
    SpecAuthorityError,
    validate_spec_authority,
)


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
    )
    return proc.stdout.strip()


@pytest.fixture
def authority_repo(tmp_path: Path) -> tuple[Path, Path, Path]:
    """Return a clean develop main checkout, its spec, and bare remote."""
    remote = tmp_path / "dotfiles.git"
    subprocess.run(
        ["git", "init", "--bare", str(remote)], check=True, capture_output=True
    )
    repo = tmp_path / "dotfiles"
    subprocess.run(
        ["git", "clone", str(remote), str(repo)], check=True, capture_output=True
    )
    _git(repo, "config", "user.email", "authority@example.com")
    _git(repo, "config", "user.name", "Authority Test")
    _git(repo, "checkout", "-b", "develop")
    spec = (
        repo / "src" / ".scitex" / "agent-container" / "agents" / "alpha" / "spec.yaml"
    )
    spec.parent.mkdir(parents=True)
    spec.write_text("apiVersion: scitex-agent-container/v3\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "authority")
    _git(repo, "push", "-u", "origin", "develop")
    return repo, spec, remote


def _advance_remote(remote: Path, tmp_path: Path) -> None:
    other = tmp_path / "remote-writer"
    subprocess.run(
        ["git", "clone", str(remote), str(other)], check=True, capture_output=True
    )
    _git(other, "config", "user.email", "writer@example.com")
    _git(other, "config", "user.name", "Remote Writer")
    _git(other, "checkout", "develop")
    (other / "remote.txt").write_text("new\n", encoding="utf-8")
    _git(other, "add", "-A")
    _git(other, "commit", "-m", "advance")
    _git(other, "push")


def test_clean_develop_main_is_accepted(authority_repo):
    # Arrange
    _repo, spec, _remote = authority_repo
    # Act
    authority = validate_spec_authority(spec)
    # Assert
    assert authority.kind == "live-develop"


def test_non_git_source_is_refused(tmp_path: Path):
    # Arrange
    spec = tmp_path / "spec.yaml"
    spec.write_text("x\n", encoding="utf-8")
    # Act
    ctx = pytest.raises(SpecAuthorityError)
    # Assert
    with ctx:
        validate_spec_authority(spec)


def test_dirty_authority_source_is_refused(authority_repo):
    # Arrange
    repo, spec, _remote = authority_repo
    (repo / "dirty.txt").write_text("uncommitted\n", encoding="utf-8")
    # Act
    ctx = pytest.raises(SpecAuthorityError, match="dirty")
    # Assert
    with ctx:
        validate_spec_authority(spec)


def test_wrong_main_branch_is_refused(authority_repo):
    # Arrange
    repo, spec, _remote = authority_repo
    _git(repo, "checkout", "-b", "feature/unsafe-authority")
    # Act
    ctx = pytest.raises(SpecAuthorityError, match="must be on 'develop'")
    # Assert
    with ctx:
        validate_spec_authority(spec)


def test_linked_feature_worktree_is_refused(authority_repo, tmp_path: Path):
    # Arrange
    repo, _spec, _remote = authority_repo
    linked = tmp_path / "feature-worktree"
    _git(repo, "worktree", "add", "-b", "feature/work", str(linked), "develop")
    spec = (
        linked
        / "src"
        / ".scitex"
        / "agent-container"
        / "agents"
        / "alpha"
        / "spec.yaml"
    )
    # Act
    ctx = pytest.raises(SpecAuthorityError, match="cannot become live spec authority")
    # Assert
    with ctx:
        validate_spec_authority(spec)


def test_ahead_authority_source_is_refused(authority_repo):
    # Arrange
    repo, spec, _remote = authority_repo
    (repo / "ahead.txt").write_text("local\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "ahead")
    # Act
    ctx = pytest.raises(SpecAuthorityError, match="ahead")
    # Assert
    with ctx:
        validate_spec_authority(spec)


def test_behind_authority_source_is_refused(authority_repo, tmp_path: Path):
    # Arrange
    _repo, spec, remote = authority_repo
    _advance_remote(remote, tmp_path)
    # Act
    ctx = pytest.raises(SpecAuthorityError, match="behind")
    # Assert
    with ctx:
        validate_spec_authority(spec)


def test_diverged_authority_source_is_refused(authority_repo, tmp_path: Path):
    # Arrange
    repo, spec, remote = authority_repo
    _advance_remote(remote, tmp_path)
    (repo / "local.txt").write_text("local\n", encoding="utf-8")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "local divergence")
    # Act
    ctx = pytest.raises(SpecAuthorityError, match="diverged")
    # Assert
    with ctx:
        validate_spec_authority(spec)


def test_unknown_upstream_is_refused(authority_repo):
    # Arrange
    repo, spec, _remote = authority_repo
    _git(repo, "branch", "--unset-upstream")
    # Act
    ctx = pytest.raises(SpecAuthorityError, match="unreachable")
    # Assert
    with ctx:
        validate_spec_authority(spec)


def test_unreachable_authority_remote_is_refused(authority_repo):
    # Arrange
    repo, spec, remote = authority_repo
    _git(repo, "remote", "set-url", "origin", str(remote.with_name("absent.git")))
    # Act
    ctx = pytest.raises(SpecAuthorityError, match="unreachable")
    # Assert
    with ctx:
        validate_spec_authority(spec)


def test_exact_detached_snapshot_is_accepted(authority_repo, tmp_path: Path):
    # Arrange
    repo, _spec, remote = authority_repo
    head = _git(repo, "rev-parse", "HEAD")
    snapshot = tmp_path / "sac-authority" / f"dotfiles-{head}"
    snapshot.parent.mkdir()
    subprocess.run(
        ["git", "clone", str(remote), str(snapshot)], check=True, capture_output=True
    )
    _git(snapshot, "checkout", "--detach", head)
    spec = (
        snapshot
        / "src"
        / ".scitex"
        / "agent-container"
        / "agents"
        / "alpha"
        / "spec.yaml"
    )
    # Act
    authority = validate_spec_authority(spec)
    # Assert
    assert (authority.kind, authority.head, authority.source_identity) == (
        "immutable-snapshot",
        head,
        "dotfiles",
    )


def test_misnamed_detached_snapshot_is_refused(authority_repo, tmp_path: Path):
    # Arrange
    repo, _spec, remote = authority_repo
    head = _git(repo, "rev-parse", "HEAD")
    wrong = "0" * 40
    snapshot = tmp_path / "sac-authority" / f"dotfiles-{wrong}"
    snapshot.parent.mkdir()
    subprocess.run(
        ["git", "clone", str(remote), str(snapshot)], check=True, capture_output=True
    )
    _git(snapshot, "checkout", "--detach", head)
    spec = (
        snapshot
        / "src"
        / ".scitex"
        / "agent-container"
        / "agents"
        / "alpha"
        / "spec.yaml"
    )
    # Act
    ctx = pytest.raises(SpecAuthorityError, match="identity mismatch")
    # Assert
    with ctx:
        validate_spec_authority(spec)


def test_wrong_snapshot_source_identity_is_refused(authority_repo, tmp_path: Path):
    # Arrange
    repo, _spec, remote = authority_repo
    head = _git(repo, "rev-parse", "HEAD")
    snapshot = tmp_path / "sac-authority" / f"other-source-{head}"
    snapshot.parent.mkdir()
    subprocess.run(
        ["git", "clone", str(remote), str(snapshot)], check=True, capture_output=True
    )
    _git(snapshot, "checkout", "--detach", head)
    spec = (
        snapshot
        / "src"
        / ".scitex"
        / "agent-container"
        / "agents"
        / "alpha"
        / "spec.yaml"
    )
    # Act
    ctx = pytest.raises(SpecAuthorityError, match="source identity mismatch")
    # Assert
    with ctx:
        validate_spec_authority(spec)


def test_snapshot_spec_digest_mismatch_is_refused(authority_repo, tmp_path: Path):
    # Arrange — hide the edit from status to prove the blob check is independent.
    repo, _spec, remote = authority_repo
    head = _git(repo, "rev-parse", "HEAD")
    snapshot = tmp_path / "sac-authority" / f"dotfiles-{head}"
    snapshot.parent.mkdir()
    subprocess.run(
        ["git", "clone", str(remote), str(snapshot)], check=True, capture_output=True
    )
    _git(snapshot, "checkout", "--detach", head)
    spec = (
        snapshot
        / "src"
        / ".scitex"
        / "agent-container"
        / "agents"
        / "alpha"
        / "spec.yaml"
    )
    rel = spec.relative_to(snapshot).as_posix()
    _git(snapshot, "update-index", "--assume-unchanged", rel)
    spec.write_text("apiVersion: tampered/v1\n", encoding="utf-8")
    # Act
    ctx = pytest.raises(SpecAuthorityError, match="digest does not match")
    # Assert
    with ctx:
        validate_spec_authority(spec)
