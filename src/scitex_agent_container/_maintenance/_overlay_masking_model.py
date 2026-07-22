"""Data + vocabulary for overlay-masking detection. No subprocess, no I/O.

Companion to :mod:`._overlay_masking` (the scanner + verdict engine),
split model-from-engine exactly like ``_worktree_gc_model`` /
``_worktree_gc_predicate``.

THE INCIDENT THIS VOCABULARY EXISTS FOR (2026-07-22)
----------------------------------------------------
Agent containers run a base SIF whose venv is ``/opt/venv-sac``. Each agent
also mounts a writable directory overlay whose upper layer lives at::

    ~/.scitex/agent-container/containers/overlays/<agent>/upper/

Historical per-agent ``pip install``s left stale copies of ``scitex_cards``
(0.16.0 … 0.17.4) in those upper layers. Overlayfs resolves the UPPER layer
first, so a plain restart onto a rebuilt base did NOT migrate the package —
the fossil kept winning, silently, forever. Every downstream symptom
followed (resolve-store hitting a bundled example, coin-toss dist-info,
cards never reaching the canonical board), plus a no-op restart wave and a
premature all-clear that had to be retracted.

Nothing in sac installs packages into overlay uppers today — the ``.def``
bakes installs into ``/opt/venv-sac`` in the BASE, correctly. The fossils
were hand-upgrades. A hand-cleanup restores the state but leaves the system
able to re-enter it silently; the DETECTOR is the deliverable.

The verdict / reason strings below are API, not debug text: they land in
``sac agents check-health --json`` (and through it in the MCP
``agent_health`` tool).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .._drift.versions import DEFAULT_VENV

__all__ = [
    "OPERATIONAL_RULE",
    "REASON_BASE_UNKNOWN",
    "REASON_IMAGE_OVERLAY",
    "REASON_INSPECT_ERROR",
    "REASON_MASKED",
    "REASON_NO_OVERLAY",
    "REASON_NO_SHADOW_INSTALLS",
    "REASON_OVERLAY_MISSING",
    "REASON_OVERLAY_ONLY",
    "REASON_UPPER_UNREADABLE",
    "REASON_UPPER_VENV_UNTOUCHED",
    "SHADOW_BASE_UNKNOWN",
    "SHADOW_MASKED",
    "SHADOW_OVERLAY_ONLY",
    "VERDICT_CLEAN",
    "VERDICT_MASKED",
    "VERDICT_UNKNOWN",
    "BasePackageSet",
    "OverlayMaskVerdict",
    "ShadowInstall",
    "canonical_dist_name",
]

#: THE OPERATIONAL RULE, as ONE string, so the health output and any
#: card/report quote the same words instead of paraphrasing drift into
#: being. Documented here because the detector is the enforcement.
OPERATIONAL_RULE = (
    "NEVER pip-install a base-baked package into a running agent's overlay: "
    "the overlay upper masks the base venv from then on, and every later "
    "base rebuild is silently shadowed for that agent. Hotfix EITHER by "
    "force-reinstalling the EXACT base version, OR by recreating the "
    "overlay (stop the agent, remove <overlay>/upper" + DEFAULT_VENV + ", "
    "restart onto the base)."
)

# Agent-level verdicts.
VERDICT_MASKED = "masked"
VERDICT_CLEAN = "clean"
VERDICT_UNKNOWN = "unknown"

# Per-shadow-install classifications.
SHADOW_MASKED = "masked"  # base provides the package -> the upper shadows it
SHADOW_OVERLAY_ONLY = "overlay-only"  # complete base set lacks it -> legit add
SHADOW_BASE_UNKNOWN = "base-unknown"  # could not tell what the base provides

# Reason tokens (machine token; the verdict carries a human ``detail`` too).
REASON_NO_OVERLAY = "no-overlay-declared"
REASON_IMAGE_OVERLAY = "image-overlay-unscannable"
REASON_OVERLAY_MISSING = "overlay-root-missing"
REASON_UPPER_UNREADABLE = "upper-unreadable"
REASON_UPPER_VENV_UNTOUCHED = "upper-venv-untouched"
REASON_NO_SHADOW_INSTALLS = "no-shadow-installs"
REASON_MASKED = "base-baked-package-masked"
REASON_BASE_UNKNOWN = "base-set-unknown"
REASON_OVERLAY_ONLY = "overlay-only-installs"
REASON_INSPECT_ERROR = "inspect-error"


def canonical_dist_name(name: str) -> str:
    """PEP 503-ish canonical dist name (lower, ``_``/``.`` -> ``-``)."""
    return (name or "").strip().lower().replace("_", "-").replace(".", "-")


@dataclass(frozen=True)
class BasePackageSet:
    """What the BASE image's venv provides — with honest coverage.

    ``complete=True`` means a full ``pip list`` of the base venv: a package
    absent from it is genuinely not base-provided. ``complete=False`` means
    a partial read (the baked ``/opt/scitex-versions.json`` manifest covers
    scitex-* only): membership proves MASKED, but absence proves nothing —
    a miss classifies as :data:`SHADOW_BASE_UNKNOWN`, never as clean.
    """

    packages: Mapping[str, str]  # canonical dist name -> version
    complete: bool
    source: str = ""  # "live" | "manifest" | a test label


@dataclass(frozen=True)
class ShadowInstall:
    """ONE dist-info found in an overlay upper's site-packages."""

    package: str  # canonical dist name
    version: str  # version the upper carries (i.e. what the agent RUNS)
    dist_info: str  # host path of the dist-info directory (the evidence)
    status: str  # SHADOW_MASKED | SHADOW_OVERLAY_ONLY | SHADOW_BASE_UNKNOWN
    base_version: str = ""  # base's version when status == SHADOW_MASKED

    def to_dict(self) -> dict:
        return {
            "package": self.package,
            "version": self.version,
            "dist_info": self.dist_info,
            "status": self.status,
            "base_version": self.base_version,
        }


@dataclass(frozen=True)
class OverlayMaskVerdict:
    """What we concluded about ONE agent's overlay — and the evidence why.

    Three-valued, like every rail in ``_maintenance``: a state we could not
    read (overlay root missing, unreadable upper, unreadable base package
    set) is :data:`VERDICT_UNKNOWN` — never collapsed into clean. The
    asymmetry is deliberate: a false UNKNOWN is a yellow line in health
    output; a false CLEAN is the 2026-07-22 incident again.
    """

    agent: str
    overlay_root: str  # "" when the spec declares no overlay
    verdict: str  # VERDICT_MASKED | VERDICT_CLEAN | VERDICT_UNKNOWN
    reason: str  # one REASON_* token
    detail: str = ""  # human sentence expanding the token
    shadows: tuple[ShadowInstall, ...] = ()
    copyups: tuple[str, ...] = ()  # benign copy-up dirs (evidence, not alarm)
    stray_dirs: tuple[str, ...] = ()  # non-taxonomy dirs, listed not judged
    base_source: str = ""  # "" when the base set was never consulted

    @property
    def masked(self) -> tuple[ShadowInstall, ...]:
        return tuple(s for s in self.shadows if s.status == SHADOW_MASKED)

    def to_dict(self) -> dict:
        return {
            "agent": self.agent,
            "overlay_root": self.overlay_root,
            "verdict": self.verdict,
            "reason": self.reason,
            "detail": self.detail,
            "shadows": [s.to_dict() for s in self.shadows],
            "copyups": list(self.copyups),
            "stray_dirs": list(self.stray_dirs),
            "base_source": self.base_source,
        }
