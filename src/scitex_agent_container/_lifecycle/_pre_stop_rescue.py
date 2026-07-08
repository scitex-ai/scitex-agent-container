"""Fleet-default pre-stop rescue — commit + push dirty worktrees before stop.

Operator priority (lead a2a ``efa48850daf248ed9fe3ae5232677b2b``): make
restart cheap. Before this module, ``sac agents stop`` would happily
SIGTERM the apptainer process with uncommitted work in the agent's
worktrees still sitting there — on the next start the work was gone.
Operator-declared ``pre_stop`` hooks could rescue it, but no fleet
default shipped, so every agent had to opt in by spec edit (the
band-aid the operator explicitly dislikes).

This module is the SAC-side auto-rescue. It runs once per
``agent_stop`` call, BEFORE the operator's ``pre_stop`` hooks fire,
walks every git worktree under the agent's work roots, and for each
DIRTY worktree:

* On a NON-protected topic branch: commits the dirty changes locally
  with a stable rescue message and pushes the branch to its tracking
  remote.
* On a PROTECTED branch (``develop`` / ``main`` / ``master`` /
  ``release/*``): carries the dirty tree onto a dedicated
  ``rescue/<agent>-<utc>`` side-branch, commits + pushes it THERE, and
  returns the checkout to the protected branch with a clean tree — so
  the protected branch NEVER gains a local-only commit that would
  diverge it from origin and break ``git pull --ff-only`` + the
  deploy-freshness cron (the root cause this module was hardened to
  prevent, fleet-wide 2026-07).
* On push failure (offline / no remote): writes a diff-tarball under
  ``<state_dir>/rescue/<branch>-<utc>.tar.gz`` so the next start can
  re-apply. The dirty work is never dropped.

The whole pass is bounded by ``RESCUE_GRACE_SECONDS`` (default 60s)
so the rescue can NEVER wedge a restart — if the budget elapses,
whatever has been committed locally + the diff-tarballs landed are
preserved. Operator's directive: never lose, never wedge.

Walked roots (per lead refinement):
  * ``config.workdir`` — primary tree.
  * ``<workdir>/.worktrees/*`` — subagent worktrees (the in-container
    claude session creates them; restart kills them; the reap-bug
    lead-learnings/19 also destroys them silently — unify here).
  * ``<workdir>/worktrees/*`` — legacy convention still in use.

Git-op primitives live in :mod:`_pre_stop_rescue_git` (split for the
per-file line cap).

No mocks. Real ``tmp_path`` git repos in tests.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Iterable

from ._pre_stop_rescue_git import (
    checkout_branch,
    commit_dirty,
    current_branch,
    is_dirty,
    is_git_worktree,
    move_dirty_to_side_branch,
    push_branch,
    write_diff_tarball,
)

log = logging.getLogger(__name__)

__all__ = [
    "RESCUE_DIR_NAME",
    "RESCUE_GRACE_SECONDS",
    "is_protected_branch",
    "rescue_worktree",
    "rescue_worktrees_for_agent",
    "run_pre_stop_rescue",
]


RESCUE_GRACE_SECONDS: float = 60.0
RESCUE_DIR_NAME: str = "rescue"

# Branch-name protection list — operator-mandated. On these SHARED
# branches the rescue must NEVER leave a local-only commit: doing so
# diverges the branch from its remote and breaks ``git pull --ff-only``
# + the deploy-freshness cron (root cause of undeployed merged PRs,
# fleet-wide, 2026-07). Instead the dirty tree is carried onto a
# dedicated ``rescue/<agent>-<ts>`` side-branch (see ``rescue_worktree``)
# so the protected branch stays clean + ff-able. ``develop`` is the
# shared work checkout and MUST be on this list.
_PROTECTED_BRANCH_PREFIXES: tuple[str, ...] = (
    "develop",
    "main",
    "master",
    "release",
)


def is_protected_branch(branch: str) -> bool:
    """Return True if ``branch`` matches the protected list.

    Exact match OR prefix-with-``/`` (``release`` matches ``release``
    and ``release/2.0`` but NOT ``release-notes``). Empty / detached
    (``""``) → True (refuse to commit-in-place on the unclassifiable).
    """
    if not branch:
        return True
    for prefix in _PROTECTED_BRANCH_PREFIXES:
        if branch == prefix or branch.startswith(prefix + "/"):
            return True
    return False


def rescue_worktree(
    path: Path,
    *,
    agent_name: str,
    timestamp: str,
    rescue_root: Path,
    timeout: float,
) -> dict[str, object]:
    """Run the rescue sequence on a single git worktree.

    Returns a dict with: ``path``, ``branch``, ``committed``,
    ``pushed``, ``tarball`` (Path or None), ``protected``,
    ``rescue_branch`` (str; the ``rescue/`` side-branch used on a
    protected branch, else ``""``), ``error``. NEVER raises.

    On a NON-protected topic branch the dirty tree is committed in place
    and pushed (existing behavior). On a PROTECTED branch (``develop`` /
    ``main`` / ``master`` / ``release/*``) the dirty tree is instead
    carried onto a fresh ``rescue/<agent>-<ts>`` side-branch, committed +
    pushed there, and the checkout is returned to the protected branch
    with a clean tree — so the protected branch NEVER gains a local-only
    commit and stays ``git pull --ff-only`` friendly.
    """
    result: dict[str, object] = {
        "path": path,
        "branch": "",
        "committed": False,
        "pushed": False,
        "tarball": None,
        "protected": False,
        "rescue_branch": "",
        "error": "",
    }
    if not is_git_worktree(path):
        result["error"] = "not a git worktree"
        return result
    if not is_dirty(path, timeout=timeout):
        return result  # clean — nothing to rescue
    branch = current_branch(path, timeout=timeout)
    result["branch"] = branch
    protected = is_protected_branch(branch)
    result["protected"] = protected
    if protected:
        return _rescue_protected(
            path,
            result=result,
            branch=branch,
            agent_name=agent_name,
            timestamp=timestamp,
            rescue_root=rescue_root,
            timeout=timeout,
        )
    # --- non-protected topic branch: commit in place + push -----------
    ok_commit, commit_msg = commit_dirty(
        path, agent_name=agent_name, timestamp=timestamp, timeout=timeout
    )
    result["committed"] = ok_commit
    if not ok_commit:
        result["error"] = commit_msg
        # Even when commit fails, write a tarball so work survives.
        result["tarball"] = write_diff_tarball(
            path,
            rescue_root=rescue_root,
            branch=branch,
            agent_name=agent_name,
            timestamp=timestamp,
            timeout=timeout,
        )
        return result
    ok_push, push_err = push_branch(path, branch, timeout=timeout)
    result["pushed"] = ok_push
    if not ok_push:
        result["error"] = push_err
        result["tarball"] = write_diff_tarball(
            path,
            rescue_root=rescue_root,
            branch=branch,
            agent_name=agent_name,
            timestamp=timestamp,
            timeout=timeout,
        )
    return result


def _rescue_protected(
    path: Path,
    *,
    result: dict[str, object],
    branch: str,
    agent_name: str,
    timestamp: str,
    rescue_root: Path,
    timeout: float,
) -> dict[str, object]:
    """Rescue a dirty tree that sits on a PROTECTED branch, without polluting it.

    Carries the tree onto a ``rescue/<agent>-<ts>`` side-branch, commits
    + pushes it there, then returns the checkout to ``branch`` so the
    protected ref is unchanged from origin (``ff-only`` pull still
    works). Falls back to the diff-tarball if the side-branch can't be
    created / committed / pushed — the dirty work is never dropped. The
    protected branch is restored on EVERY path.
    """
    ok_side, rescue_branch, side_err = move_dirty_to_side_branch(
        path, agent_name=agent_name, timestamp=timestamp, timeout=timeout
    )
    result["rescue_branch"] = rescue_branch
    if not ok_side:
        # Could not carry the work onto a side-branch (checkout -b or the
        # commit failed). The tree is still uncommitted, so the tarball is
        # the last line of defense; then restore the protected branch.
        result["error"] = side_err
        result["tarball"] = write_diff_tarball(
            path,
            rescue_root=rescue_root,
            branch=branch,
            agent_name=agent_name,
            timestamp=timestamp,
            timeout=timeout,
        )
        checkout_branch(path, branch, timeout=timeout)
        return result
    # Work is committed on the rescue side-branch; protected HEAD untouched.
    result["committed"] = True
    ok_push, push_err = push_branch(path, rescue_branch, timeout=timeout)
    result["pushed"] = ok_push
    if not ok_push:
        # Offline / no remote: keep the local rescue branch AND drop a
        # tarball so the work survives even if that branch is pruned.
        # HEAD is still the rescue commit here, so the tarball captures it.
        result["error"] = push_err
        result["tarball"] = write_diff_tarball(
            path,
            rescue_root=rescue_root,
            branch=rescue_branch,
            agent_name=agent_name,
            timestamp=timestamp,
            timeout=timeout,
        )
    # ALWAYS return to the protected branch so its ref equals origin's and
    # ``git pull --ff-only`` keeps working.
    ok_back, back_err = checkout_branch(path, branch, timeout=timeout)
    if not ok_back:
        prefix = f"{result['error']}; " if result["error"] else ""
        result["error"] = f"{prefix}restore to {branch} failed: {back_err}"
    return result


def _candidate_roots(workdir: Path) -> Iterable[Path]:
    """Yield the directories the pass walks for rescue candidates.

    Per lead refinement (a2a efa48850): cover the subagent
    ``.worktrees/*`` and the legacy ``worktrees/*`` — those are
    exactly what restart kills + what the reap-bug
    (lead-learnings/19) silently destroys.
    """
    yield workdir
    sub = workdir / ".worktrees"
    if sub.is_dir():
        for child in sorted(sub.iterdir()):
            if child.is_dir():
                yield child
    legacy = workdir / "worktrees"
    if legacy.is_dir():
        for child in sorted(legacy.iterdir()):
            if child.is_dir():
                yield child


def rescue_worktrees_for_agent(
    *,
    agent_name: str,
    workdir: Path,
    state_dir: Path,
    grace_seconds: float = RESCUE_GRACE_SECONDS,
    timestamp: str | None = None,
) -> list[dict[str, object]]:
    """Walk + rescue every dirty worktree under ``workdir`` within ``grace_seconds``.

    Bounds the pass by ``grace_seconds`` so a single slow worktree
    can't wedge restart. On budget exhaustion the remaining
    candidates are skipped and a structured log line names them;
    whatever has been committed and tarballed so far is preserved.

    Returns one result dict per scanned candidate (clean ones produce
    an entry with ``committed=False`` and no error so the caller can
    count rescues vs no-ops in the same log).
    """
    rescue_root = state_dir / RESCUE_DIR_NAME
    if timestamp is None:
        timestamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    results: list[dict[str, object]] = []
    deadline = time.monotonic() + grace_seconds
    per_candidate_timeout = max(2.0, grace_seconds / 4.0)
    for candidate in _candidate_roots(workdir):
        if time.monotonic() >= deadline:
            log.warning(
                "pre-stop rescue: grace budget elapsed before scanning %s; "
                "remaining candidates skipped, preserved work stays",
                candidate,
            )
            results.append(
                {
                    "path": candidate,
                    "branch": "",
                    "committed": False,
                    "pushed": False,
                    "tarball": None,
                    "protected": False,
                    "rescue_branch": "",
                    "error": "grace budget elapsed",
                }
            )
            continue
        results.append(
            rescue_worktree(
                candidate,
                agent_name=agent_name,
                timestamp=timestamp,
                rescue_root=rescue_root,
                timeout=min(per_candidate_timeout, deadline - time.monotonic()),
            )
        )
    return results


def run_pre_stop_rescue(config) -> None:
    """Lifecycle entry point — called by ``_lifecycle/_stop.agent_stop``.

    Resolves the agent's workdir + state_dir from ``config``, runs
    the rescue pass with the default grace budget, and emits one
    structured log line summarising the outcome. NEVER raises — the
    rescue is best-effort defence in depth; the operator's
    ``pre_stop`` hooks still get to do project-specific work after.
    """
    try:
        workdir_str = (
            getattr(config, "expanded_workdir", None)
            or getattr(config, "workdir", None)
            or ""
        )
        if not workdir_str:
            return
        workdir = Path(str(workdir_str)).expanduser()
        if not workdir.is_dir():
            return
        from .._runners._session_state import state_dir_for

        state_dir = Path(state_dir_for(getattr(config, "name", "")))
    except (
        Exception
    ) as exc:  # stx-allow: fallback (reason: rescue setup must never block stop.)
        log.warning("pre-stop rescue: setup failed (%r); skipping", exc)
        return
    try:
        results = rescue_worktrees_for_agent(
            agent_name=getattr(config, "name", "?"),
            workdir=workdir,
            state_dir=state_dir,
        )
    except Exception as exc:  # stx-allow: fallback (reason: rescue must never block stop; the per-worktree loop is already defensive.)
        log.warning("pre-stop rescue: unexpected failure (%r); skipping", exc)
        return
    committed = sum(1 for r in results if r.get("committed"))
    pushed = sum(1 for r in results if r.get("pushed"))
    tarballed = sum(1 for r in results if r.get("tarball"))
    skipped = sum(1 for r in results if r.get("error"))
    log.info(
        "pre-stop rescue: agent=%s committed=%d pushed=%d tarballed=%d skipped=%d",
        getattr(config, "name", "?"),
        committed,
        pushed,
        tarballed,
        skipped,
    )
