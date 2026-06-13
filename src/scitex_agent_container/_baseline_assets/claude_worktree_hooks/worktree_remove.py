#!/usr/bin/env python3
"""Claude Code ``WorktreeRemove`` hook — counterpart to
:mod:`worktree_create`. Removes a worktree previously created by the
relocation hook.

Input (stdin, JSON)::

    {
        "hook_event_name": "WorktreeRemove",
        "worktree_path": "<abs-path-the-create-hook-echoed>",
        "session_id": "<uuid>",
        ...base fields
    }

The hook runs ``git worktree remove <path>`` from a discoverable git
root and exits 0 on success. The SDK's built-in WorktreeRemove default
leaves orphans silently; ours tries harder (force-remove second-pass)
and is loud on stderr when the work cannot be done — but ONLY when the
worktree provably still exists AND we have a working git binary. When
the environment itself is broken (git missing, interpreter drift, the
worktree's repo gone), we WARN+exit 0: the operator's prune cron
catches residual ``.worktrees/`` bloat through the existing F-CS8
audit surface, so wedging the SDK teardown buys nothing.

Idempotence
-----------
If the worktree is already gone (operator pruned via the daily cron, or
the directory was removed out-of-band), we report success — the desired
end-state is "this worktree is no longer registered", and that's
already true.

Graceful degradation (operator priority — fleet-wide WorktreeCreate
hook breakage observed 2026-06-13, lead a2a 07a9187b/777d0a5a):
* The HARD FAILURE that broke the fleet was an interpreter-path drift
  on the CREATE hook; the wiring layer (``settings.local.json``) is
  fixed, but THIS file additionally hardens the remove script body so
  the same class of breakage (git binary missing, transient OSError on
  the subprocess invocation) cannot wedge SDK teardown across the
  fleet. WARN on stderr names the failure; exit 0 lets teardown
  proceed; operator's prune cron still cleans the orphan via the F-CS8
  audit surface.
* Hard exit 2 is reserved for the case we can actually act on but
  ``git worktree remove`` itself refuses — that's a real surface worth
  the operator seeing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _try_git(*args: str, cwd: str) -> tuple[bool, str]:
    """Try ``git -C cwd <args>``; return (ok, stdout-or-stderr).

    Catches the env-drift exception classes so a missing/broken git
    binary degrades to (False, "<reason>") instead of raising into
    main(). The caller picks the policy on a False (idempotent
    success vs. hard-fail) based on whether the orphan it leaves
    behind is one the operator's prune cron will catch.
    """
    try:
        res = subprocess.run(
            ["git", "-C", cwd, *args],
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, OSError) as exc:
        return False, f"git invocation failed ({type(exc).__name__}): {exc}"
    if res.returncode == 0:
        return True, res.stdout.strip()
    return False, (res.stderr or res.stdout).strip()


def _worktree_registered(git_root: str, target: str) -> bool:
    ok, out = _try_git("worktree", "list", "--porcelain", cwd=git_root)
    if not ok:
        return False
    needle = f"worktree {target}"
    return any(line == needle for line in out.splitlines())


def _git_root_for(start: str) -> str | None:
    """Resolve a discoverable git root: prefer the worktree itself, fall
    back to its parent (``.worktrees/`` is inside the main repo)."""
    candidate_dirs: list[str] = []
    if os.path.isdir(start):
        candidate_dirs.append(start)
    parent = str(Path(start).parent)
    if parent and parent != start and os.path.isdir(parent):
        candidate_dirs.append(parent)
    grandparent = str(Path(start).parent.parent)
    if (
        grandparent
        and grandparent not in (start, parent)
        and os.path.isdir(grandparent)
    ):
        candidate_dirs.append(grandparent)
    for cand in candidate_dirs:
        ok, out = _try_git("rev-parse", "--show-toplevel", cwd=cand)
        if ok and out:
            return out
    return None


def main() -> int:
    """Read the hook input from stdin, remove the worktree, exit 0.

    Graceful-degradation order:
      1. If we can locate the git root AND the worktree is registered,
         try ``git worktree remove`` and then ``--force`` — if both
         fail, surface the error (exit 2) so the operator sees it.
      2. If we cannot locate the git root, or the worktree is already
         unregistered, succeed silently (desired end-state achieved).
      3. If the environment itself is broken (git binary missing,
         OSError on subprocess invocation), WARN on stderr but exit 0
         — wedging SDK teardown across the fleet on env drift buys
         nothing the operator's F-CS8 prune cron doesn't already
         catch.
    """
    raw = sys.stdin.read()
    if not raw.strip():
        # Contract violation, but nothing to remove and wedging SDK
        # teardown to flag a parser bug buys nothing. WARN + exit 0.
        print(
            "WorktreeRemove hook: empty stdin (expected JSON); "
            "skipping cleanup (operator cron will catch any orphan).",
            file=sys.stderr,
        )
        return 0
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(
            f"WorktreeRemove hook: stdin is not valid JSON ({exc}); "
            f"skipping cleanup (operator cron will catch any orphan).",
            file=sys.stderr,
        )
        return 0

    worktree_path = (payload.get("worktree_path") or "").strip()
    if not worktree_path:
        print(
            "WorktreeRemove hook: 'worktree_path' missing in input; "
            "skipping cleanup (operator cron will catch any orphan).",
            file=sys.stderr,
        )
        return 0

    git_root = _git_root_for(worktree_path)
    if not git_root:
        # Idempotent: the worktree's containing repo is gone too —
        # nothing to do; succeed (no work, no orphan to clean).
        return 0

    if not _worktree_registered(git_root, worktree_path):
        # Already unregistered — desired end-state achieved.
        return 0

    ok, err = _try_git("worktree", "remove", worktree_path, cwd=git_root)
    if not ok:
        # Try --force as a second pass — the worktree may have local
        # changes the operator's cron didn't trip on. Still better than
        # silent orphan.
        ok2, err2 = _try_git(
            "worktree", "remove", "--force", worktree_path, cwd=git_root
        )
        if not ok2:
            # We located the git root AND the worktree was registered,
            # but git itself refuses to remove it. This is a real
            # surface the operator should see (vs. env-drift WARNs
            # above which are recoverable via prune cron).
            print(
                f"WorktreeRemove hook: 'git worktree remove' failed for "
                f"{worktree_path!r}: {err}; force-remove also failed: {err2}",
                file=sys.stderr,
            )
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
