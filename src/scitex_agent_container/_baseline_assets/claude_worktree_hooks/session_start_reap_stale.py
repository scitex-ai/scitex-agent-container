#!/usr/bin/env python3
"""Claude Code ``SessionStart`` hook — reap the agent's own STALE
``.worktrees/<name>`` and ``.claude/worktrees/<name>`` entries BEFORE
the SDK's bloat-scan runs.

Why
---
PR #93 (``feat(_state): worktree-reap safety predicate``) made
host-side ``git worktree prune`` SAFE via the ``is_safe_to_reap``
predicate but did NOT make it AUTOMATIC. Operator observed
(2026-05-24, card ``sac-stale-worktree-autoreap-enable``): without
auto-reap, agent-local worktree bloat re-accumulates over each 24h
window and re-triggers the F-CS8 0-token freeze. A manual tool does
not prevent recurrence.

This hook closes that gap on the CHEAPEST + SAFEST side: it runs at
``SessionStart`` (i.e. BEFORE Claude Code's first bloat-scan inside
the session) and reaps the agent's own worktree directories whose
mtime is older than the age-threshold AND whose
:func:`is_safe_to_reap` predicate returns True.

Safety asymmetry (matches the predicate's doctrine):
* **Age-gated**: only entries older than ``--age-hours`` (default 24h)
  are considered. A worktree the agent created in the last 24h is
  presumed in-flight, never touched.
* **Predicate-gated**: ``is_safe_to_reap`` requires a clean tree AND
  HEAD not ahead of ``develop``. Any failure of the predicate → SKIP.
* **Fail-open**: any error reaping ONE worktree is logged + skipped;
  the hook NEVER exits non-zero on a reap miss. Wedging
  ``SessionStart`` would brick the entire SDK session — far worse than
  leaving a stale dir on disk (the operator's prune cron still
  catches that on the next pass).
* **Agent-only scope**: scans only the canonical agent-worktree roots
  (``<git-root>/.worktrees/`` and ``$HOME/.claude/worktrees/``).
  Never walks operator-owned trees.

Hook contract
-------------
Claude Code v2.x ``SessionStart`` hook input (stdin, JSON):

    {
      "hook_event_name": "SessionStart",
      "source": "startup" | "resume" | "clear" | "compact",
      "cwd": "<dir-the-session-launched-from>",
      "session_id": "<uuid>",
      ...base fields
    }

Output: a single ``reaped=N skipped=M roots=...`` line on stderr for
operator visibility; stdout is left empty (the SDK does not consume
it for SessionStart). Exit 0 unconditionally (fail-open).
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from pathlib import Path

# Default age threshold. The operator's card explicitly calls out the
# 24h window — anything younger is in-flight and must not be touched.
DEFAULT_AGE_HOURS = 24

# Canonical agent-worktree roots. The two locations the agent itself
# writes to:
#   * ``<git-root>/.worktrees/<name>`` — the relocation policy in
#     :mod:`worktree_create` (preferred).
#   * ``$HOME/.claude/worktrees/<name>`` — the SDK's default location
#     (graceful-degradation fallback when the policy target fails).
# Both are agent-owned; both are in-scope for the auto-reaper.
_CANONICAL_ROOT_RELS_FROM_GIT_ROOT = (".worktrees",)
_CANONICAL_ROOT_RELS_FROM_HOME = (".claude/worktrees",)


def _run_git(*args: str, cwd: str) -> tuple[bool, str]:
    """Run ``git -C cwd <args>``; return (ok, stdout-or-stderr).

    Never raises — env-drift (missing git binary, OSError on subprocess
    spawn) degrades to ``(False, "<reason>")`` so the caller can decide
    whether to skip silently or surface.
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


def _is_safe_to_reap(worktree_path: Path) -> bool:
    """Mirror of :func:`scitex_agent_container._state.worktree_safety.is_safe_to_reap`.

    Re-implemented inline so the hook script has ZERO scitex import
    dependency at runtime — the hook runs from ``$HOME/.claude/hooks/``
    where the sac package may or may not be on ``sys.path`` depending
    on which venv launched Claude. The semantics are identical:

    Returns True iff ALL of:
      * ``worktree_path/.git`` exists (it really IS a worktree).
      * ``git -C <path> status --porcelain`` is empty (clean tree).
      * ``git -C <path> rev-list develop..HEAD`` is empty (not ahead).

    Any error or ambiguity → False (default-False = annoying-but-safe,
    NEVER destructive — matches lead-learnings/19 doctrine).
    """
    if not (worktree_path / ".git").exists():
        return False
    ok_status, status_out = _run_git("status", "--porcelain", cwd=str(worktree_path))
    if not ok_status:
        return False
    if status_out.strip():
        return False
    ok_ahead, ahead_out = _run_git("rev-list", "develop..HEAD", cwd=str(worktree_path))
    if not ok_ahead:
        return False
    if ahead_out.strip():
        return False
    return True


def _candidate_roots(cwd: str) -> list[Path]:
    """Discover the canonical agent-worktree roots to scan.

    Two sources contribute candidate roots:
      1. The git root discovered from ``cwd`` (if any) — adds
         ``<git-root>/.worktrees/`` per :data:`_CANONICAL_ROOT_RELS_FROM_GIT_ROOT`.
      2. ``$HOME`` — adds ``.claude/worktrees`` per
         :data:`_CANONICAL_ROOT_RELS_FROM_HOME`.

    Missing roots (no git context, no ``$HOME``) just don't contribute —
    silent skip, no error. De-duplicated so a symlinked layout doesn't
    double-scan.
    """
    roots: list[Path] = []
    seen: set[Path] = set()
    git_ok, git_out = _run_git("rev-parse", "--show-toplevel", cwd=cwd)
    if git_ok and git_out:
        for rel in _CANONICAL_ROOT_RELS_FROM_GIT_ROOT:
            candidate = Path(git_out) / rel
            resolved = candidate.resolve() if candidate.exists() else candidate
            if resolved not in seen:
                seen.add(resolved)
                roots.append(candidate)
    home = os.environ.get("HOME", "").strip()
    if home:
        for rel in _CANONICAL_ROOT_RELS_FROM_HOME:
            candidate = Path(home) / rel
            resolved = candidate.resolve() if candidate.exists() else candidate
            if resolved not in seen:
                seen.add(resolved)
                roots.append(candidate)
    return roots


def _stale_children(root: Path, age_hours: int, now: float) -> list[Path]:
    """List immediate subdirectories of ``root`` whose mtime is older
    than ``age_hours``.

    Only walks one level deep — agent-worktree roots are flat:
    ``<root>/<name>/``. Anything nested deeper is either a regular
    worktree's git-internals (whose mtimes the predicate already gates
    on) or operator-managed content we shouldn't touch.

    Missing root → empty list (no-op). Permission errors → empty list +
    no raise (fail-open).
    """
    threshold = now - (age_hours * 3600)
    if not root.is_dir():
        return []
    try:
        children = list(root.iterdir())
    except OSError:
        # stx-allow: fallback (reason: permission/IO error on root listing
        # must NOT wedge SessionStart — degrade to "nothing to reap here")
        return []
    out: list[Path] = []
    for child in children:
        if not child.is_dir():
            continue
        try:
            mtime = child.stat().st_mtime
        except OSError:
            # stx-allow: fallback (reason: stat failure on one child must
            # not block the rest of the scan; skip and continue)
            continue
        if mtime <= threshold:
            out.append(child)
    return out


def _reap_one(worktree_path: Path) -> tuple[bool, str]:
    """Attempt to reap ONE worktree.

    Order of operations:
      1. Predicate gate: :func:`_is_safe_to_reap` MUST return True.
      2. Resolve the worktree's containing git root (the worktree
         itself works — ``git -C <worktree>`` resolves to the main repo
         transparently because ``.git`` is a gitlink).
      3. ``git worktree remove <path>`` — clean teardown.
      4. On failure, ``git worktree remove --force <path>`` — second
         pass for residual locks/marker files.

    Returns ``(True, "")`` on successful reap, ``(False, "<reason>")``
    on any skip. The reason string is for the operator-visible stderr
    summary, not for retry logic.
    """
    if not _is_safe_to_reap(worktree_path):
        return False, "predicate-gated"
    # Resolve git root from the worktree itself (its ``.git`` gitlink
    # points back at the main repo); fall back to the parent (typical
    # ``<git-root>/.worktrees/<name>`` layout) if the worktree's gitlink
    # is broken.
    candidates = [str(worktree_path), str(worktree_path.parent)]
    git_root = ""
    for cand in candidates:
        ok, out = _run_git("rev-parse", "--show-toplevel", cwd=cand)
        if ok and out:
            git_root = out
            break
    if not git_root:
        return False, "no-git-root"
    ok, _ = _run_git("worktree", "remove", str(worktree_path), cwd=git_root)
    if ok:
        return True, ""
    ok2, err2 = _run_git(
        "worktree", "remove", "--force", str(worktree_path), cwd=git_root
    )
    if ok2:
        return True, ""
    return False, f"git-refused: {err2}"


def _summarize(reaped: int, skipped: int, roots: list[Path]) -> str:
    """One-line operator-visible summary written to stderr."""
    roots_repr = ",".join(str(r) for r in roots) or "<none>"
    return (
        f"SessionStart auto-reap: reaped={reaped} skipped={skipped} roots={roots_repr}"
    )


def main(argv: list[str] | None = None) -> int:
    """Read SessionStart payload from stdin; reap stale agent worktrees.

    Fail-open across the board: any error → log to stderr, exit 0.
    Wedging ``SessionStart`` would brick the SDK session, which is far
    worse than leaving a stale worktree dir on disk (the operator's
    prune cron picks it up on next pass).

    CLI args (parsed off ``argv``, defaults to ``sys.argv[1:]``):
      ``--age-hours N``  override the 24h threshold (tests inject).
      ``--now-epoch S``  override ``time.time()`` (tests inject).
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--age-hours", type=int, default=DEFAULT_AGE_HOURS)
    parser.add_argument("--now-epoch", type=float, default=None)
    args, _ = parser.parse_known_args(argv if argv is not None else sys.argv[1:])

    now = args.now_epoch if args.now_epoch is not None else time.time()

    raw = sys.stdin.read() if not sys.stdin.isatty() else ""
    cwd = ""
    if raw.strip():
        try:
            payload = json.loads(raw)
            cwd = (payload.get("cwd") or "").strip()
        except json.JSONDecodeError:
            # stx-allow: fallback (reason: malformed stdin must not wedge
            # SessionStart; degrade to cwd=$PWD discovery below)
            cwd = ""
    if not cwd or not os.path.isdir(cwd):
        cwd = os.getcwd()

    roots = _candidate_roots(cwd)
    reaped = 0
    skipped = 0
    for root in roots:
        for child in _stale_children(root, args.age_hours, now):
            ok, _reason = _reap_one(child)
            if ok:
                reaped += 1
            else:
                skipped += 1

    print(_summarize(reaped, skipped, roots), file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
