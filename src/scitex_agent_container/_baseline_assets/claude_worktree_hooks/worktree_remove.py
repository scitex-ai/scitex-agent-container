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
root and exits 0 on success. Any failure is loud on stderr + non-zero
so the SDK surfaces it (its built-in default for WorktreeRemove leaves
orphans behind silently; ours fails loud).

Idempotence
-----------
If the worktree is already gone (operator pruned via the daily cron, or
the directory was removed out-of-band), we report success — the desired
end-state is "this worktree is no longer registered", and that's
already true.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _try_git(*args: str, cwd: str) -> tuple[bool, str]:
    res = subprocess.run(
        ["git", "-C", cwd, *args],
        capture_output=True,
        text=True,
    )
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
    raw = sys.stdin.read()
    if not raw.strip():
        print("WorktreeRemove hook: empty stdin (expected JSON)", file=sys.stderr)
        return 2
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"WorktreeRemove hook: stdin is not valid JSON: {exc}", file=sys.stderr)
        return 2

    worktree_path = (payload.get("worktree_path") or "").strip()
    if not worktree_path:
        print("WorktreeRemove hook: 'worktree_path' missing in input", file=sys.stderr)
        return 2

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
            print(
                f"WorktreeRemove hook: 'git worktree remove' failed for "
                f"{worktree_path!r}: {err}; force-remove also failed: {err2}",
                file=sys.stderr,
            )
            return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
