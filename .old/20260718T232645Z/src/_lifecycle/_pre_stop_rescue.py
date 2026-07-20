"""Fleet-default pre-stop rescue — commit dirty worktrees LOCALLY before stop.

Operator priority (lead a2a ``efa48850daf248ed9fe3ae5232677b2b``): make
restart cheap. Before this module, ``sac agents stop`` would happily
SIGTERM the apptainer process with uncommitted work in the agent's
worktrees still sitting there — on the next start the work was gone.
Operator-declared ``pre_stop`` hooks could rescue it, but no fleet
default shipped, so every agent had to opt in by spec edit (the
band-aid the operator explicitly dislikes).

*** THE RESCUE NEVER PUBLISHES. ***

Operator ruling, 2026-07-17 (「プッシュはなしじゃない？」, after
「やりかけで落ちてしまったなら仕方ないじゃん…潔くやり直すのも良い」):
this module saves work LOCALLY and stops there. It does not push, and
there is no push primitive in ``_pre_stop_rescue_git`` to reach for.

Why the push had to go — it published on the agent's behalf, at the one
moment the agent could not review the result:

* It ran NO tests. ``git add -A`` → commit → push, with no concept of
  whether the code so much as imports. That is how red code reached
  origin (operator: 「テストが赤いまま公開？どうやって？」).
* It used ``--force-with-lease``, which every agent is hook-banned from
  running by hand. A stop hook is not an exemption.
* It protected BRANCHES, not TREES, and the walk had no ownership
  model: a foreign worktree parked on a topic branch (another agent's
  in-flight work, in a checkout this agent merely shares) got committed
  and force-pushed with no guard anywhere in its path. Observed
  2026-07-17: a stopping agent force-pushed a peer's ``feat/`` branch.

Nothing is lost by dropping it. ``workdir`` is a host bind mount, so a
LOCAL commit already survives the restart that motivated this module —
the push was never what made the work durable.

*** THE RESCUE ONLY TOUCHES WORKTREES IT OWNS. ***

The push is gone (#743), but a residual harm survived it: on a SHARED
checkout the LOCAL commit still mis-attributed a peer's tree. The
``scitex-cards`` lane runs four agents (``scitex-cards`` / ``-chat`` /
``-gui`` / ``-mobile``) over ONE physical checkout — the workdir is a
symlink farm onto a single ``scitex-todo`` checkout — so its ``.git``,
``.worktrees/`` and ``git worktree list`` are SHARED. The walk therefore
saw every peer's worktree and, since worktrees are branch-named with no
agent id, could not tell them apart. Observed twice 2026-07-17: agent
``chat``'s rescue committed agent ``gui``'s in-flight worktree under
``chat``'s identity.

The fix is an OWNERSHIP MARKER + DEFAULT-DENY. Each subagent worktree is
stamped at creation (the ``WorktreeCreate`` hook) with its creating
agent's id, stored OUT of the working tree at ``<git-dir>/sac-owner`` so
the ``git add -A`` this module runs can NEVER stage it. The walk reads
that stamp (:func:`_pre_stop_rescue_git.worktree_owner`) and rescues a
``.worktrees`` child ONLY when the stamp names the stopping agent —
skipping any child whose owner MISMATCHES or is ABSENT
(:func:`_ownership_allows`). The unstamped-window cost is bounded: the
commit-before-idle stop hook already forces dirty subagent worktrees to
commit, so this rescue is the SECOND net, mattering mainly for CRASHES.

It runs once per ``agent_stop`` call, BEFORE the operator's
``pre_stop`` hooks fire, walks every git worktree under the agent's
work roots, and for each DIRTY worktree:

* On a NON-protected topic branch: commits the dirty changes locally
  with a stable rescue message.
* On a PROTECTED branch (``develop`` / ``main`` / ``master`` /
  ``release/*``): carries the dirty tree onto a dedicated
  ``rescue/<agent>-<utc>`` side-branch, commits it THERE, and
  returns the checkout to the protected branch with a clean tree — so
  the protected branch NEVER gains a local-only commit that would
  diverge it from origin and break ``git pull --ff-only`` + the
  deploy-freshness cron (the root cause this module was hardened to
  prevent, fleet-wide 2026-07).
* On COMMIT failure: writes a diff-tarball under
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
    worktree_owner,
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
    """Run the rescue sequence on a single git worktree. Never pushes.

    Returns a dict with: ``path``, ``branch``, ``committed``,
    ``tarball`` (Path or None), ``protected``, ``rescue_branch`` (str;
    the ``rescue/`` side-branch used on a protected branch, else
    ``""``), ``error``. NEVER raises.

    On a NON-protected topic branch the dirty tree is committed in
    place. On a PROTECTED branch (``develop`` / ``main`` / ``master`` /
    ``release/*``) the dirty tree is instead carried onto a fresh
    ``rescue/<agent>-<ts>`` side-branch, committed there, and the
    checkout is returned to the protected branch with a clean tree — so
    the protected branch NEVER gains a local-only commit and stays
    ``git pull --ff-only`` friendly.

    The result deliberately has NO ``pushed`` key: the rescue does not
    publish (see the module docstring). A permanently-``False`` field
    would read as a capability that merely happens to be off.
    """
    result: dict[str, object] = {
        "path": path,
        "branch": "",
        "committed": False,
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
    # --- non-protected topic branch: commit in place, LOCAL ONLY ------
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

    Carries the tree onto a ``rescue/<agent>-<ts>`` side-branch and
    commits it there — LOCALLY; the side-branch is never pushed — then
    returns the checkout to ``branch`` so the protected ref is unchanged
    from origin (``ff-only`` pull still works). Falls back to the
    diff-tarball if the side-branch can't be created or committed — the
    dirty work is never dropped. The protected branch is restored on
    EVERY path.
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
    # The side-branch stays LOCAL — it is a save, not a publication.
    result["committed"] = True
    # ALWAYS return to the protected branch so its ref equals origin's and
    # ``git pull --ff-only`` keeps working.
    ok_back, back_err = checkout_branch(path, branch, timeout=timeout)
    if not ok_back:
        prefix = f"{result['error']}; " if result["error"] else ""
        result["error"] = f"{prefix}restore to {branch} failed: {back_err}"
    return result


def _candidate_roots(workdir: Path) -> Iterable[tuple[Path, bool]]:
    """Yield ``(path, is_child)`` for every directory the pass walks.

    Per lead refinement (a2a efa48850): cover the subagent
    ``.worktrees/*`` and the legacy ``worktrees/*`` — those are
    exactly what restart kills + what the reap-bug
    (lead-learnings/19) silently destroys.

    ``is_child`` is ``True`` for a ``.worktrees/*`` / ``worktrees/*``
    subagent worktree and ``False`` for the primary ``workdir`` root.
    The distinction drives the OWNERSHIP gate in
    ``rescue_worktrees_for_agent``: children are DEFAULT-DENY (rescued
    only when their ``sac-owner`` stamp names the stopping agent), while
    the shared-checkout root keeps its existing conservative handling.
    """
    yield workdir, False
    sub = workdir / ".worktrees"
    if sub.is_dir():
        for child in sorted(sub.iterdir()):
            if child.is_dir():
                yield child, True
    legacy = workdir / "worktrees"
    if legacy.is_dir():
        for child in sorted(legacy.iterdir()):
            if child.is_dir():
                yield child, True


def _ownership_allows(owner: str | None, agent_name: str, *, is_child: bool) -> bool:
    """Decide whether the stopping ``agent_name`` may rescue this candidate.

    THREE states, never a boolean collapse of "I don't know" into a pole:

    * ``owner == agent_name`` — positively owned → ALLOW (both children
      and root).
    * ``owner`` present but ``!= agent_name`` — a DIFFERENT agent owns
      this worktree → DENY (both). This is the exact mis-attribution the
      fix exists to stop: a peer's in-flight tree must never be committed
      under the stopping agent's identity.
    * ``owner is None`` (marker ABSENT / unknown) — DEFAULT-DENY for a
      ``.worktrees`` CHILD (an unstamped peer worktree in the shared
      checkout is indistinguishable from our own, so we refuse it), but
      ALLOW for the primary ROOT. The root is the agent's own workdir;
      denying an unstamped root would REGRESS the ordinary
      single-checkout agent whose root-level work must still be rescued.
      The shared-checkout root is protected a different way: it sits on
      ``develop`` (protected), so ``rescue_worktree`` routes it to a
      local ``rescue/`` side-branch rather than committing peer work onto
      the shared branch.
    """
    if owner == agent_name:
        return True
    if owner is None:
        return not is_child
    return False


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
    for candidate, is_child in _candidate_roots(workdir):
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
                    "tarball": None,
                    "protected": False,
                    "rescue_branch": "",
                    "error": "grace budget elapsed",
                }
            )
            continue
        remaining = deadline - time.monotonic()
        candidate_timeout = min(per_candidate_timeout, remaining)
        # OWNERSHIP GATE (shared-checkout mis-attribution fix). Read the
        # worktree's ``sac-owner`` stamp and DEFAULT-DENY a child whose
        # owner is not the stopping agent (mismatch) OR is absent
        # (unknown) — a stopping agent must never commit a peer's
        # in-flight worktree under its own identity. The primary root is
        # handled conservatively inside ``_ownership_allows`` (an
        # unstamped own-checkout root is still rescued; a shared-checkout
        # root on ``develop`` is routed to a local side-branch by
        # ``rescue_worktree``).
        owner = worktree_owner(candidate, timeout=candidate_timeout)
        if not _ownership_allows(owner, agent_name, is_child=is_child):
            log.info(
                "pre-stop rescue: skipping %s — owner=%r != stopping agent=%r "
                "(default-deny; not this agent's worktree)",
                candidate,
                owner,
                agent_name,
            )
            results.append(
                {
                    "path": candidate,
                    "branch": "",
                    "committed": False,
                    "tarball": None,
                    "protected": False,
                    "rescue_branch": "",
                    "error": (
                        f"skipped: owner={owner!r} != stopping agent="
                        f"{agent_name!r} (ownership default-deny)"
                    ),
                    "owner": owner,
                }
            )
            continue
        results.append(
            rescue_worktree(
                candidate,
                agent_name=agent_name,
                timestamp=timestamp,
                rescue_root=rescue_root,
                timeout=candidate_timeout,
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
    tarballed = sum(1 for r in results if r.get("tarball"))
    skipped = sum(1 for r in results if r.get("error"))
    log.info(
        "pre-stop rescue: agent=%s committed=%d tarballed=%d skipped=%d "
        "(local only — the rescue never pushes)",
        getattr(config, "name", "?"),
        committed,
        tarballed,
        skipped,
    )
