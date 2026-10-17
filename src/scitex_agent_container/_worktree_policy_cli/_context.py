"""Inspect Git context and decide authority versus linked-worktree access."""

from __future__ import annotations

from pathlib import Path
import subprocess
from typing import Any

from ._policy import PolicyError, projection_hash


def run_git(directory: Path, *args: str) -> str:
    try:
        result = subprocess.run(
            ["git", "-C", str(directory), *args],
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise PolicyError(f"git inspection failed in {directory}: {exc}", 4) from exc
    if result.returncode != 0:
        detail = result.stderr.strip() or f"git exited {result.returncode}"
        raise PolicyError(f"git inspection failed in {directory}: {detail}", 4)
    return result.stdout.strip()


def inspect_repo(
    directory: Path, policy: dict[str, Any], source_hash: str, projection_dir: Path
) -> dict[str, Any]:
    directory = directory.resolve()
    root = Path(run_git(directory, "rev-parse", "--show-toplevel")).resolve()
    git_dir_raw = Path(run_git(directory, "rev-parse", "--git-dir"))
    common_raw = Path(run_git(directory, "rev-parse", "--git-common-dir"))
    git_dir = (
        (directory / git_dir_raw).resolve()
        if not git_dir_raw.is_absolute()
        else git_dir_raw.resolve()
    )
    common_dir = (
        (directory / common_raw).resolve()
        if not common_raw.is_absolute()
        else common_raw.resolve()
    )
    branch = run_git(directory, "rev-parse", "--abbrev-ref", "HEAD")
    surface = "authority" if git_dir == common_dir else "linked-worktree"
    return {
        "schema_version": policy["schema_version"],
        "policy_id": policy["policy_id"],
        "policy_sha256": source_hash,
        "projection_sha256": projection_hash(projection_dir),
        "repo_root": str(root),
        "git_common_dir": str(common_dir),
        "branch": branch,
        "surface": surface,
    }


def is_topic(branch: str, policy: dict[str, Any]) -> bool:
    return any(branch.startswith(prefix) for prefix in policy["topic_branch_prefixes"])


def decide_context(
    info: dict[str, Any], intent: str, policy: dict[str, Any]
) -> tuple[bool, str]:
    branch = info["branch"]
    surface = info["surface"]
    authority = policy["authority_branch"]
    if surface == "authority":
        if branch != authority:
            return (
                False,
                f"authority checkout must remain on {authority!r}; found {branch!r}",
            )
        if intent != "read":
            return (
                False,
                f"authority checkout on {authority!r} is read-only; {intent!r} requires a linked worktree",
            )
        return (
            True,
            "authority checkout is on the required branch and the intent is read-only",
        )
    if not is_topic(branch, policy):
        return (
            False,
            f"linked worktree branch {branch!r} is not an allowed topic branch",
        )
    if intent == "branch-change":
        return (
            False,
            "a linked worktree is branch-stable; create another linked worktree for another topic",
        )
    return True, "linked worktree is on an allowed topic branch"


def resolve_edit_repo(path: Path) -> Path | None:
    start = path if path.is_dir() else path.parent
    result = subprocess.run(
        ["git", "-C", str(start), "rev-parse", "--show-toplevel"],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    return start if result.returncode == 0 else None
