"""Host-hygiene maintenance rails that run on a schedule, not on a whim.

Today one concern lives here: **git worktree sprawl**
(:mod:`._worktree_gc`), the standing liability behind the incident card
``incident-worktree-sprawl-permanent-gc-20260710`` — one repo reached 105
worktrees and helped trigger a host load-spike.

The shape every rail in this package follows, learned from
``_hostsync``:

* **Three-state honest.** A check that could not run reports UNKNOWN and
  the caller KEEPS. "I could not look" must never read as "I looked and
  it was fine".
* **Report by default, mutate only on request.** ``--dry-run`` is the
  default surface; ``--apply`` is the deliberate act.
* **The board is a side rail.** A card-delivery failure prints loudly and
  never crashes the maintenance pass that feeds it.
"""

from ._worktree_gc import (
    DEFAULT_CAP,
    DEFAULT_MIN_AGE_HOURS,
    GcOutcome,
    RepoGcResult,
    WorktreeInfo,
    WorktreeVerdict,
    exit_code_for,
    gc_repo,
    gc_repos,
    gh_pr_merged,
    list_worktrees,
    running_cwds,
)
from ._worktree_gc_alarm import (
    CARD_ID_PREFIX,
    AlarmOutcome,
    card_id_for,
    route_gc_to_cards,
)
from ._worktree_gc_repos import discover_repos, spec_workdirs

__all__ = [
    "CARD_ID_PREFIX",
    "DEFAULT_CAP",
    "DEFAULT_MIN_AGE_HOURS",
    "AlarmOutcome",
    "GcOutcome",
    "RepoGcResult",
    "WorktreeInfo",
    "WorktreeVerdict",
    "card_id_for",
    "discover_repos",
    "exit_code_for",
    "gc_repo",
    "gc_repos",
    "gh_pr_merged",
    "list_worktrees",
    "route_gc_to_cards",
    "running_cwds",
    "spec_workdirs",
]
