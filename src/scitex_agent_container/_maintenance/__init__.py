"""Host-hygiene maintenance rails that run on a schedule, not on a whim.

Three concerns live here:

* **git worktree sprawl** (:mod:`._worktree_gc`), the standing liability
  behind the incident card ``incident-worktree-sprawl-permanent-gc-20260710``
  — one repo reached 105 worktrees and helped trigger a host load-spike.
* **overlay masking of base-baked packages** (:mod:`._overlay_masking`),
  surfaced per-agent in ``sac agents check-health``.
* **install integrity** (:mod:`._install_integrity_probe`), surfaced as
  ``sac installation check`` — dead/shadowed editable pointers, orphaned
  and duplicated dist-info. Twice now (2026-07-16, 2026-08-09) an agent
  has executed code from a deleted or abandoned tree while ``--version``
  reported a healthy number; only path-level inspection exposes it.

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

from ._install_integrity_model import (
    IMPORTS_LIVE,
    IMPORTS_UNAVAILABLE,
    REASON_DEAD_POINTER,
    REASON_DUPLICATE_DIST_INFO,
    REASON_ORPHANED_DIST_INFO,
    REASON_RESOLVES_OUTSIDE,
    REASON_SHADOWED_POINTER,
    STATE_BROKEN,
    STATE_OK,
    STATE_UNKNOWN,
    DistributionEvidence,
    DistributionVerdict,
    EditablePointer,
    InstallIntegrityReport,
    SiteEvidence,
)
from ._install_integrity_predicate import (
    build_report,
    classify_distribution,
)
from ._install_integrity_predicate import (
    exit_code_for as install_integrity_exit_code,
)
from ._install_integrity_probe import (
    inspect_install,
    read_site_evidence,
    resolve_site_packages,
)
from ._layers_migration_gate import (
    ArmingGateVerdict,
    ArmingSnapshot,
    fleet_arming_snapshot,
    gate_arming,
)
from ._layers_migration_plan import (
    already_declared,
    plan_migration,
    resolved_layer_names,
)
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
from ._overlay_venv_invalidate import (
    reconcile_overlay_venv,
    reconcile_overlay_venv_for_launch,
    sif_identity,
)
from ._overlay_venv_model import (
    ACTION_INVALIDATE,
    ACTION_NONE,
    ACTION_REFUSE,
    InvalidationPlan,
    OverlayVenvFacts,
    VenvCheck,
)
from ._overlay_venv_predicate import plan_invalidation
from ._venv_dist_assertion import (
    VenvDistributionError,
    assert_venv_distributions_unique,
    duplicate_distributions,
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
    "ACTION_INVALIDATE",
    "ACTION_NONE",
    "ACTION_REFUSE",
    "DEFAULT_CAP",
    "DEFAULT_MIN_AGE_HOURS",
    "InvalidationPlan",
    "OverlayVenvFacts",
    "VenvCheck",
    "VenvDistributionError",
    "assert_venv_distributions_unique",
    "duplicate_distributions",
    "plan_invalidation",
    "reconcile_overlay_venv",
    "reconcile_overlay_venv_for_launch",
    "sif_identity",
    "IMPORTS_LIVE",
    "IMPORTS_UNAVAILABLE",
    "OPERATIONAL_RULE",
    "REASON_DEAD_POINTER",
    "REASON_DUPLICATE_DIST_INFO",
    "REASON_ORPHANED_DIST_INFO",
    "REASON_RESOLVES_OUTSIDE",
    "REASON_SHADOWED_POINTER",
    "STATE_BROKEN",
    "STATE_OK",
    "STATE_UNKNOWN",
    "ArmingGateVerdict",
    "ArmingSnapshot",
    "BasePackageSet",
    "DistributionEvidence",
    "DistributionVerdict",
    "EditablePointer",
    "GcOutcome",
    "InstallIntegrityReport",
    "SiteEvidence",
    "build_report",
    "classify_distribution",
    "inspect_install",
    "install_integrity_exit_code",
    "read_site_evidence",
    "resolve_site_packages",
    "already_declared",
    "fleet_arming_snapshot",
    "gate_arming",
    "plan_migration",
    "resolved_layer_names",
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
