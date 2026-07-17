"""OBSERVATION for the worktree GC — everything that touches the world.

Isolated from the decision logic (:mod:`._worktree_gc_predicate`) on
purpose: every function here can fail for reasons that have nothing to do
with the worktree (git missing, gh unauthenticated, ``/proc`` unreadable),
and each one converts that failure into an honest UNKNOWN rather than a
convenient boolean. The predicate then only has to know how to keep on an
unknown — it never has to guess whether an answer is real.

The two seams the GC injects in tests live here as the DEFAULTS:
:func:`gh_pr_merged` (the merged-PR lookup — no network in tests) and
:func:`running_cwds` (the ``/proc`` scan — no ambient processes in tests).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Callable

from ._worktree_gc_model import WorktreeInfo

__all__ = [
    "CwdScan",
    "PrLookup",
    "gh_pr_merged",
    "list_worktrees",
    "run_git",
    "running_cwds",
]

#: (repo, branch) -> True merged / False not merged / None unknown.
PrLookup = Callable[[Path, str], "bool | None"]
#: () -> set of cwds of running processes, or None when unobservable.
CwdScan = Callable[[], "set[Path] | None"]


def run_git(
    cwd: str | Path, *args: str, timeout: int = 60, merge_stderr: bool = False
) -> tuple[bool, str]:
    """``git -C <cwd> <args>`` -> (ok, stdout-or-stderr). Never raises.

    Env drift (no git binary, an unreadable cwd, a hung git) degrades to
    ``(False, "<reason>")`` so every caller can route it to an UNKNOWN
    verdict instead of an exception — an unreadable repo must be a KEEP,
    not a crash in a scheduled pass.

    ``merge_stderr`` folds stderr into the SUCCESS output. Needed because
    some git subcommands report on stderr even when they succeed:
    ``worktree prune --verbose`` writes "Removing worktrees/x: ..." there,
    so a stdout-only read silently returns "" and the pass claims a prune
    it cannot evidence. (Measured, not assumed — git 2.43.)
    """
    # stx-allow: fallback (reason: a missing/hung git must degrade to an UNKNOWN leg -> KEEP, never a traceback out of a scheduled maintenance pass)
    try:
        res = subprocess.run(
            ["git", "-C", str(cwd), *args],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError) as exc:
        return False, f"git invocation failed ({type(exc).__name__}): {exc}"
    if res.returncode == 0:
        if merge_stderr:
            return True, "\n".join(
                part for part in (res.stdout.strip(), res.stderr.strip()) if part
            )
        return True, res.stdout.strip()
    return False, (res.stderr or res.stdout).strip()


def list_worktrees(repo: str | Path) -> tuple[bool, list[WorktreeInfo], str]:
    """Enumerate ``repo``'s worktrees -> (ok, infos, error).

    Reads ``git worktree list --porcelain``, which is authoritative: it
    reports every worktree registered to the repo wherever it lives on
    disk, so the GC never has to guess at directory layouts
    (``.worktrees/`` vs ``.claude/worktrees/`` vs wherever an agent put
    one). Directory-scanning would miss exactly the worktrees nobody
    expected — which are the ones that sprawl.

    The FIRST record is git's main worktree — the repo checkout itself.
    It is flagged :attr:`WorktreeInfo.is_main` and is never a GC
    candidate. ``ok=False`` means the path is not a readable git repo;
    the caller reports UNKNOWN rather than assuming "no worktrees".
    """
    ok, out = run_git(repo, "worktree", "list", "--porcelain")
    if not ok:
        return False, [], out
    infos: list[WorktreeInfo] = []
    fields: dict = {}

    def _flush() -> None:
        if not fields.get("path"):
            return
        infos.append(
            WorktreeInfo(
                path=fields["path"],
                head=fields.get("head", ""),
                branch=fields.get("branch", ""),
                is_main=not infos,  # the first record is the main worktree
                is_bare=fields.get("bare", False),
                is_locked=fields.get("locked", False),
                is_prunable=fields.get("prunable", False),
            )
        )
        fields.clear()

    for raw in out.splitlines():
        line = raw.rstrip()
        if not line:
            _flush()
            continue
        key, _, value = line.partition(" ")
        if key == "worktree":
            _flush()
            fields["path"] = value
        elif key == "HEAD":
            fields["head"] = value
        elif key == "branch":
            fields["branch"] = value.replace("refs/heads/", "", 1)
        elif key in ("bare", "detached", "locked", "prunable"):
            fields[key] = True
    _flush()
    return True, infos, ""


def running_cwds() -> set[Path] | None:
    """Cwds of every readable running process, or ``None`` if unknowable.

    BEST-EFFORT, and honestly so. This reads ``/proc/<pid>/cwd``, which
    means it can UNDER-report:

    * a process whose cwd we cannot read (another user's, or a race with
      its exit) is simply absent from the set;
    * only this PID namespace is visible — a container that bind-mounts
      the worktree and chdirs into it from its OWN namespace is invisible;
    * a process holding an open FILE in the worktree without chdir'ing
      there is invisible too.

    Because it can under-report, it is ONE leg of four rather than the
    whole predicate. A worktree that passes the other three and merely
    looks idle is by construction clean, merged, and a day old — removing
    it costs a re-checkout, not work.

    ``None`` (no ``/proc``, or nothing readable at all) means the signal
    is UNAVAILABLE, and every caller turns that into a KEEP. It never
    silently reads as "nothing is running".
    """
    proc = Path("/proc")
    if not proc.is_dir():
        return None
    # stx-allow: fallback (reason: /proc iteration racing process exit must degrade to an honest None, never crash a scheduled pass)
    try:
        entries = list(proc.iterdir())
    except OSError:
        return None
    found: set[Path] = set()
    readable = False
    for entry in entries:
        if not entry.name.isdigit():
            continue
        try:
            target = (entry / "cwd").resolve()
        except (OSError, RuntimeError):
            # Unreadable (not ours) or vanished mid-scan. Skip it — a scan
            # that reads SOMETHING is still a usable signal.
            continue
        readable = True
        found.add(target)
    return found if readable else None


def gh_pr_merged(repo: Path, branch: str) -> bool | None:
    """Does ``branch`` have a MERGED PR? True / False / None (unknown).

    The squash-merge half of the merged leg. A squash-merged branch is not
    an ancestor of its base, so ``rev-list`` calls it unmerged forever;
    GitHub still knows the truth, and this asks it. Without this, a repo
    that squash-merges would never have a single worktree GC'd.

    Returns ``None`` — never ``False`` — on ANY doubt: gh missing, not
    authenticated, offline, not a GitHub remote, rate-limited, or any
    other non-zero exit. That distinction is the point: ``False`` means
    "GitHub answered: no merged PR exists" and lets the GC conclude
    UNMERGED (paired with the ancestor check); ``None`` means "nobody
    answered", which keeps the worktree.
    """
    if not branch:
        return None  # detached HEAD — there is no branch to ask about
    # stx-allow: fallback (reason: gh absent/unauthenticated/offline must read as UNKNOWN -> KEEP; it must never crash the GC nor masquerade as "no merged PR")
    try:
        res = subprocess.run(
            [
                "gh",
                "pr",
                "list",
                "--head",
                branch,
                "--state",
                "merged",
                "--json",
                "number",
                "--limit",
                "1",
            ],
            cwd=str(repo),
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (FileNotFoundError, OSError, subprocess.SubprocessError):
        return None
    if res.returncode != 0:
        return None
    try:
        payload = json.loads(res.stdout or "[]")
    except json.JSONDecodeError:
        return None
    if not isinstance(payload, list):
        return None
    return bool(payload)
