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

* Commits the dirty changes locally with a stable rescue message.
* Pushes the branch to its tracking remote IF the branch is non-
  protected (no ``main`` / ``master`` / ``release/*``).
* On push failure or protected branch: writes a diff-tarball under
  ``<state_dir>/rescue/<branch>-<utc>.tar.gz`` so the next start can
  re-apply.

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
    commit_dirty,
    current_branch,
    is_dirty,
    is_git_worktree,
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

# Branch-name protection list — operator-mandated. Auto-COMMIT on these
# branches is fine; auto-PUSH is refused; diff-tarball fallback used.
_PROTECTED_BRANCH_PREFIXES: tuple[str, ...] = (
    "main",
    "master",
    "release",
)


def is_protected_branch(branch: str) -> bool:
    """Return True if ``branch`` matches the push-deny list.

    Exact match OR prefix-with-``/`` (``release`` matches ``release``
    and ``release/2.0`` but NOT ``release-notes``). Empty / detached
    (``""``) → True (refuse to push the unclassifiable).
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
    ``pushed``, ``tarball`` (Path or None), ``protected``, ``error``.
    NEVER raises.
    """
    result: dict[str, object] = {
        "path": path,
        "branch": "",
        "committed": False,
        "pushed": False,
        "tarball": None,
        "protected": False,
        "error": "",
    }
    if not is_git_worktree(path):
        result["error"] = "not a git worktree"
        return result
    if not is_dirty(path, timeout=timeout):
        return result  # clean — nothing to rescue
    branch = current_branch(path, timeout=timeout)
    result["branch"] = branch
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
    protected = is_protected_branch(branch)
    result["protected"] = protected
    if protected:
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
