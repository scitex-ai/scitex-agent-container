"""``sac agents prune-claude`` — purge F-CS8 bloat sources from a workdir.

The fleet sweep on 2026-06-03 found two distinct leaks that drive
``<workdir>/.claude/`` past the silent-MCP-spawn-failure threshold:

1. ``hooks/pre-tool-use/.pending/toolu_*.json`` — pipe-stage permission
   records written by ``pipe-stage-permissions.sh`` per gated tool-use,
   never cleaned up. Two observed agents had > 5 k records (the worst
   was 7,957 / 32 MB on proj-scitex-agent-container, since May 5).

2. ``.claude/worktrees/agent-*`` — subagent worktrees. Each is a full
   git checkout, so cleanup is git-aware: skip locked worktrees, skip
   any whose branch HEAD is NOT yet merged into either ``develop`` or
   ``main`` (operator's safety bar from the 2026-06-03 cutover review).

This command is a deterministic, idempotent, dry-run-by-default prune.
Default mode prints what *would* be pruned without touching disk. Pass
``--apply`` to actually move (NEVER ``rm -rf``) candidates to a sibling
``.claude-<bucket>-parked-<date>/`` directory next to the workdir's
``.claude/``. Operators can then ``rm -rf`` the parked dir at their
leisure once they've verified nothing live depends on the contents.

The "park, don't delete" rule mirrors the manual triage we used on
orochi + proj-scitex-agent-container; nothing in the prune path
forecloses operator review.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import click

# Default age threshold for pending-record prune. Records are timestamped
# by filesystem mtime; entries older than this are considered stale.
_DEFAULT_PENDING_AGE_DAYS = 7

# Subdirs we know how to prune. ``.pending`` is the easy file-by-file
# prune; ``worktrees`` is git-aware (locked + merged check).
_PENDING_REL = "hooks/pre-tool-use/.pending"
_WORKTREES_REL = "worktrees"

# Branches whose ancestry counts as "safe to drop" for the worktrees
# prune. Mirroring the operator review rule for the 2026-06-03 cutover.
_SAFE_BASE_REFS: tuple[str, ...] = ("origin/develop", "origin/main")


# ---------------------------------------------------------------------------
# Result shape
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PrunePlanEntry:
    """A single candidate for pruning."""

    kind: str  # "pending-record" | "worktree"
    path: str
    reason: str
    files: int = 0
    bytes: int = 0


@dataclass(frozen=True)
class PruneSkipEntry:
    """A candidate that the planner deliberately SKIPPED — recorded so
    the dry-run output explains every decision."""

    kind: str
    path: str
    reason: str


@dataclass(frozen=True)
class PrunePlan:
    """Result of the planner stage; ``apply`` reads this verbatim."""

    workdir: str
    pending: tuple[PrunePlanEntry, ...]
    worktrees: tuple[PrunePlanEntry, ...]
    skipped: tuple[PruneSkipEntry, ...]


# ---------------------------------------------------------------------------
# Planner
# ---------------------------------------------------------------------------


def _now() -> float:
    return time.time()


def _file_age_seconds(path: Path) -> float | None:
    """Return file mtime age in seconds; ``None`` if stat fails."""
    # stx-allow: fallback (reason: stat may fail on broken symlinks or
    # permission-denied entries; planner skips them rather than aborts)
    try:
        return _now() - path.stat().st_mtime
    except OSError:  # stx-allow: fallback (reason: see inline comment)
        return None


def _plan_pending(
    workdir: Path, age_days: int
) -> tuple[list[PrunePlanEntry], list[PruneSkipEntry]]:
    """Plan the ``.pending/`` file prune. Bucket-by-bucket file prune;
    everything older than ``age_days`` becomes a candidate."""
    pending_dir = workdir / ".claude" / _PENDING_REL
    if not pending_dir.is_dir():
        return [], []
    cutoff = age_days * 86400
    candidates: list[PrunePlanEntry] = []
    skipped: list[PruneSkipEntry] = []
    for entry in pending_dir.iterdir():
        if not entry.is_file() or entry.is_symlink():
            skipped.append(
                PruneSkipEntry(
                    kind="pending-record",
                    path=str(entry),
                    reason="not a regular file (skipped)",
                )
            )
            continue
        age = _file_age_seconds(entry)
        if age is None:
            skipped.append(
                PruneSkipEntry(
                    kind="pending-record",
                    path=str(entry),
                    reason="stat() failed",
                )
            )
            continue
        if age < cutoff:
            continue  # too young — leave alone, not a skip-warning
        size = 0
        # stx-allow: fallback (reason: post-stat race where the file
        # disappears mid-walk; treat as 0 bytes rather than abort)
        try:
            size = entry.stat().st_size
        except OSError:  # stx-allow: fallback (reason: see inline comment)
            pass
        candidates.append(
            PrunePlanEntry(
                kind="pending-record",
                path=str(entry),
                reason=f"mtime {age / 86400:.1f}d old (threshold {age_days}d)",
                files=1,
                bytes=size,
            )
        )
    return candidates, skipped


def _run_git(args: list[str], cwd: Path) -> str | None:
    """Best-effort `git` invocation; ``None`` on any failure.

    Used in read-only `git worktree list --porcelain` / `git merge-base`
    queries below — the planner never mutates a repo.
    """
    # stx-allow: fallback (reason: git may be absent, the repo may be
    # corrupt, or the worktree may have been moved; planner records a
    # skip rather than aborts)
    try:
        result = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
    except (
        OSError,
        subprocess.TimeoutExpired,
    ):  # stx-allow: fallback (reason: see inline comment)
        return None
    if result.returncode != 0:
        return None
    return result.stdout


def _branch_merged_into(repo: Path, branch: str, base: str) -> bool:
    """Return True iff ``branch``'s tip is an ancestor of ``base``."""
    out = _run_git(["merge-base", "--is-ancestor", branch, base], repo)
    # `git merge-base --is-ancestor` returns 0 (success → "is ancestor")
    # or 1 (not ancestor). _run_git returns None on rc != 0 so a True
    # response here is exactly "is ancestor".
    return out is not None


def _list_worktrees(repo: Path) -> list[dict]:
    """Parse `git worktree list --porcelain`. Empty on any failure."""
    raw = _run_git(["worktree", "list", "--porcelain"], repo)
    if raw is None:
        return []
    out: list[dict] = []
    cur: dict = {}
    for line in raw.splitlines():
        if not line.strip():
            if cur:
                out.append(cur)
                cur = {}
            continue
        if line.startswith("worktree "):
            cur = {"worktree": line[len("worktree ") :]}
        elif line.startswith("branch "):
            cur["branch"] = line[len("branch ") :]
        elif line == "locked":
            cur["locked"] = True
        elif line.startswith("locked "):
            cur["locked"] = True
            cur["lock_reason"] = line[len("locked ") :]
    if cur:
        out.append(cur)
    return out


def _plan_worktrees(
    workdir: Path,
) -> tuple[list[PrunePlanEntry], list[PruneSkipEntry]]:
    """Plan the ``.claude/worktrees/`` git-aware prune.

    A worktree becomes a candidate iff:
      * its path is under ``<workdir>/.claude/worktrees/agent-*``,
      * it is NOT marked ``locked`` in `git worktree list --porcelain`,
      * its branch's HEAD is an ancestor of EITHER ``origin/develop``
        or ``origin/main`` — i.e. the work is upstream-merged.

    Anything else gets a skip entry with the reason so operators can
    audit the planner's decisions.
    """
    worktrees_root = workdir / ".claude" / _WORKTREES_REL
    if not worktrees_root.is_dir():
        return [], []

    repo = workdir  # the workdir IS the main checkout
    listed = _list_worktrees(repo)
    if not listed:
        # Git unavailable or worktree list empty — skip safely; we
        # never delete worktrees blind.
        return [], [
            PruneSkipEntry(
                kind="worktree",
                path=str(worktrees_root),
                reason="git worktree list unavailable",
            )
        ]

    by_path = {entry["worktree"]: entry for entry in listed if "worktree" in entry}

    candidates: list[PrunePlanEntry] = []
    skipped: list[PruneSkipEntry] = []
    for child in worktrees_root.iterdir():
        if not child.is_dir():
            continue
        if not child.name.startswith("agent-"):
            skipped.append(
                PruneSkipEntry(
                    kind="worktree",
                    path=str(child),
                    reason=("not an agent-* worktree dir; refusing to prune"),
                )
            )
            continue
        entry = by_path.get(str(child))
        if entry is None:
            skipped.append(
                PruneSkipEntry(
                    kind="worktree",
                    path=str(child),
                    reason=(
                        "no matching `git worktree list` entry — "
                        "stale registry; manual cleanup required"
                    ),
                )
            )
            continue
        if entry.get("locked"):
            skipped.append(
                PruneSkipEntry(
                    kind="worktree",
                    path=str(child),
                    reason="git-locked (intentional preservation)",
                )
            )
            continue
        branch = entry.get("branch", "").lstrip("refs/heads/")
        if not branch:
            skipped.append(
                PruneSkipEntry(
                    kind="worktree",
                    path=str(child),
                    reason="detached HEAD (no branch to check merge state)",
                )
            )
            continue
        merged = any(
            _branch_merged_into(repo, branch, base) for base in _SAFE_BASE_REFS
        )
        if not merged:
            skipped.append(
                PruneSkipEntry(
                    kind="worktree",
                    path=str(child),
                    reason=(
                        f"branch {branch} not merged into "
                        + " OR ".join(_SAFE_BASE_REFS)
                    ),
                )
            )
            continue
        size = 0
        files = 0
        # stx-allow: fallback (reason: best-effort sizing; failure does
        # not change the prune decision, just the reported metrics)
        try:
            for path in child.rglob("*"):
                if path.is_file() and not path.is_symlink():
                    files += 1
                    size += path.stat().st_size
        except OSError:  # stx-allow: fallback (reason: see inline comment)
            pass
        candidates.append(
            PrunePlanEntry(
                kind="worktree",
                path=str(child),
                reason=f"merged into one of {_SAFE_BASE_REFS}",
                files=files,
                bytes=size,
            )
        )
    return candidates, skipped


def plan_prune(workdir: str | Path, *, pending_age_days: int) -> PrunePlan:
    """Compute the prune plan for ``workdir``. Pure function (no writes).

    Caller can render this for dry-run output and / or pass to
    :func:`apply_plan` to execute. Plans are deterministic given the
    same filesystem + git state.
    """
    wd = Path(workdir)
    pending, p_skip = _plan_pending(wd, pending_age_days)
    worktrees, w_skip = _plan_worktrees(wd)
    return PrunePlan(
        workdir=str(wd),
        pending=tuple(pending),
        worktrees=tuple(worktrees),
        skipped=tuple([*p_skip, *w_skip]),
    )


# ---------------------------------------------------------------------------
# Apply
# ---------------------------------------------------------------------------


def _parked_root(workdir: Path, bucket: str) -> Path:
    """Return the sibling parked-dir path (NOT inside .claude/)."""
    from datetime import date

    return workdir / f".claude-{bucket}-parked-{date.today().isoformat()}"


def apply_plan(plan: PrunePlan) -> dict:
    """Move every plan entry to a sibling parked dir; return summary.

    Each candidate is moved into ``<workdir>/.claude-<bucket>-parked-
    <YYYY-MM-DD>/`` (created if needed). Move (not delete) so the
    operator retains the data for verification before final ``rm -rf``.

    Returns a dict with counts + the parked-dir paths so the CLI can
    report freed space and where to find the moved data.
    """
    wd = Path(plan.workdir)
    moved = {"pending": 0, "worktrees": 0}
    parked_paths: list[str] = []

    if plan.pending:
        dest = _parked_root(wd, "pending")
        dest.mkdir(parents=True, exist_ok=True)
        parked_paths.append(str(dest))
        for entry in plan.pending:
            src = Path(entry.path)
            # stx-allow: fallback (reason: post-plan race where the file
            # vanished mid-apply; just decrement and move on)
            try:
                target = dest / src.name
                shutil.move(str(src), str(target))
                moved["pending"] += 1
            except (
                OSError,
                shutil.Error,
            ):  # stx-allow: fallback (reason: see inline comment)
                continue

    if plan.worktrees:
        dest = _parked_root(wd, "worktrees")
        dest.mkdir(parents=True, exist_ok=True)
        parked_paths.append(str(dest))
        for entry in plan.worktrees:
            src = Path(entry.path)
            # stx-allow: fallback (reason: see _plan_pending comment)
            try:
                target = dest / src.name
                shutil.move(str(src), str(target))
                moved["worktrees"] += 1
            except (
                OSError,
                shutil.Error,
            ):  # stx-allow: fallback (reason: see inline comment)
                continue

    return {
        "moved": moved,
        "parked_paths": parked_paths,
    }


# ---------------------------------------------------------------------------
# CLI command
# ---------------------------------------------------------------------------


def _resolve_agent_workdir(name: str) -> str | None:
    """Look up an agent's workdir via the registry → config path."""
    # stx-allow: fallback (reason: the agent registry may be empty in
    # CI / fresh installs; the CLI surfaces a clear error instead of
    # exploding)
    try:
        from .._state.registry import Registry
        from ..config import load_config
    except ImportError:  # stx-allow: fallback (reason: see inline comment)
        return None
    reg = Registry()
    entry = reg.get(name)
    if not entry:
        return None
    cfg_path = entry.get("config")
    if not cfg_path:
        return None
    # stx-allow: fallback (reason: malformed/missing spec.yaml; CLI
    # reports the error rather than abort)
    try:
        cfg = load_config(cfg_path)
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        return None
    return getattr(cfg, "expanded_workdir", None) or getattr(cfg, "workdir", None)


@click.command(name="prune-claude")
@click.argument("name", required=True)
@click.option(
    "--apply",
    "apply_flag",
    is_flag=True,
    default=False,
    help=(
        "Actually move the planned candidates to a sibling parked-dir. "
        "Default is dry-run: print the plan and exit 0 without touching "
        "any files. PARK semantics — nothing is deleted; the operator "
        "decides when to `rm -rf` the parked-dir."
    ),
)
@click.option(
    "--pending-age-days",
    "pending_age_days",
    default=_DEFAULT_PENDING_AGE_DAYS,
    show_default=True,
    type=int,
    help=(
        "Prune `.pending/toolu_*.json` records older than this many days. "
        "Records younger than the threshold are left alone."
    ),
)
@click.option(
    "--json",
    "as_json",
    is_flag=True,
    default=False,
    help="Emit the plan as JSON (machine-parseable).",
)
def prune_claude(
    name: str, apply_flag: bool, pending_age_days: int, as_json: bool
) -> None:
    """Prune F-CS8 bloat sources from an agent's workdir/.claude/ tree.

    Targets two known accumulating leaks:

    \b
      - hooks/pre-tool-use/.pending/toolu_*.json  (>= --pending-age-days)
      - worktrees/agent-*                          (merged + unlocked only)

    Default is dry-run. Pass --apply to execute. Park-not-delete:
    nothing is removed; matched entries are moved to a sibling parked-dir
    named .claude-<bucket>-parked-<YYYY-MM-DD>/.

    \b
    Examples:
      $ sac agents prune-claude proj-scitex-orochi             # dry-run
      $ sac agents prune-claude proj-scitex-orochi --apply
      $ sac agents prune-claude proj-grant --pending-age-days 14 --json
    """
    workdir = _resolve_agent_workdir(name)
    if not workdir:
        click.echo(f"unknown agent or missing workdir: {name}", err=True)
        raise SystemExit(2)

    plan = plan_prune(workdir, pending_age_days=pending_age_days)

    if as_json:
        click.echo(
            json.dumps(
                {
                    "workdir": plan.workdir,
                    "dry_run": not apply_flag,
                    "pending": [
                        {
                            "kind": e.kind,
                            "path": e.path,
                            "reason": e.reason,
                            "files": e.files,
                            "bytes": e.bytes,
                        }
                        for e in plan.pending
                    ],
                    "worktrees": [
                        {
                            "kind": e.kind,
                            "path": e.path,
                            "reason": e.reason,
                            "files": e.files,
                            "bytes": e.bytes,
                        }
                        for e in plan.worktrees
                    ],
                    "skipped": [
                        {"kind": s.kind, "path": s.path, "reason": s.reason}
                        for s in plan.skipped
                    ],
                    **(apply_plan(plan) if apply_flag else {}),
                },
                indent=2,
            )
        )
        return

    # Human-readable rendering.
    click.echo(f"workdir: {plan.workdir}")
    if plan.pending:
        click.echo(f"\n.pending/ records to prune ({len(plan.pending)}):")
        for entry in plan.pending[:5]:
            click.echo(f"  - {entry.path}  ({entry.reason})")
        if len(plan.pending) > 5:
            click.echo(f"  ... and {len(plan.pending) - 5} more")
    if plan.worktrees:
        click.echo(f"\nworktrees/agent-* to prune ({len(plan.worktrees)}):")
        for entry in plan.worktrees:
            click.echo(
                f"  - {entry.path}  "
                f"({entry.files} files, {entry.bytes / (1024 * 1024):.1f} MB; "
                f"{entry.reason})"
            )
    if plan.skipped:
        click.echo(f"\nSKIPPED ({len(plan.skipped)}):")
        for s in plan.skipped[:5]:
            click.echo(f"  - {s.path}  ({s.reason})")
        if len(plan.skipped) > 5:
            click.echo(f"  ... and {len(plan.skipped) - 5} more")
    if not (plan.pending or plan.worktrees):
        click.echo("\nNo prune candidates found.")
        return

    if not apply_flag:
        click.echo("\n(dry-run — pass --apply to execute the prune)")
        return

    result = apply_plan(plan)
    click.echo(
        f"\nApplied. Moved {result['moved']['pending']} pending records, "
        f"{result['moved']['worktrees']} worktrees to:"
    )
    for path in result["parked_paths"]:
        click.echo(f"  - {path}")
    click.echo("\nReview, then `rm -rf <parked-dir>` once verified clean.")


__all__ = [
    "PrunePlan",
    "PrunePlanEntry",
    "PruneSkipEntry",
    "apply_plan",
    "plan_prune",
    "prune_claude",
]
