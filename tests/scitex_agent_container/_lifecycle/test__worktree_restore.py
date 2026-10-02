"""Restore mechanics against the real neutral CLI and real Git fixtures.

Set SCITEX_WORKTREE_POLICY_TEST_CLI to a reviewed neutral-policy checkout
for the cross-repository contract suite. Policy owns approval; SAC owns apply.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest

from scitex_agent_container._lifecycle import _worktree_restore as implementation
from scitex_agent_container._lifecycle._worktree_policy import WorktreePolicyError
from scitex_agent_container._lifecycle._worktree_restore import (
    restore_owned_task_worktree,
)
from scitex_agent_container.config import AgentConfig


def git(repo, *args):
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def owned(tmp_path, monkeypatch):
    cli = Path(
        os.environ.get("SCITEX_WORKTREE_POLICY_TEST_CLI")
        or Path(__file__).parent
        / "_fixtures/worktree-policy/src/.bin/scitex-worktree-policy"
    )
    cli.chmod(cli.stat().st_mode | 0o111)
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q", "-b", "fix/retained-primary")
    (repo / "tracked.txt").write_text("primary\n")
    git(repo, "add", "tracked.txt")
    git(
        repo,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "-c",
        "commit.gpgsign=false",
        "commit",
        "-qm",
        "primary",
    )
    cfg = AgentConfig(name="scitex-scholar", workdir=str(repo))
    slug = cfg.name + "-" + hashlib.sha256(cfg.name.encode()).hexdigest()[:8]
    branch = "feature/sac-" + slug
    primary = git(repo, "rev-parse", "HEAD")
    retained = git(
        repo,
        "-c",
        "user.name=Test",
        "-c",
        "user.email=test@example.com",
        "commit-tree",
        git(repo, "rev-parse", "HEAD^{tree}"),
        "-p",
        primary,
        "-m",
        "retained work history",
    )
    git(repo, "branch", branch, retained)
    target = repo / ".worktrees" / ("sac-" + slug)
    runtime = tmp_path / "runtime"
    monkeypatch.setenv("SCITEX_AGENT_CONTAINER_RUNTIME_DIR", str(runtime))
    owner = runtime / cfg.name / "worktree-owner.json"
    owner.parent.mkdir(parents=True)
    owner.write_text(
        json.dumps(
            dict(
                agent=cfg.name,
                repo_root=str(repo),
                worktree=str(target),
                branch=branch,
                incarnation="retained-instance",
            )
        )
        + "\n"
    )
    return dict(
        config=cfg,
        cli=cli,
        repo=repo,
        owner=owner,
        target=target,
        branch=branch,
        tip=git(repo, "rev-parse", branch),
    )


def restore(owned, **kwargs):
    return restore_owned_task_worktree(
        owned["config"], expected_tip=owned["tip"], cli_path=owned["cli"], **kwargs
    )


def test_restore_default_dry_run_and_reviewed_apply_preserve_identity(owned):
    original_owner = owned["owner"].read_bytes()
    primary_head = git(owned["repo"], "rev-parse", "HEAD")
    retained_history = git(owned["repo"], "rev-list", owned["branch"])
    receipt = restore(owned)
    assert receipt["mode"] == "dry-run"
    assert not owned["target"].exists()
    applied = restore(owned, apply=True, receipt_sha256=receipt["receipt_sha256"])
    assert applied["mode"] == "apply"
    assert applied["verified"] is True
    assert git(owned["target"], "rev-parse", "HEAD") == owned["tip"]
    assert git(owned["target"], "branch", "--show-current") == owned["branch"]
    assert git(owned["repo"], "rev-parse", "HEAD") == primary_head
    assert git(owned["repo"], "rev-list", owned["branch"]) == retained_history
    assert git(owned["repo"], "branch", "--show-current") == "fix/retained-primary"
    assert owned["owner"].read_bytes() == original_owner
    assert owned["config"].workdir == str(owned["repo"])


def test_apply_requires_exact_reviewed_receipt(owned):
    with pytest.raises(WorktreePolicyError, match="receipt"):
        restore(owned, apply=True)
    with pytest.raises(WorktreePolicyError, match="receipt"):
        restore(owned, apply=True, receipt_sha256="0" * 64)
    assert not owned["target"].exists()


def test_configured_recorded_missing_checkout_can_be_restored(owned):
    owned["config"].workdir = str(owned["target"])
    receipt = restore(owned)
    assert receipt["receipt"]["repo_root"] == str(owned["repo"])
    restore(owned, apply=True, receipt_sha256=receipt["receipt_sha256"])
    assert owned["config"].workdir == str(owned["target"])


def test_wrong_configured_repository_cannot_use_owner_record(owned, tmp_path):
    owned["config"].workdir = str(tmp_path / "another-repository")
    with pytest.raises(WorktreePolicyError, match="configured workdir"):
        restore(owned)
    assert not owned["target"].exists()


@pytest.mark.parametrize("change", ["owner", "primary", "tip", "target"])
def test_stale_reviewed_receipt_cannot_apply(owned, change):
    receipt = restore(owned)
    if change == "owner":
        owned["owner"].write_bytes(owned["owner"].read_bytes() + b" ")
    elif change == "primary":
        (owned["repo"] / "tracked.txt").write_text("changed\n")
    elif change == "tip":
        tree = git(owned["repo"], "rev-parse", "HEAD^{tree}")
        proc = subprocess.run(
            [
                "git",
                "-C",
                str(owned["repo"]),
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit-tree",
                tree,
                "-p",
                owned["tip"],
                "-m",
                "branch advanced",
            ],
            check=True,
            capture_output=True,
            text=True,
        )
        git(
            owned["repo"],
            "update-ref",
            "refs/heads/" + owned["branch"],
            proc.stdout.strip(),
        )
    else:
        owned["target"].mkdir(parents=True)
    with pytest.raises(WorktreePolicyError):
        restore(owned, apply=True, receipt_sha256=receipt["receipt_sha256"])
    assert not (owned["target"] / ".git").exists()


def test_state_race_between_approval_and_add_is_refused(owned, monkeypatch):
    receipt = restore(owned)
    original = implementation._approval
    calls = 0

    def change_after_approval(*args):
        nonlocal calls
        result = original(*args)
        calls += 1
        if calls == 1:
            owned["owner"].write_bytes(owned["owner"].read_bytes() + b" ")
        return result

    monkeypatch.setattr(implementation, "_approval", change_after_approval)
    with pytest.raises(WorktreePolicyError, match="raced"):
        restore(owned, apply=True, receipt_sha256=receipt["receipt_sha256"])
    assert not owned["target"].exists()


def test_postverification_failure_preserves_evidence(owned, monkeypatch):
    receipt = restore(owned)
    original = implementation._verify

    def damage_after_add(*args):
        (owned["target"] / "tracked.txt").write_text("changed after add\n")
        return original(*args)

    monkeypatch.setattr(implementation, "_verify", damage_after_add)
    with pytest.raises(WorktreePolicyError, match="postverification"):
        restore(owned, apply=True, receipt_sha256=receipt["receipt_sha256"])
    assert (owned["target"] / ".git").exists()
    assert git(owned["target"], "rev-parse", "HEAD") == owned["tip"]
    assert git(owned["repo"], "branch", "--show-current") == "fix/retained-primary"
