#!/usr/bin/env python3
# File: src/scitex_agent_container/_guard/_report.py

"""The guard's ANSWER SHAPE — one dataclass, three verdicts, declared codes.

Constitution §2, "answer in a fixed, declared shape": an ad-hoc return is
how "I could not tell" silently becomes "yes". So the guard always returns
:class:`DeletionReport`, never a bool, tuple, or shape-shifting dict.

Three verdicts, never two
=========================
``clean``               we compared both trees and nothing vanished.
``violations``          we can NAME what vanished.
``could-not-determine`` we could not compare — no baseline, not a git
                        repo, an unreadable ref, or a file that no longer
                        parses (its symbols are invisible to the diff).

The third one is the entire value of the guard. Collapsing it into
``clean`` hands a green light to the exact case where nothing was checked;
collapsing it into ``violations`` cries wolf and gets the guard disabled.
:meth:`DeletionReport.__post_init__` is the mechanical barrier — a report
that says ``clean`` while carrying findings, or ``could-not-determine``
without a stated reason, CANNOT BE CONSTRUCTED.

Exit codes are declared, not improvised
=======================================
``1`` and ``2`` already mean "generic failure" and "usage error" in every
CLI framework, so a renamed verb or a bad flag would impersonate a domain
answer. The domain codes therefore start at 3.
"""

from __future__ import annotations

from dataclasses import dataclass, field

__all__ = [
    "CLEAN",
    "EXIT_CLEAN",
    "EXIT_UNDETERMINED",
    "EXIT_VIOLATIONS",
    "UNDETERMINED",
    "VERDICTS",
    "VIOLATIONS",
    "Deletion",
    "DeletionReport",
]

CLEAN = "clean"
VIOLATIONS = "violations"
UNDETERMINED = "could-not-determine"
VERDICTS = (CLEAN, VIOLATIONS, UNDETERMINED)

EXIT_CLEAN = 0
EXIT_VIOLATIONS = 3
EXIT_UNDETERMINED = 4

_EXIT_FOR = {
    CLEAN: EXIT_CLEAN,
    VIOLATIONS: EXIT_VIOLATIONS,
    UNDETERMINED: EXIT_UNDETERMINED,
}


@dataclass(frozen=True)
class Deletion:
    """One symbol that existed in the baseline and does not exist now."""

    path: str
    symbol: str
    first_line: int | None = None
    last_line: int | None = None

    @property
    def key(self) -> str:
        """The ``path::symbol`` form — also the ``--allow`` token."""
        return f"{self.path}::{self.symbol}"

    @property
    def where(self) -> str:
        if self.first_line is None:
            return self.path
        if self.last_line and self.last_line != self.first_line:
            return f"{self.path}:{self.first_line}-{self.last_line}"
        return f"{self.path}:{self.first_line}"

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "symbol": self.symbol,
            "first_line": self.first_line,
            "last_line": self.last_line,
            "key": self.key,
        }


@dataclass(frozen=True)
class DeletionReport:
    """The guard's whole answer. Validated at construction."""

    verdict: str
    baseline: str
    target: str
    deletions: tuple[Deletion, ...] = ()
    deleted_files: tuple[str, ...] = ()
    broken_files: tuple[str, ...] = ()
    allowed_deletions: tuple[str, ...] = ()
    files_compared: int = 0
    undetermined_reason: str | None = None
    next_steps: tuple[str, ...] = field(default=())

    def __post_init__(self) -> None:
        if self.verdict not in VERDICTS:
            raise ValueError(
                f"verdict {self.verdict!r} is not one of {VERDICTS}"
            )
        found = bool(self.deletions or self.deleted_files)
        if self.verdict == VIOLATIONS and not found:
            raise ValueError(
                "verdict 'violations' with nothing to name — a guard must "
                "say WHAT was deleted, not merely that something was"
            )
        if self.verdict == CLEAN and found:
            raise ValueError(
                "verdict 'clean' carrying deletions — this is the exact "
                "collapse the three-valued verdict exists to prevent"
            )
        if self.verdict == CLEAN and self.broken_files:
            raise ValueError(
                "verdict 'clean' with unparsable files — their symbols were "
                "never compared, so 'clean' would be a claim we cannot make; "
                "use 'could-not-determine'"
            )
        if self.verdict == CLEAN and self.undetermined_reason:
            raise ValueError(
                "verdict 'clean' carrying an undetermined_reason"
            )
        if self.verdict == UNDETERMINED:
            if not (self.undetermined_reason or "").strip():
                raise ValueError(
                    "verdict 'could-not-determine' must state WHY — an "
                    "unexplained unknown is indistinguishable from a bug"
                )
            if found:
                raise ValueError(
                    "verdict 'could-not-determine' carrying named deletions "
                    "— name them and return 'violations' instead"
                )

    @property
    def exit_code(self) -> int:
        """Declared numeric code: 0 clean / 3 violations / 4 undetermined."""
        return _EXIT_FOR[self.verdict]

    def to_dict(self) -> dict:
        """Stable JSON shape. Keys never disappear; values may be empty."""
        return {
            "verdict": self.verdict,
            "exit_code": self.exit_code,
            "baseline": self.baseline,
            "target": self.target,
            "files_compared": self.files_compared,
            "deletions": [d.to_dict() for d in self.deletions],
            "deleted_files": list(self.deleted_files),
            "broken_files": list(self.broken_files),
            "allowed_deletions": list(self.allowed_deletions),
            "undetermined_reason": self.undetermined_reason,
            "next_steps": list(self.next_steps),
        }


# EOF
