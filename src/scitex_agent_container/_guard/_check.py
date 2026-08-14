#!/usr/bin/env python3
# File: src/scitex_agent_container/_guard/_check.py

"""``check_deletions`` — resolve a baseline, diff it, return one report.

The policy layer. :mod:`._symbols` says WHAT changed; this module decides
what that MEANS, and the one judgement worth stating out loud is the
broken-file rule: a file that no longer parses is skipped by the diff, so
its symbols were never compared. That is an UNKNOWN, and it renders as
``could-not-determine`` — never as ``clean``.
"""

from __future__ import annotations

from pathlib import Path

from ._report import (
    CLEAN,
    UNDETERMINED,
    VIOLATIONS,
    Deletion,
    DeletionReport,
)
from ._symbols import detect_deletions, symbol_locations
from ._trees import (
    BaselineUnavailable,
    tree_from_dir,
    tree_from_ref,
    tree_from_worktree,
)

__all__ = ["check_deletions"]

_NO_BASELINE = (
    "no baseline was given, so nothing was compared — pass --base <git-ref> "
    "(what the tree looked like before the change) or an explicit "
    "--before <dir> --after <dir> snapshot pair"
)
_HALF_PAIR = (
    "--before and --after must be given together; one half of a snapshot "
    "pair is not a baseline"
)
_AMBIGUOUS = (
    "both --base and --before/--after were given — pass exactly one baseline "
    "so the report says which comparison it actually made"
)


def _undetermined(reason: str, baseline: str, target: str) -> DeletionReport:
    return DeletionReport(
        verdict=UNDETERMINED,
        baseline=baseline,
        target=target,
        undetermined_reason=reason,
        next_steps=(
            "Do NOT read this as clean: no comparison was made.",
            "Fix the baseline and re-run; the guard only guards what it "
            "can see.",
        ),
    )


def _expand_allowed(allowed: frozenset, before_files: dict) -> frozenset:
    """Allowing a CLASS allows the methods that went with it.

    Without this, clearing one intentional class removal means pasting one
    ``--allow`` per method — and a guard nobody can clear in one line is a
    guard people turn off.
    """
    prefixes = tuple(
        f"{key}." for key in allowed if "::class:" in key and not key.endswith(".")
    )
    if not prefixes:
        return allowed
    out = set(allowed)
    for path, source in before_files.items():
        if not path.endswith(".py"):
            continue
        for sym in symbol_locations(source) or {}:
            key = f"{path}::{sym}"
            if key.startswith(prefixes):
                out.add(key)
    return frozenset(out)


def _locate(before_files: dict, key: str) -> Deletion:
    """Turn a ``path::symbol`` key into a Deletion with its baseline lines."""
    path, _, symbol = key.partition("::")
    located = symbol_locations(before_files.get(path, "")) or {}
    first, last = located.get(symbol, (None, None))
    return Deletion(path=path, symbol=symbol, first_line=first, last_line=last)


def _violation_steps(deletions: tuple, deleted_files: tuple) -> tuple:
    steps = []
    if deletions:
        first = deletions[0]
        bare = first.symbol.split(":", 1)[-1].split(".")[-1]
        steps.append(
            f"Restore {first.symbol} in {first.path} (it was at lines "
            f"{first.first_line}-{first.last_line} in the baseline), or "
            f"re-run with --allow '{first.key}' if the task really did "
            "require removing it."
        )
        steps.append(
            f"Find who depends on it before deciding: rg -n '\\b{bare}\\b'"
        )
    if deleted_files:
        steps.append(
            f"{len(deleted_files)} file(s) vanished entirely: "
            f"{', '.join(deleted_files[:3])}"
            + (" ..." if len(deleted_files) > 3 else "")
            + " — restore them, or --allow each path."
        )
    steps.append(
        "A model's own summary is not evidence here: the incident this "
        "guard exists for deleted two classes and never said so."
    )
    return tuple(steps)


def _resolve_trees(repo, base, target, before, after):
    """Return ``(before_files, after_files, baseline_label, target_label)``."""
    if before is not None or after is not None:
        if base is not None:
            raise BaselineUnavailable(_AMBIGUOUS)
        if before is None or after is None:
            raise BaselineUnavailable(_HALF_PAIR)
        before_path, after_path = Path(before), Path(after)
        return (
            tree_from_dir(before_path),
            tree_from_dir(after_path),
            f"snapshot {before_path}",
            f"snapshot {after_path}",
        )
    if base is None:
        raise BaselineUnavailable(_NO_BASELINE)
    repo_path = Path(repo or ".").resolve()
    before_files = tree_from_ref(repo_path, base)
    if target is None:
        return (before_files, tree_from_worktree(repo_path),
                f"git ref {base}", "working tree")
    return (before_files, tree_from_ref(repo_path, target),
            f"git ref {base}", f"git ref {target}")


def check_deletions(*, repo=None, base=None, target=None, before=None,
                    after=None, allowed=()) -> DeletionReport:
    """Compare two trees and report unrequested deletions.

    ``allowed`` holds deletions the task explicitly required — either a
    ``path::symbol`` key or a bare path for a whole file. Everything else
    that vanished is UNREQUESTED, which is the thing being guarded.
    """
    allowed_set = frozenset(allowed)
    baseline_label = f"git ref {base}" if base else str(before or "(none)")
    target_label = str(after or "working tree")
    try:
        before_files, after_files, baseline_label, target_label = _resolve_trees(
            repo, base, target, before, after
        )
    except BaselineUnavailable as exc:
        return _undetermined(exc.reason, baseline_label, target_label)

    allowed_set = _expand_allowed(allowed_set, before_files)
    found = detect_deletions(before_files, after_files, allowed_set)
    deleted_files = tuple(
        p for p in found["deleted_files"] if p not in allowed_set
    )
    allowed_hits = tuple(found["allowed_hits"]) + tuple(
        p for p in found["deleted_files"] if p in allowed_set
    )
    deletions = tuple(_locate(before_files, key) for key in found["deleted"])
    broken = tuple(found["broken_files"])

    if deletions or deleted_files:
        verdict, reason = VIOLATIONS, None
        steps = _violation_steps(deletions, deleted_files)
    elif broken:
        return DeletionReport(
            verdict=UNDETERMINED,
            baseline=baseline_label,
            target=target_label,
            broken_files=broken,
            allowed_deletions=allowed_hits,
            files_compared=len(before_files),
            undetermined_reason=(
                f"{len(broken)} file(s) no longer parse, so every symbol they "
                "exported was skipped by the diff — a deletion could be "
                f"hiding in any of them: {', '.join(broken[:3])}"
            ),
            next_steps=(
                "Fix the syntax error, then re-run — a file that does not "
                "parse cannot be cleared.",
                "Do NOT read this as clean: those files were never compared.",
            ),
        )
    else:
        verdict, reason, steps = CLEAN, None, ()

    return DeletionReport(
        verdict=verdict,
        baseline=baseline_label,
        target=target_label,
        deletions=deletions,
        deleted_files=deleted_files,
        broken_files=broken,
        allowed_deletions=allowed_hits,
        files_compared=len(before_files),
        undetermined_reason=reason,
        next_steps=steps,
    )


# EOF
