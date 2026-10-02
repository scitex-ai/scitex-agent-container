"""Real-git tests for the managed-home authority kind (no origin).

A git-managed ``~/.scitex`` home repository is self-contained local state:
no ``origin`` remote, clean tree, spec blob tracked and equal to HEAD.
These tests build such repos with plain git only — no scitex-dev import —
so they prove the GATE's contract independent of the helper that adopts
homes in production (``scitex_dev.home``, tested in its own package).
"""

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
def managed_home(tmp_path: Path) -> tuple[Path, Path]:
    """Return a managed-home-shaped repo and its committed spec.

    ``agents/demo/spec.yaml`` tracked and committed; ``session.jsonl``
    runtime sibling present but gitignored (placement contract); no
    origin remote; clean tree.
    """
    root = tmp_path / ".scitex"
    agent = root / "agent-container" / "agents" / "demo"
    agent.mkdir(parents=True)
    spec = agent / "spec.yaml"
    spec.write_text("apiVersion: scitex-agent-container/v3\n", encoding="utf-8")
    (agent / "session.jsonl").write_text("{}\n", encoding="utf-8")
    (root / ".gitignore").write_text("session.jsonl\n", encoding="utf-8")
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "home@example.com")
    _git(root, "config", "user.name", "Home Test")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "manage .scitex")
    return root, spec


def test_managed_home_accepts_clean_no_origin(managed_home):
    # Arrange — fixture is a clean, origin-less, committed home.
    _, spec = managed_home
    # Act
    auth = validate_spec_authority(spec)
    # Assert — third kind, local identity, digest equals the HEAD blob.
    assert auth.kind == "managed-home"
    assert auth.source_identity == "local-dotscitex"
    assert len(auth.head) == 40
    assert len(auth.spec_digest) == 40
    assert auth.spec_digest != auth.head  # blob sha, not the commit sha


def test_managed_home_digest_matches_head_blob(managed_home):
    # Arrange
    root, spec = managed_home
    # Act
    auth = validate_spec_authority(spec)
    # Assert — digest is the committed blob, not the worktree hash alone.
    committed = _git(root, "rev-parse", f"HEAD:{spec.relative_to(root).as_posix()}")
    assert auth.spec_digest == committed


def test_managed_home_refuses_dirty_spec(managed_home):
    # Arrange — operator edits the spec after the commit.
    _, spec = managed_home
    spec.write_text("apiVersion: scitex-agent-container/v3\ndrift: true\n", encoding="utf-8")
    # Act / Assert
    with pytest.raises(SpecAuthorityError, match="dirty"):
        validate_spec_authority(spec)


def test_managed_home_refuses_uncommitted_home(tmp_path):
    # Arrange — a home directory that was never adopted (no repo at all).
    spec = tmp_path / ".scitex" / "agent-container" / "agents" / "demo" / "spec.yaml"
    spec.parent.mkdir(parents=True)
    spec.write_text("apiVersion: scitex-agent-container/v3\n", encoding="utf-8")
    # Act / Assert — no repo means no proof, fail closed.
    with pytest.raises(SpecAuthorityError):
        validate_spec_authority(spec)


def test_managed_home_refuses_repo_without_commits(tmp_path):
    # Arrange — git init ran but nothing was ever committed (no HEAD).
    root = tmp_path / ".scitex"
    spec = root / "agent-container" / "agents" / "demo" / "spec.yaml"
    spec.parent.mkdir(parents=True)
    spec.write_text("apiVersion: scitex-agent-container/v3\n", encoding="utf-8")
    _git(root, "init", "-q")
    # Act / Assert
    with pytest.raises(SpecAuthorityError):
        validate_spec_authority(spec)


def test_origin_repo_never_rides_managed_home(managed_home):
    # Arrange — someone adds an origin to the managed home afterwards.
    root, spec = managed_home
    _git(root, "remote", "add", "origin", "https://example.com/foreign/home.git")
    # Act / Assert — the local trust path closes; live rules take over
    # and refuse (no develop, no upstream).
    with pytest.raises(SpecAuthorityError):
        validate_spec_authority(spec)


def test_untracked_spec_file_refuses(managed_home):
    # Arrange — a second spec exists but was never added (tree dirty).
    root, spec = managed_home
    other = root / "agent-container" / "agents" / "rogue" / "spec.yaml"
    other.parent.mkdir(parents=True)
    other.write_text("apiVersion: scitex-agent-container/v3\n", encoding="utf-8")
    # Act / Assert — the KNOWN spec also refuses: the repo is not clean.
    with pytest.raises(SpecAuthorityError, match="dirty"):
        validate_spec_authority(spec)


def test_runtime_sibling_does_not_dirty_the_home(managed_home):
    # Arrange — a new runtime file lands next to the spec mid-operation...
    # but the contract ignores it, so the tree stays clean.
    root, spec = managed_home
    (root / ".gitignore").write_text("session.jsonl\n*.log\n", encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "ignore logs")
    (spec.parent / "boot.stdout.log").write_text("x\n", encoding="utf-8")
    # Act
    auth = validate_spec_authority(spec)
    # Assert
    assert auth.kind == "managed-home"


def test_home_for_spec_finds_containing_home(tmp_path):
    # Arrange — a spec nested inside a .scitex tree.
    from scitex_agent_container._drift._managed_home import home_for_spec

    spec = tmp_path / "datahome" / ".scitex" / "agent-container" / "agents" / "x" / "spec.yaml"
    spec.parent.mkdir(parents=True)
    spec.write_text("v: 1\n", encoding="utf-8")
    # Act
    found = home_for_spec(spec)
    # Assert — the DATA home, regardless of the process home.
    assert found == tmp_path / "datahome"


def test_home_for_spec_returns_none_outside_any_tree(tmp_path):
    # Arrange — a spec with no .scitex ancestor at all.
    from scitex_agent_container._drift._managed_home import home_for_spec

    spec = tmp_path / "custom" / "my-agent" / "spec.yaml"
    spec.parent.mkdir(parents=True)
    spec.write_text("v: 1\n", encoding="utf-8")
    # Act / Assert
    assert home_for_spec(spec) is None


def test_home_for_spec_ignores_bare_dotscitex_name_on_file(tmp_path):
    # Arrange — a FILE named .scitex is not a tree (is_dir guard).
    from scitex_agent_container._drift._managed_home import home_for_spec

    fake = tmp_path / "datahome" / ".scitex"
    fake.parent.mkdir(parents=True)
    fake.write_text("not a dir\n", encoding="utf-8")
    spec = tmp_path / "datahome" / "agents" / "x" / "spec.yaml"
    spec.parent.mkdir(parents=True)
    spec.write_text("v: 1\n", encoding="utf-8")
    # Act / Assert
    assert home_for_spec(spec) is None
