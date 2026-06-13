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

Graceful degradation (operator priority — fleet-wide WorktreeCreate hook
breakage observed 2026-06-13, lead a2a 07a9187b/777d0a5a):
* The HARD FAILURE that broke the fleet was an interpreter-path drift —
  the dotfiles JSON wired ``/opt/venv-agent/bin/python3`` which was
  absent in some SIFs. That has been fixed at the wiring layer (PATH-
  based ``python3``). THIS file additionally hardens the SCRIPT
  itself: when the .worktrees/<name> policy CANNOT be honoured (the
  cwd isn't a git repo, git is missing, the directory is read-only, …),
  the hook falls back to the SDK's own default location
  ``<cwd>/.claude/worktrees/<name>``, creates a real git worktree there,
  and echoes that path. Operator sees a WARN on stderr explaining the
  policy was skipped; the Agent SPAWN PROCEEDS. The operator's prune
  cron eventually catches the .claude/worktrees/ bloat through the
  existing F-CS8 audit surface.
* Only when BOTH the policy AND the fallback fail do we exit 2 with a
  diagnostic — there is no plausible recovery beyond that.
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


def _try_policy_target(name: str, cwd: str) -> str | None:
    """Try the ``<git-root>/.worktrees/<name>`` relocation policy.

    Returns the absolute path on success; ``None`` on any failure
    (graceful-degradation entry point — the caller falls back to the
    SDK default location on ``None``).
    """
    try:
        git_root = _run_git("rev-parse", "--show-toplevel", cwd=cwd)
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None
    if not git_root:
        return None

    target = Path(git_root) / ".worktrees" / name
    branch = f"claude/{name}"

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None

    try:
        if _worktree_already_exists(git_root, target):
            return str(target)
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None

    base = _resolve_base(git_root)

    if _branch_exists(git_root, branch):
        ok, _err = _try_git(
            "worktree", "add", str(target), branch, cwd=git_root
        )
    else:
        ok, _err = _try_git(
            "worktree", "add", "-b", branch, str(target), base, cwd=git_root
        )

    if not ok or not target.is_dir():
        return None
    return str(target)


def _try_sdk_default_fallback(name: str, cwd: str) -> str | None:
    """Create a worktree at the SDK's default ``<cwd>/.claude/worktrees/<name>``.

    Used when ``_try_policy_target`` fails so the Agent spawn proceeds
    instead of hard-failing. NO bookkeeping for the prune cron — the
    operator's F-CS8 audit surface still picks the .claude/worktrees
    bloat up via the existing daily sweep.
    """
    target = Path(cwd).resolve() / ".claude" / "worktrees" / name
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        return None
    # Use the same git-worktree-add machinery so the dir is a real
    # worktree the SDK can drive. Discover the git root again here
    # so the fallback doesn't require the policy walk to have succeeded.
    try:
        git_root = _run_git("rev-parse", "--show-toplevel", cwd=cwd)
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None
    if not git_root:
        return None
    branch = f"claude/{name}"
    if _branch_exists(git_root, branch):
        ok, _err = _try_git(
            "worktree", "add", str(target), branch, cwd=git_root
        )
    else:
        base = _resolve_base(git_root)
        ok, _err = _try_git(
            "worktree", "add", "-b", branch, str(target), base, cwd=git_root
        )
    if not ok or not target.is_dir():
        return None
    return str(target)


def main() -> int:
    """Read the hook input from stdin, create the worktree, echo its path.

    Graceful-degradation order:
      1. Try the ``.worktrees/<name>`` policy target.
      2. On failure, fall back to the SDK's ``.claude/worktrees/<name>``
         default (WARN on stderr; the Agent spawn still proceeds).
      3. Only exit 2 when BOTH paths fail — no plausible recovery
         beyond that, so surface the SDK's "hook failed" error.
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
    # so the hook can never write outside the worktree roots.
    if "/" in name or ".." in name.split("."):
        print(
            f"WorktreeCreate hook: invalid 'name' (path-traversal): {name!r}",
            file=sys.stderr,
        )
        return 2

    policy_path = _try_policy_target(name, cwd)
    if policy_path is not None:
        print(policy_path)
        return 0

    # Policy failed — fall back to SDK default so the Agent spawn still
    # proceeds. Operator sees a single-line WARN naming the fallback so
    # the prune cron is the implicit cleanup contract.
    fallback_path = _try_sdk_default_fallback(name, cwd)
    if fallback_path is not None:
        print(
            f"WorktreeCreate hook: policy target .worktrees/{name} failed; "
            f"falling back to SDK default {fallback_path} so the Agent "
            f"spawn proceeds (operator F-CS8 audit + prune cron applies).",
            file=sys.stderr,
        )
        print(fallback_path)
        return 0

    print(
        f"WorktreeCreate hook: BOTH policy target .worktrees/{name} AND "
        f"SDK fallback .claude/worktrees/{name} failed; no plausible "
        f"recovery. cwd={cwd!r}. Check git availability + write "
        f"permissions on both candidate paths.",
        file=sys.stderr,
    )
    return 2


if __name__ == "__main__":
    sys.exit(main())
