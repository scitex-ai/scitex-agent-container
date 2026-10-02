"""Restore mechanics against the real neutral CLI and real Git fixtures.

Set SCITEX_WORKTREE_POLICY_TEST_CLI to a reviewed neutral-policy checkout
for the cross-repository contract suite. Policy owns approval; SAC owns apply.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

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
def owned(tmp_path):
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
    saved_runtime = os.environ.get("SCITEX_AGENT_CONTAINER_RUNTIME_DIR")
    os.environ["SCITEX_AGENT_CONTAINER_RUNTIME_DIR"] = str(runtime)
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
    fixture = dict(
        config=cfg,
        cli=cli,
        repo=repo,
        owner=owner,
        target=target,
        branch=branch,
        tip=git(repo, "rev-parse", branch),
    )
    try:
        yield fixture
    finally:
        if saved_runtime is None:
            os.environ.pop("SCITEX_AGENT_CONTAINER_RUNTIME_DIR", None)
        else:
            os.environ["SCITEX_AGENT_CONTAINER_RUNTIME_DIR"] = saved_runtime


def restore(owned, **kwargs):
    return restore_owned_task_worktree(
        owned["config"], expected_tip=owned["tip"], cli_path=owned["cli"], **kwargs
    )


@pytest.fixture
def applied(owned):
    # Arrange
    original_owner = owned["owner"].read_bytes()
    primary_head = git(owned["repo"], "rev-parse", "HEAD")
    retained_history = git(owned["repo"], "rev-list", owned["branch"])
    # Act
    receipt = restore(owned)
    dry_run = (receipt["mode"], owned["target"].exists())
    applied = restore(owned, apply=True, receipt_sha256=receipt["receipt_sha256"])
    return {
        "dry_run": dry_run,
        "result": applied,
        "owner": original_owner,
        "primary_head": primary_head,
        "history": retained_history,
    }


@pytest.mark.parametrize("key,expected", [("mode", "apply"), ("verified", True)])
def test_reviewed_apply_reports_verified_operation(applied, key, expected):
    # Arrange
    receipt = applied["result"]
    # Act
    value = receipt[key]
    # Assert
    assert value == expected


def test_default_dry_run_does_not_materialize_a_worktree(applied):
    # Arrange
    observation = applied["dry_run"]
    # Act
    mode, existed = observation
    # Assert
    assert (mode, existed) == ("dry-run", False)


@pytest.mark.parametrize(
    "args,expected_key",
    [(("rev-parse", "HEAD"), "tip"), (("branch", "--show-current"), "branch")],
)
def test_restored_checkout_preserves_exact_retained_identity(
    owned, applied, args, expected_key
):
    # Arrange
    target = owned["target"]
    # Act
    actual = git(target, *args)
    # Assert
    assert actual == owned[expected_key]


def test_restore_preserves_primary_head(owned, applied):
    # Arrange
    primary = owned["repo"]
    # Act
    actual = git(primary, "rev-parse", "HEAD")
    # Assert
    assert actual == applied["primary_head"]


def test_restore_preserves_retained_branch_history(owned, applied):
    # Arrange
    primary = owned["repo"]
    # Act
    actual = git(primary, "rev-list", owned["branch"])
    # Assert
    assert actual == applied["history"]


def test_restore_preserves_primary_branch(owned, applied):
    # Arrange
    primary = owned["repo"]
    # Act
    actual = git(primary, "branch", "--show-current")
    # Assert
    assert actual == "fix/retained-primary"


def test_restore_does_not_rewrite_owner_record(owned, applied):
    # Arrange
    path = owned["owner"]
    # Act
    actual = path.read_bytes()
    # Assert
    assert actual == applied["owner"]


def test_restore_does_not_rewrite_configured_workdir(owned, applied):
    # Arrange
    config = owned["config"]
    # Act
    actual = config.workdir
    # Assert
    assert actual == str(owned["repo"])


@pytest.mark.parametrize("digest", [None, "0" * 64])
def test_apply_requires_exact_reviewed_receipt(owned, digest):
    # Arrange
    # Act
    # Assert
    with pytest.raises(WorktreePolicyError, match="receipt"):
        restore(owned, apply=True, receipt_sha256=digest)


def test_configured_recorded_missing_checkout_can_be_restored(owned):
    # Arrange
    owned["config"].workdir = str(owned["target"])
    # Act
    receipt = restore(owned)
    # Assert
    restore(owned, apply=True, receipt_sha256=receipt["receipt_sha256"])
    # Assert
    assert owned["config"].workdir == str(owned["target"])


def test_wrong_configured_repository_cannot_use_owner_record(owned, tmp_path):
    # Arrange
    # Act
    owned["config"].workdir = str(tmp_path / "another-repository")
    # Assert
    with pytest.raises(WorktreePolicyError, match="configured workdir"):
        restore(owned)


def _advance_retained_tip(owned):
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


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(
            lambda owned: owned["owner"].write_bytes(
                owned["owner"].read_bytes() + b" "
            ),
            id="owner",
        ),
        pytest.param(
            lambda owned: (owned["repo"] / "tracked.txt").write_text("changed\n"),
            id="primary",
        ),
        pytest.param(_advance_retained_tip, id="tip"),
        pytest.param(lambda owned: owned["target"].mkdir(parents=True), id="target"),
    ],
)
def test_stale_reviewed_receipt_cannot_apply(owned, mutate):
    # Arrange
    receipt = restore(owned)
    mutate(owned)
    # Act
    # Assert
    with pytest.raises(WorktreePolicyError):
        restore(owned, apply=True, receipt_sha256=receipt["receipt_sha256"])


def racing_policy(owned, phase):
    """Run the real policy process, with an external filesystem race at a boundary."""
    wrapper = owned["repo"].parent / "racing-policy.py"
    count = wrapper.with_suffix(".count")
    wrapper.write_text(
        f"#!{sys.executable}\n"
        "import subprocess, sys\nfrom pathlib import Path\n"
        f"cli = {str(owned['cli'])!r}\n"
        f"owner = Path({str(owned['owner'])!r})\n"
        f"target = Path({str(owned['target'])!r})\n"
        f"count = Path({str(count)!r})\nphase = {phase!r}\n"
        "if phase == 'verify' and sys.argv[1] == 'inspect':\n"
        "    (target / 'tracked.txt').write_text('changed after add\\n')\n"
        "result = subprocess.run([cli, *sys.argv[1:]], capture_output=True)\n"
        "if phase == 'approval' and sys.argv[1] == 'check-owned-restore':\n"
        "    n = int(count.read_text()) + 1 if count.exists() else 1\n"
        "    count.write_text(str(n))\n"
        "    if n == 2: owner.write_bytes(owner.read_bytes() + b' ')\n"
        "sys.stdout.buffer.write(result.stdout)\n"
        "sys.stderr.buffer.write(result.stderr)\nsys.exit(result.returncode)\n"
    )
    wrapper.chmod(0o700)
    owned["cli"] = wrapper
    return owned


def test_state_race_between_approval_and_add_is_refused(owned):
    # Arrange
    racing_policy(owned, "approval")
    receipt = restore(owned)
    # Act
    # Assert
    with pytest.raises(WorktreePolicyError, match="raced"):
        restore(owned, apply=True, receipt_sha256=receipt["receipt_sha256"])


def _owner_changed(owned):
    owned["owner"].write_bytes(owned["owner"].read_bytes() + b" ")


def _primary_changed(owned):
    (owned["repo"] / "tracked.txt").write_text("changed\n")


def _wrong_repository(owned):
    owned["config"].workdir = str(owned["repo"].parent / "foreign")


@pytest.fixture(
    params=[
        pytest.param(lambda owned: None, id="missing-receipt"),
        pytest.param(_owner_changed, id="owner-changed"),
        pytest.param(_primary_changed, id="primary-changed"),
        pytest.param(_advance_retained_tip, id="tip-changed"),
        pytest.param(
            lambda owned: owned["target"].mkdir(parents=True), id="target-created"
        ),
        pytest.param(_wrong_repository, id="wrong-repository"),
        pytest.param(
            lambda owned: racing_policy(owned, "approval"), id="approval-race"
        ),
    ]
)
def refused_checkout(owned, request):
    mutate = request.param
    if request.node.callspec.id == "approval-race":
        mutate(owned)
    receipt = restore(owned)
    if request.node.callspec.id != "approval-race":
        mutate(owned)
    digest = (
        None
        if request.node.callspec.id == "missing-receipt"
        else receipt["receipt_sha256"]
    )
    with pytest.raises(WorktreePolicyError):
        restore(owned, apply=True, receipt_sha256=digest)
    return owned["target"]


def test_refusal_leaves_no_checkout_or_git_registration(refused_checkout):
    # Arrange
    target = refused_checkout
    # Act
    registered = (target / ".git").exists()
    # Assert
    assert registered is False


@pytest.fixture
def failed_postverification(owned):
    # Arrange
    racing_policy(owned, "verify")
    receipt = restore(owned)
    # Act
    # Assert
    with pytest.raises(WorktreePolicyError, match="postverification"):
        restore(owned, apply=True, receipt_sha256=receipt["receipt_sha256"])
    return owned


def test_postverification_failure_preserves_checkout_evidence(failed_postverification):
    # Arrange
    owned = failed_postverification
    # Act
    exists = (owned["target"] / ".git").exists()
    # Assert
    assert exists


def test_postverification_failure_preserves_retained_tip(failed_postverification):
    # Arrange
    owned = failed_postverification
    # Act
    head = git(owned["target"], "rev-parse", "HEAD")
    # Assert
    assert head == owned["tip"]


def test_postverification_failure_preserves_primary_branch(failed_postverification):
    # Arrange
    owned = failed_postverification
    # Act
    branch = git(owned["repo"], "branch", "--show-current")
    # Assert
    assert branch == "fix/retained-primary"
