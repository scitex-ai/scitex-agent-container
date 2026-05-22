"""Shared drift-status model for sac-drift.

A :class:`DriftStatus` is the result of comparing a local git repo
against its upstream tracking branch. Both the launch-time local check
(:mod:`._local`) and the fleet ssh check (:mod:`._fleet`) speak this
vocabulary so the table renderer and the warning formatter stay shared.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass


class DriftState(enum.Enum):
    """The drift verdict for one spec-source git repo.

    * ``CURRENT`` — local HEAD == upstream; nothing to do.
    * ``BEHIND`` — upstream has commits the local repo lacks. The
      running spec may be stale (an old version of the YAML).
    * ``AHEAD`` — local has unpushed commits. The spec WON'T propagate
      to other hosts until pushed.
    * ``DIVERGED`` — both ahead AND behind. The most dangerous case:
      stale *and* unpushed.
    * ``NOT_A_REPO`` — the spec source isn't inside a git working tree
      (or git is unavailable). Drift is undefined; warn-and-continue.
    * ``UNREACHABLE`` — the remote could not be contacted (offline,
      auth, no upstream configured). Drift is unknown; warn-and-continue.
    """

    CURRENT = "current"
    BEHIND = "behind"
    AHEAD = "ahead"
    DIVERGED = "diverged"
    NOT_A_REPO = "not-a-repo"
    UNREACHABLE = "unreachable"


@dataclass(frozen=True)
class DriftStatus:
    """Outcome of a drift check against one repo's upstream.

    Attributes:
        state: The :class:`DriftState` verdict.
        behind: Commits upstream has that local lacks (0 unless BEHIND/DIVERGED).
        ahead: Local commits not on upstream (0 unless AHEAD/DIVERGED).
        repo: The git working-tree root that was checked (``""`` when NOT_A_REPO).
        upstream: The upstream ref compared against (e.g. ``origin/develop``).
        detail: Human-readable note for NOT_A_REPO / UNREACHABLE causes.
    """

    state: DriftState
    behind: int = 0
    ahead: int = 0
    repo: str = ""
    upstream: str = ""
    detail: str = ""

    @property
    def is_drifted(self) -> bool:
        """True when the repo is behind, ahead, or diverged.

        NOT_A_REPO / UNREACHABLE are NOT drift — drift is *unknown*
        there, not present. Callers that want "anything non-current"
        should test ``state != DriftState.CURRENT`` instead.
        """
        return self.state in (
            DriftState.BEHIND,
            DriftState.AHEAD,
            DriftState.DIVERGED,
        )

    def summary(self) -> str:
        """One-line human summary of the drift verdict."""
        if self.state is DriftState.CURRENT:
            return "current"
        if self.state is DriftState.BEHIND:
            return f"{self.behind} behind {self.upstream}"
        if self.state is DriftState.AHEAD:
            return f"{self.ahead} ahead of {self.upstream} (unpushed)"
        if self.state is DriftState.DIVERGED:
            return (
                f"diverged: {self.ahead} ahead / {self.behind} behind {self.upstream}"
            )
        if self.state is DriftState.NOT_A_REPO:
            return f"not a git repo{(' — ' + self.detail) if self.detail else ''}"
        return f"unreachable{(' — ' + self.detail) if self.detail else ''}"

    def to_dict(self) -> dict:
        """JSON-friendly projection (for ``--json`` surfaces)."""
        return {
            "state": self.state.value,
            "behind": self.behind,
            "ahead": self.ahead,
            "repo": self.repo,
            "upstream": self.upstream,
            "detail": self.detail,
            "summary": self.summary(),
        }


__all__ = ["DriftState", "DriftStatus"]
