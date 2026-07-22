"""Vocabulary + data types for overlay-masking detection (:mod:`._overlay_masking`)."""

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

# User-facing output (quoted verbatim by the health rendering), not a comment.
OPERATIONAL_RULE = (
    "NEVER pip-install a base-baked package into a running agent's overlay: "
    "the overlay upper masks the base venv from then on, and every later "
    "base rebuild is silently shadowed for that agent. Hotfix EITHER by "
    "force-reinstalling the EXACT base version, OR by recreating the "
    "overlay (stop the agent, remove <overlay>/upper" + DEFAULT_VENV + ", "
    "restart onto the base)."
)

# Agent-level verdicts (API strings: `sac agents check-health --json`).
VERDICT_MASKED = "masked"
VERDICT_CLEAN = "clean"
VERDICT_UNKNOWN = "unknown"

# Per-shadow-install classifications.
SHADOW_MASKED = "masked"
SHADOW_OVERLAY_ONLY = "overlay-only"
SHADOW_BASE_UNKNOWN = "base-unknown"

# Reason tokens (API; the verdict carries a human ``detail`` too).
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
    """Packages the BASE image's venv provides.

    ``complete=False`` (e.g. the baked scitex-* manifest) means absence
    proves nothing: a miss classifies SHADOW_BASE_UNKNOWN, never clean.
    """

    packages: Mapping[str, str]  # canonical dist name -> version
    complete: bool
    source: str = ""  # "live" | "manifest" | a test label


@dataclass(frozen=True)
class ShadowInstall:
    """One dist-info found in an overlay upper's site-packages."""

    package: str
    version: str  # what the agent actually runs
    dist_info: str  # evidence path
    status: str  # SHADOW_MASKED | SHADOW_OVERLAY_ONLY | SHADOW_BASE_UNKNOWN
    base_version: str = ""

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
    """Tri-state verdict for one agent's overlay; UNKNOWN never reads clean."""

    agent: str
    overlay_root: str  # "" when the spec declares no overlay
    verdict: str  # VERDICT_MASKED | VERDICT_CLEAN | VERDICT_UNKNOWN
    reason: str  # one REASON_* token
    detail: str = ""
    shadows: tuple[ShadowInstall, ...] = ()
    copyups: tuple[str, ...] = ()  # benign copy-up dirs (evidence, not alarm)
    stray_dirs: tuple[str, ...] = ()
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
