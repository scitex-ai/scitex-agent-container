"""Restore an absent owned checkout without changing its retained identity.

Neutral policy approves the exact shape. SAC compares review receipts, invokes
normal Git worktree add, and verifies preserved primary/owner/branch state.
It neither launches an agent nor adopts, rewrites, or deletes ownership.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
from pathlib import Path
from typing import Any

from ._worktree_policy import (
    DEFAULT_WORKTREE_POLICY_CLI,
    WorktreePolicyError,
    _git,
    _invoke,
    _owner_path,
    _read_owner,
    _sha256,
    _text,
)


def _digest(value: dict[str, Any]) -> str:
    raw = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(raw).hexdigest()


def _approval(config, expected_tip, cli, timeout_s):
    projection = _invoke(cli, ["check-projections"], timeout_s=timeout_s)
    owner = _owner_path(config).absolute()
    record = _read_owner(owner)
    if record is None:
        raise WorktreePolicyError(
            "owned restore requires the original ownership record"
        )
    repo = Path(_text(record, "repo_root"))
    authored = str(Path(config.workdir).expanduser().absolute())
    if authored not in {str(repo), _text(record, "worktree")}:
        raise WorktreePolicyError(
            "configured workdir differs from the recorded repository and owned checkout"
        )
    result = _invoke(
        cli,
        [
            "check-owned-restore",
            "--repo",
            str(repo),
            "--agent",
            str(config.name),
            "--owner-file",
            str(owner),
            "--expected-tip",
            expected_tip,
        ],
        timeout_s=timeout_s,
    )
    for key in ("policy_sha256", "projection_sha256"):
        if _sha256(result, key) != _sha256(projection, key):
            raise WorktreePolicyError(
                "restore policy/projection changed during approval"
            )
    for key in ("owner_sha256", "primary_status_sha256", "worktree_registry_sha256"):
        _sha256(result, key)
    for key in ("worktree", "restore_branch", "primary_head", "git_common_dir"):
        _text(result, key)
    command = [
        "git",
        "-C",
        str(repo),
        "worktree",
        "add",
        result["worktree"],
        result["restore_branch"],
    ]
    if (
        result.get("operation") != "restore-owned-worktree"
        or result.get("agent") != str(config.name)
        or result.get("repo_root") != str(repo)
        or result.get("owner_file") != str(owner)
        or result.get("expected_tip") != expected_tip
        or result.get("command") != command
    ):
        raise WorktreePolicyError(
            "restore approval does not describe the exact requested operation"
        )
    status_argv = result.get("primary_status_argv")
    if (
        not isinstance(status_argv, list)
        or not status_argv
        or status_argv[0] != "status"
        or any(not isinstance(arg, str) for arg in status_argv)
    ):
        raise WorktreePolicyError(
            "restore approval has no primary status inspection argv"
        )
    return result


def _verify(receipt, cli, timeout_s):
    repo = Path(receipt["repo_root"])
    target = Path(receipt["worktree"])
    info = _invoke(cli, ["inspect", "--repo", str(target)], timeout_s=timeout_s)
    status = _git(repo, *receipt["primary_status_argv"]).stdout.strip()
    unchanged = (
        hashlib.sha256(Path(receipt["owner_file"]).read_bytes()).hexdigest()
        == receipt["owner_sha256"]
        and _git(repo, "rev-parse", "HEAD").stdout.strip() == receipt["primary_head"]
        and _git(repo, "branch", "--show-current").stdout.strip() == receipt["branch"]
        and hashlib.sha256(status.encode()).hexdigest()
        == receipt["primary_status_sha256"]
        and not _git(repo, "diff", "HEAD", "--", ".worktrees").stdout.strip()
        and info.get("surface") == "linked-worktree"
        and info.get("git_common_dir") == receipt["git_common_dir"]
        and info.get("branch") == receipt["restore_branch"]
        and info.get("policy_sha256") == receipt["policy_sha256"]
        and info.get("projection_sha256") == receipt["projection_sha256"]
        and _git(target, "rev-parse", "HEAD").stdout.strip() == receipt["expected_tip"]
        and _git(
            repo, "rev-parse", "refs/heads/" + receipt["restore_branch"]
        ).stdout.strip()
        == receipt["expected_tip"]
        and not _git(target, "status", "--porcelain=v1").stdout.strip()
    )
    if not unchanged:
        raise WorktreePolicyError(
            "restore postverification failed; preserve the checkout for inspection, do not force or delete it"
        )


def restore_owned_task_worktree(
    config: Any,
    *,
    expected_tip: str,
    apply: bool = False,
    receipt_sha256: str | None = None,
    cli_path: str | Path | None = None,
    timeout_s: float = 10,
) -> dict[str, Any]:
    """Dry-run one missing owned checkout; apply only the exact reviewed receipt.

    Apply serializes official restores and repeats full neutral approval just
    before Git. Git's ordinary no-force add handles path/branch contention.
    A failed postverification leaves evidence intact for explicit review.
    """
    cli = Path(cli_path or DEFAULT_WORKTREE_POLICY_CLI).expanduser()
    if apply and not receipt_sha256:
        raise WorktreePolicyError("apply requires the exact dry-run receipt SHA256")
    approval = _approval(config, expected_tip, cli, timeout_s)
    digest = _digest(approval)
    if apply and digest != receipt_sha256:
        raise WorktreePolicyError(
            "restore receipt changed since review; run dry-run again"
        )
    result = {
        "mode": "apply" if apply else "dry-run",
        "receipt": approval,
        "receipt_sha256": digest,
        "verified": False,
    }
    if not apply:
        return result
    lock_path = Path(approval["owner_file"]).with_name("worktree-restore.lock")
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    except OSError as exc:
        raise WorktreePolicyError(f"cannot acquire owned restore lock: {exc}") from exc
    if not stat.S_ISREG(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise WorktreePolicyError("owned restore lock must be a regular file")
    with os.fdopen(descriptor, "a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise WorktreePolicyError(
                "another official owned restore is in progress"
            ) from exc
        current = _approval(config, expected_tip, cli, timeout_s)
        if current != approval:
            raise WorktreePolicyError(
                "restore state raced after approval; no worktree was added"
            )
        _git(
            Path(approval["repo_root"]),
            "worktree",
            "add",
            approval["worktree"],
            approval["restore_branch"],
        )
        _verify(approval, cli, timeout_s)
    result["verified"] = True
    return result


__all__ = ["restore_owned_task_worktree"]
