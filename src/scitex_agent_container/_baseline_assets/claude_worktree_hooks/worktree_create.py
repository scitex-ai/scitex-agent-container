#!/usr/bin/env python3
"""Claude Code ``WorktreeCreate`` hook — relocates worktree creation to
``<git-root>/.worktrees/<name>`` instead of the SDK's default
``.claude/worktrees/<name>``.

Why
---
Claude Code's bundled binary (Agent SDK) creates session/agent worktrees
under ``.claude/worktrees/<name>`` by default. That directory is never
self-cleaned and accumulates into multi-GB / 100k-file bloat that wedges
agents (F-CS8 recurrence ~100×; wedged neurovista at 22.9 GB / 80k files
→ 0-output turns). The operator already runs a daily prune cron over the
``<root>/.worktrees/`` tree (mtime-gated, never touches ``.claude/``);
relocating creations there hooks the prevention side of the same policy.

How
---
Claude Code v2.x supports a ``WorktreeCreate`` hook in ``settings.json``
that supersedes its built-in worktree creation. The hook reads a JSON
blob from stdin:

    {
        "hook_event_name": "WorktreeCreate",
        "name": "<worktree-name>",
        "cwd": "<dir-the-session-launched-from>",
        "session_id": "<uuid>",
        ...base fields
    }

and MUST do TWO things:

1. Create a fresh git worktree on disk at a real directory.
2. Echo the absolute path of that directory to stdout (single line).

The hook contract is enforced loudly by the SDK — if we echo a path that
isn't a directory, or echo nothing, Claude Code rejects with
``WorktreeCreate hook returned a path that is not a directory`` /
``hook succeeded but returned no worktree path``. The deobfuscated
binary's ``VRH`` (executeWorktreeCreateHook) function is the contract
source-of-truth verified 2026-06-06 against the
``claude_agent_sdk._bundled.claude`` binary shipped with this venv.

Policy
------
* **Target dir**: ``<git-root>/.worktrees/<name>`` — under ``.worktrees``,
  NOT ``.claude/worktrees``. The operator's cron already maintains hygiene
  on this tree.
* **Branch ref**: ``origin/develop`` when fetchable, ``HEAD`` otherwise.
  Mirrors Claude's own "fresh" default (clean tree from the trunk).
* **Branch name**: ``claude/<name>`` — namespaced so a bulk
  ``git branch -D 'claude/*'`` reaps them cleanly.
* **Idempotence**: if the target worktree path already exists in
  ``git worktree list``, we just re-emit its path (no double-create).
* **Existing branch**: if ``claude/<name>`` already exists, we attach the
  worktree to it (worktree add without ``-b``). Lets the operator/agent
  resume a worktree across restarts without manual fixup.

Failure mode
------------
Every failure exits non-zero with a diagnostic on stderr. The SDK
surfaces stderr in its ``WorktreeCreate hook failed`` error so the
operator sees what went wrong instead of a silent fall-through to the
hardcoded default.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path


def _run_git(*args: str, cwd: str) -> str:
    """Run ``git -C cwd <args>``; return stdout stripped. Raises CalledProcessError on non-zero exit."""
    res = subprocess.run(
        ["git", "-C", cwd, *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return res.stdout.strip()


def _try_git(*args: str, cwd: str) -> tuple[bool, str]:
    """Try ``git -C cwd <args>``; return (ok, stdout-or-stderr). No raise."""
    res = subprocess.run(
        ["git", "-C", cwd, *args],
        capture_output=True,
        text=True,
    )
    if res.returncode == 0:
        return True, res.stdout.strip()
    return False, (res.stderr or res.stdout).strip()


def _worktree_already_exists(git_root: str, target: Path) -> bool:
    """``git worktree list --porcelain`` includes a ``worktree <abs-path>`` line per worktree."""
    out = _run_git("worktree", "list", "--porcelain", cwd=git_root)
    needle = f"worktree {target}"
    return any(line == needle for line in out.splitlines())


def _branch_exists(git_root: str, branch: str) -> bool:
    ok, _ = _try_git(
        "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", cwd=git_root
    )
    return ok


def _resolve_base(git_root: str) -> str:
    """Branch source policy: ``origin/develop`` when present, else HEAD."""
    if _branch_exists(git_root, "develop"):
        # Local develop fast-path — usually already up-to-date with origin.
        pass
    ok, _ = _try_git("rev-parse", "--verify", "--quiet", "origin/develop", cwd=git_root)
    if ok:
        return "origin/develop"
    return "HEAD"


def main() -> int:
    """Read the hook input from stdin, create the worktree, echo its path.

    Returns the process exit code (0 on success; non-zero with stderr
    diagnostic on any failure, so the SDK shows the real reason).
    """
    raw = sys.stdin.read()
    if not raw.strip():
        print("WorktreeCreate hook: empty stdin (expected JSON)", file=sys.stderr)
        return 2
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        print(f"WorktreeCreate hook: stdin is not valid JSON: {exc}", file=sys.stderr)
        return 2

    name = (payload.get("name") or "").strip()
    cwd = (payload.get("cwd") or "").strip()
    if not name:
        print("WorktreeCreate hook: 'name' missing in input", file=sys.stderr)
        return 2
    if not cwd or not os.path.isdir(cwd):
        print(
            f"WorktreeCreate hook: 'cwd' missing or not a dir: {cwd!r}", file=sys.stderr
        )
        return 2

    # ``name`` arrives from Claude Code's session bookkeeping; we treat
    # it as a path segment and reject any traversal/separator nasties
    # so the hook can never write outside ``<git-root>/.worktrees/``.
    if "/" in name or ".." in name.split("."):
        print(
            f"WorktreeCreate hook: invalid 'name' (path-traversal): {name!r}",
            file=sys.stderr,
        )
        return 2

    try:
        git_root = _run_git("rev-parse", "--show-toplevel", cwd=cwd)
    except subprocess.CalledProcessError as exc:
        print(
            f"WorktreeCreate hook: {cwd!r} is not in a git repository: {exc.stderr.strip()}",
            file=sys.stderr,
        )
        return 2
    if not git_root:
        print(
            f"WorktreeCreate hook: 'git rev-parse' returned empty toplevel for {cwd!r}",
            file=sys.stderr,
        )
        return 2

    target = Path(git_root) / ".worktrees" / name
    branch = f"claude/{name}"

    # Pre-create the .worktrees container so `git worktree add` has
    # somewhere to land. Idempotent.
    target.parent.mkdir(parents=True, exist_ok=True)

    if _worktree_already_exists(git_root, target):
        # Resume case — operator/agent restart picked the same name.
        print(str(target))
        return 0

    base = _resolve_base(git_root)

    if _branch_exists(git_root, branch):
        # Branch lingered from a previous worktree; attach without -b so
        # the existing branch stays the source of truth.
        ok, err = _try_git("worktree", "add", str(target), branch, cwd=git_root)
    else:
        ok, err = _try_git(
            "worktree", "add", "-b", branch, str(target), base, cwd=git_root
        )

    if not ok:
        print(
            f"WorktreeCreate hook: 'git worktree add' failed for "
            f"name={name!r} target={target} branch={branch} base={base}: {err}",
            file=sys.stderr,
        )
        return 2

    if not target.is_dir():
        # SDK enforces this — fail loud rather than letting the SDK's
        # generic "not a directory" error swallow our diagnostic.
        print(
            f"WorktreeCreate hook: target {target} not a directory after "
            "'git worktree add' (filesystem race or post-create deletion)",
            file=sys.stderr,
        )
        return 2

    print(str(target))
    return 0


if __name__ == "__main__":
    sys.exit(main())
