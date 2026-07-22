"""Host-hygiene maintenance rails that run on a schedule, not on a whim.

Two concerns live here:

* **git worktree sprawl** (:mod:`._worktree_gc`), the standing liability
  behind the incident card ``incident-worktree-sprawl-permanent-gc-20260710``
  — one repo reached 105 worktrees and helped trigger a host load-spike.
* **overlay masking of base-baked packages** (:mod:`._overlay_masking`),
  surfaced per-agent in ``sac agents check-health``.

The shape every rail in this package follows, learned from
``_hostsync``:

* **Three-state honest.** A check that could not run reports UNKNOWN and
  the caller KEEPS. "I could not look" must never read as "I looked and
  it was fine".
* **Report by default, mutate only on request.** ``--dry-run`` is the
  default surface; ``--apply`` is the deliberate act.
* **Recording is a side rail.** A failed write prints loudly and
  never crashes the maintenance pass that feeds it.
"""

from ._overlay_masking import (
    base_package_set_for,
    inspect_agent_overlay,
    inspect_overlay,
    sweep_agent_overlays,
)
from ._overlay_masking_model import (
    OPERATIONAL_RULE,
    BasePackageSet,
    OverlayMaskVerdict,
    ShadowInstall,
)
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
    SUBSYSTEM,
    record_gc_results,
)
from ._worktree_gc_repos import discover_repos, spec_workdirs

__all__ = [
    "DEFAULT_CAP",
    "DEFAULT_MIN_AGE_HOURS",
    "OPERATIONAL_RULE",
    "BasePackageSet",
    "GcOutcome",
    "OverlayMaskVerdict",
    "RepoGcResult",
    "ShadowInstall",
    "WorktreeInfo",
    "WorktreeVerdict",
    "base_package_set_for",
    "discover_repos",
    "inspect_agent_overlay",
    "inspect_overlay",
    "sweep_agent_overlays",
    "exit_code_for",
    "gc_repo",
    "gc_repos",
    "gh_pr_merged",
    "list_worktrees",
    "SUBSYSTEM",
    "record_gc_results",
    "running_cwds",
    "spec_workdirs",
]
