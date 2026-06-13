"""Git-operation primitives for ``_pre_stop_rescue``.

Extracted from the orchestrator to keep both files under the per-file
line cap. The orchestrator
(:mod:`_lifecycle._pre_stop_rescue`) owns the policy (which roots to
walk, when to skip, when to push vs tarball); this module owns the
how (subprocess wrappers, branch detection, diff-tarball writing).

No mocks. Real subprocesses against real git worktrees.
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

__all__ = [
    "commit_dirty",
    "current_branch",
    "is_dirty",
    "is_git_worktree",
    "push_branch",
    "write_diff_tarball",
]


def _run(cmd: list[str], *, cwd: Path, timeout: float) -> tuple[int, str, str]:
    """Run ``cmd`` under ``cwd`` capturing output. NEVER raises."""
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(cwd),
            timeout=timeout,
            capture_output=True,
            text=True,
        )
    except subprocess.TimeoutExpired as exc:  # stx-allow: fallback (reason: the rescue budget is the caller's invariant.)
        return (-1, "", f"timeout: {exc}")
    except (
        OSError,
        FileNotFoundError,
    ) as exc:  # stx-allow: fallback (reason: missing git / unreadable cwd must not crash the rescue pass.)
        return (-1, "", str(exc))
    return (proc.returncode, proc.stdout, proc.stderr)


def is_git_worktree(path: Path) -> bool:
    """Return True if ``path/.git`` exists (regular repo OR linked worktree)."""
    if not path.is_dir():
        return False
    return (path / ".git").exists()


def current_branch(path: Path, *, timeout: float) -> str:
    """Return the current branch name; empty string for detached/error."""
    rc, out, _ = _run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"], cwd=path, timeout=timeout
    )
    if rc != 0:
        return ""
    branch = out.strip()
    return "" if branch == "HEAD" else branch


def is_dirty(path: Path, *, timeout: float) -> bool:
    """Return True iff ``git status --porcelain`` reports any changes."""
    rc, out, _ = _run(["git", "status", "--porcelain"], cwd=path, timeout=timeout)
    if rc != 0:
        # Treat git errors as "presumed dirty" — never silently skip a
        # possibly-real rescue case.
        return True
    return bool(out.strip())


def commit_dirty(
    path: Path, *, agent_name: str, timestamp: str, timeout: float
) -> tuple[bool, str]:
    """Stage + commit. Returns (ok, message_or_error).

    Commit message: ``rescue: pre-stop autosave <agent>@<UTC>`` — stable
    so the operator can grep their history for rescued commits.
    """
    rc_add, _, err_add = _run(["git", "add", "-A"], cwd=path, timeout=timeout)
    if rc_add != 0:
        return (False, f"git add failed: {err_add.strip()}")
    msg = f"rescue: pre-stop autosave {agent_name}@{timestamp}"
    rc_commit, _, err_commit = _run(
        ["git", "commit", "-m", msg], cwd=path, timeout=timeout
    )
    if rc_commit != 0:
        return (False, f"git commit failed: {err_commit.strip()}")
    return (True, msg)


def push_branch(path: Path, branch: str, *, timeout: float) -> tuple[bool, str]:
    """Push ``branch`` to ``origin`` with ``--force-with-lease -u``."""
    rc, _, err = _run(
        ["git", "push", "--force-with-lease", "-u", "origin", branch],
        cwd=path,
        timeout=timeout,
    )
    if rc != 0:
        return (False, err.strip() or "push failed")
    return (True, "")


def write_diff_tarball(
    path: Path,
    *,
    rescue_root: Path,
    branch: str,
    agent_name: str,
    timestamp: str,
    timeout: float,
) -> Path | None:
    """Write a ``.tar.gz`` bundling the worktree's last-commit patch + manifest.

    Lands under ``<rescue_root>/<safe-branch>-<timestamp>.tar.gz``.
    Used as the fallback when ``push_branch`` couldn't ship the work
    upstream. Returns the written path on success, ``None`` on
    failure (logged once, never raised).
    """
    rescue_root.mkdir(parents=True, exist_ok=True)
    safe_branch = branch.replace("/", "-") or "_detached"
    tarball = rescue_root / f"{safe_branch}-{timestamp}.tar.gz"
    diff_path = rescue_root / f".{safe_branch}-{timestamp}.diff"
    rc, _, _ = _run(
        ["git", "format-patch", "-1", "--stdout"], cwd=path, timeout=timeout
    )
    if rc != 0:
        # No parent commit (first commit case) — fall back to diff HEAD.
        rc_diff, out_diff, _ = _run(["git", "diff", "HEAD"], cwd=path, timeout=timeout)
        diff_path.write_text(out_diff if rc_diff == 0 else "")
    else:
        # Re-run capturing stdout to file — single source of truth.
        proc = subprocess.run(
            ["git", "format-patch", "-1", "--stdout"],
            cwd=str(path),
            timeout=timeout,
            capture_output=True,
            text=True,
        )
        diff_path.write_text(proc.stdout)
    manifest_path = rescue_root / f".{safe_branch}-{timestamp}.manifest"
    manifest_path.write_text(
        f"agent: {agent_name}\nbranch: {branch}\ntimestamp: {timestamp}\n"
        f"worktree: {path}\nreason: pre-stop rescue, push fallback\n"
    )
    rc_tar, _, err_tar = _run(
        [
            "tar",
            "-czf",
            str(tarball),
            "-C",
            str(rescue_root),
            diff_path.name,
            manifest_path.name,
        ],
        cwd=rescue_root,
        timeout=timeout,
    )
    if rc_tar != 0:
        log.warning(
            "pre-stop rescue: tar fallback failed for %s (%s)",
            path,
            err_tar.strip(),
        )
        return None
    for staging in (diff_path, manifest_path):
        try:
            staging.unlink()
        except FileNotFoundError:  # stx-allow: fallback (reason: tar may have left the staging files in place on some platforms.)
            pass
    return tarball
