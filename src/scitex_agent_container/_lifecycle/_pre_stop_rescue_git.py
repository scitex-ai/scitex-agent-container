"""Git-operation primitives for ``_pre_stop_rescue``.

Extracted from the orchestrator to keep both files under the per-file
line cap. The orchestrator
(:mod:`_lifecycle._pre_stop_rescue`) owns the policy (which roots to
walk, when to skip, when to tarball); this module owns the how
(subprocess wrappers, branch detection, diff-tarball writing).

There is deliberately NO push primitive here. The rescue is a LOCAL
save and nothing more — see the "never publishes" contract in
:mod:`_lifecycle._pre_stop_rescue`. A push helper in this module would
be a loaded gun: the orchestrator is one call away from it, and the
whole point is that no code path can publish on the agent's behalf.

No mocks. Real subprocesses against real git worktrees.
"""

from __future__ import annotations

import logging
import re
import subprocess
from pathlib import Path

log = logging.getLogger(__name__)

__all__ = [
    "OWNER_MARKER_NAME",
    "checkout_branch",
    "commit_dirty",
    "current_branch",
    "is_dirty",
    "is_git_worktree",
    "move_dirty_to_side_branch",
    "rescue_branch_name",
    "worktree_owner",
    "write_diff_tarball",
]

# Ownership marker filename. Stored in the worktree's PRIVATE gitdir
# (``<git-dir>/sac-owner``), NEVER in the working tree — see
# ``worktree_owner`` for why. The stamp is written at worktree-creation
# time by the ``WorktreeCreate`` hook (``claude_worktree_hooks``); the
# rescue reads it here to decide OWNERSHIP.
OWNER_MARKER_NAME: str = "sac-owner"


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


def worktree_owner(path: Path, *, timeout: float) -> str | None:
    """Return the owning agent id stamped at ``<git-dir>/sac-owner``, or None.

    OWNERSHIP is the fix for the shared-checkout mis-attribution bug: the
    ``scitex-cards`` lane runs four agents over ONE physical checkout (a
    symlink farm), so its ``.git`` — and therefore ``.worktrees/`` and
    ``git worktree list`` — is SHARED. A stopping agent's rescue walk sees
    every peer's worktree and cannot tell them apart from the branch name.

    So each worktree is stamped with its creating agent's id at creation
    time (``WorktreeCreate`` hook). The stamp lives in the worktree's
    PRIVATE gitdir — ``<main>/.git/worktrees/<name>/sac-owner`` for a
    linked worktree, ``<checkout>/.git/sac-owner`` for a primary checkout
    — resolved here via ``git rev-parse --absolute-git-dir``. That path is
    OUTSIDE the working tree, so the ``git add -A`` every rescue runs can
    NEVER stage it (git does not stage anything under ``.git/``).

    Returns the stamped id (stripped) when the marker is present and
    non-empty; ``None`` when the marker is ABSENT or unreadable, or when
    the git-dir cannot be resolved. ``None`` means "unknown owner" — the
    caller DEFAULT-DENIES an unknown owner on a ``.worktrees`` child.
    NEVER raises.
    """
    rc, out, _ = _run(
        ["git", "rev-parse", "--absolute-git-dir"], cwd=path, timeout=timeout
    )
    if rc != 0:
        return None
    git_dir = out.strip()
    if not git_dir:
        return None
    marker = Path(git_dir) / OWNER_MARKER_NAME
    try:
        text = marker.read_text(encoding="utf-8")
    except (
        OSError,
        FileNotFoundError,
    ):  # stx-allow: fallback (reason: an absent/unreadable marker is "unknown owner", not a crash — the caller default-denies.)
        return None
    owner = text.strip()
    return owner or None


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


def rescue_branch_name(agent_name: str, timestamp: str) -> str:
    """Return the dedicated side-branch name ``rescue/<agent>-<timestamp>``.

    ``agent_name`` is sanitised to the git ref-safe alphabet so a stray
    space / slash / colon in an agent id can't produce an invalid ref.
    """
    safe_agent = re.sub(r"[^A-Za-z0-9._-]", "-", agent_name).strip("-") or "agent"
    return f"rescue/{safe_agent}-{timestamp}"


def checkout_branch(path: Path, branch: str, *, timeout: float) -> tuple[bool, str]:
    """``git checkout <branch>`` (existing branch). Returns (ok, error)."""
    rc, _, err = _run(["git", "checkout", branch], cwd=path, timeout=timeout)
    if rc != 0:
        return (False, err.strip() or "checkout failed")
    return (True, "")


def move_dirty_to_side_branch(
    path: Path, *, agent_name: str, timestamp: str, timeout: float
) -> tuple[bool, str, str]:
    """Carry the dirty tree onto a fresh ``rescue/`` side-branch and commit it.

    Used when the checkout sits on a PROTECTED branch (``develop`` /
    ``main`` / ``master`` / ``release/*``): committing the rescue there
    would leave a local-only commit that diverges the branch from its
    remote and breaks ``git pull --ff-only`` + the deploy-freshness
    cron. Instead we ``git checkout -b rescue/<agent>-<ts>`` (which
    CARRIES the uncommitted tree — tracked + untracked — onto the new
    branch off the same HEAD, so no conflict is possible), then commit
    the dirty tree THERE with the stable rescue message.

    On success the caller is left standing ON the rescue branch (HEAD =
    the rescue commit) so a subsequent diff-tarball captures the right
    commit; the caller MUST call ``checkout_branch``
    to return to the protected branch — which then reverts to a clean,
    ff-able tree since all the work is now committed on the side branch.

    Returns ``(ok, rescue_branch, error)``. On failure the rescue branch
    name is still returned (best effort) and ``error`` is populated; the
    caller decides on the tarball fallback.
    """
    rescue_branch = rescue_branch_name(agent_name, timestamp)
    rc, _, err = _run(
        ["git", "checkout", "-b", rescue_branch], cwd=path, timeout=timeout
    )
    if rc != 0:
        return (False, rescue_branch, f"git checkout -b failed: {err.strip()}")
    ok_commit, commit_msg = commit_dirty(
        path, agent_name=agent_name, timestamp=timestamp, timeout=timeout
    )
    if not ok_commit:
        return (False, rescue_branch, commit_msg)
    return (True, rescue_branch, "")


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
    Used as the fallback when the rescue COMMIT could not land — then
    the tarball is the only copy of the dirty tree. (It is no longer a
    push fallback: the rescue does not push, and a commit that landed
    is already durable on the bind-mounted host disk.) Returns the
    written path on success, ``None`` on failure (logged once, never
    raised).
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
        patch_text = out_diff if rc_diff == 0 else ""
    else:
        # Re-run capturing stdout to file — single source of truth.
        proc = subprocess.run(
            ["git", "format-patch", "-1", "--stdout"],
            cwd=str(path),
            timeout=timeout,
            capture_output=True,
            text=True,
        )
        patch_text = proc.stdout
    # ALSO capture any still-uncommitted work. On the happy path (rescue
    # committed on a side-branch) this is empty; on the rare path where
    # the commit never landed it is the ONLY copy of the dirty tree, so
    # appending it guarantees the tarball never silently loses work.
    _, out_uncommitted, _ = _run(["git", "diff", "HEAD"], cwd=path, timeout=timeout)
    if out_uncommitted.strip():
        patch_text += (
            "\n### pre-stop rescue: uncommitted working-tree diff ###\n"
            + out_uncommitted
        )
    diff_path.write_text(patch_text)
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
