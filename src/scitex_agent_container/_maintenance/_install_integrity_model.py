"""Data + vocabulary for install-integrity. No subprocess, no I/O.

The types every other ``_install_integrity*`` module speaks in, kept apart
from the code that observes the world (:mod:`._install_integrity_probe`)
and the code that decides (:mod:`._install_integrity_predicate`) — the
same split :mod:`._worktree_gc_model` uses.

WHY THIS EXISTS — two recurrences of ONE failure class, where the code an
agent executes is not the code anybody believes is installed:

* 2026-07-16 (scitex-dev container): site-packages held an orphaned
  ``scitex_dev-0.31.0.dist-info/`` with no code behind it, plus an
  ``__editable__.scitex_dev-....pth`` redirecting imports into an
  ABANDONED PR scratch worktree. Commands ran from a disposable temp
  directory for days.
* 2026-08-09 (sac container, ``/opt/venv-sac``):
  ``_editable_impl_scitex_agent_container.pth`` points at
  ``.worktrees/persist-boot-failure-diag/.worktrees/agent-aae4c0d9a337e71b6/src``
  — a path that no longer exists, on a branch that no longer exists. A
  REAL ``scitex_agent_container/`` directory sits in site-packages next to
  it, so every import succeeds and ``sac --version`` reports 0.24.25
  happily, while the editable pointer — the thing that was supposed to
  propagate new code — has been inert the whole time.

THE OBVIOUS PREDICATE IS NOT ENOUGH. "Flag anything whose ``__file__``
resolves outside site-packages" catches July and calls August CLEAN,
because the shadowing copy makes ``__file__`` resolve INSIDE
site-packages. ``--version`` is useless for the entire class (confirmed
twice) — only path-level inspection exposes it. Hence five reasons, not
one, and a state that is allowed to say "I could not tell".

The reason strings below are API, not debug text: they land in
``sac installation check --json`` and in the console report.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ._overlay_masking_model import canonical_dist_name

__all__ = [
    "IMPORTS_LIVE",
    "IMPORTS_UNAVAILABLE",
    "POINTER_FINDER",
    "POINTER_PTH_IMPORT",
    "POINTER_PTH_PATH",
    "REASON_DEAD_POINTER",
    "REASON_DUPLICATE_DIST_INFO",
    "REASON_ORDER",
    "REASON_ORPHANED_DIST_INFO",
    "REASON_RESOLVES_OUTSIDE",
    "REASON_SHADOWED_POINTER",
    "STATE_BROKEN",
    "STATE_OK",
    "STATE_UNKNOWN",
    "UNKNOWN_DIST_ABSENT",
    "UNKNOWN_NO_DIST_INFO",
    "UNKNOWN_OWNERSHIP_UNKNOWN",
    "UNKNOWN_POINTER_UNPARSABLE",
    "UNKNOWN_READ_ERROR",
    "UNKNOWN_SITE_UNREADABLE",
    "UNKNOWN_TARGET_UNSTATABLE",
    "DistributionEvidence",
    "DistributionVerdict",
    "EditablePointer",
    "InstallIntegrityReport",
    "SiteEvidence",
    "canonical_dist_name",
]

# --------------------------------------------------------------------------
# States. THREE-VALUED, and UNKNOWN never collapses into either pole:
# it is the absence of evidence, reported as loudly as a failure. A check
# that cannot say "I could not tell" is worse than no check, because it
# launders ignorance into a green tick.
# --------------------------------------------------------------------------
STATE_OK = "ok"
STATE_BROKEN = "broken"
STATE_UNKNOWN = "unknown"

# --------------------------------------------------------------------------
# BROKEN reasons. Each one is a shape that has actually shipped.
# --------------------------------------------------------------------------
#: The imported module resolves outside site-packages and NO live editable
#: pointer for this distribution explains it — an unexplained redirect (a
#: worktree winning on PYTHONPATH, a stray .pth from another dist).
#: Qualified on purpose: an editable install resolving to its OWN live
#: target is what an editable install IS, and flagging that would make the
#: check scream on every healthy dev venv. The July shape is still caught,
#: by ORPHANED_DIST_INFO (and by DEAD_POINTER once the scratch tree goes).
REASON_RESOLVES_OUTSIDE = "resolves-outside-site-packages"

#: An editable pointer exists whose target path does NOT exist. Whatever
#: that pointer was supposed to serve is served by nothing.
REASON_DEAD_POINTER = "dead-pointer"

#: An editable pointer AND a real package directory both exist for the
#: same distribution. The copy WINS and the pointer is inert, so imports
#: succeed and propagation is silently dead. The August 2026 shape — the
#: one a ``__file__``-outside-site-packages check reports as clean.
REASON_SHADOWED_POINTER = "shadowed-pointer"

#: A dist-info is present with no code behind it: nothing in
#: site-packages, and no live pointer serving it from elsewhere. It keeps
#: advertising a version whose code is gone.
REASON_ORPHANED_DIST_INFO = "orphaned-dist-info"

#: More than one dist-info for the same distribution. One of them is a
#: fossil. (Prior instance: two ``scitex_cards`` dist-info dirs, 0.17.5
#: and 0.17.7, in one site-packages.)
REASON_DUPLICATE_DIST_INFO = "duplicate-dist-info"

#: Deterministic render/serialise order — never set-iteration order.
REASON_ORDER = (
    REASON_RESOLVES_OUTSIDE,
    REASON_DEAD_POINTER,
    REASON_SHADOWED_POINTER,
    REASON_ORPHANED_DIST_INFO,
    REASON_DUPLICATE_DIST_INFO,
)

# --------------------------------------------------------------------------
# UNKNOWN reasons. Never folded into OK ("nothing found, looks fine") nor
# into BROKEN ("can't read it, call it broken") — both are lies, with
# different signs.
# --------------------------------------------------------------------------
UNKNOWN_SITE_UNREADABLE = "site-packages-unreadable"
UNKNOWN_DIST_ABSENT = "distribution-absent"
UNKNOWN_READ_ERROR = "distribution-unreadable"
UNKNOWN_OWNERSHIP_UNKNOWN = "owned-modules-undeterminable"
UNKNOWN_POINTER_UNPARSABLE = "pointer-target-unparsable"
UNKNOWN_TARGET_UNSTATABLE = "pointer-target-unstatable"
UNKNOWN_NO_DIST_INFO = "no-dist-info-for-pointer"

# --------------------------------------------------------------------------
# Editable-pointer shapes pip / uv / setuptools actually emit.
# --------------------------------------------------------------------------
#: ``__editable__*.pth`` / ``*_editable_impl_*.pth`` / a plain ``.pth``
#: holding a bare absolute path on its own line.
POINTER_PTH_PATH = "pth-path"
#: A ``.pth`` whose line is an ``import __editable___x_finder; ...install()``
#: statement — the path lives in the finder module it names.
POINTER_PTH_IMPORT = "pth-import"
#: ``__editable___*_finder.py`` — its MAPPING dict maps module -> real dir.
POINTER_FINDER = "finder-module"

# --------------------------------------------------------------------------
# Whether import resolution was observable at all.
# --------------------------------------------------------------------------
#: The inspected site-packages IS the running interpreter's, so where an
#: import really lands is observable.
IMPORTS_LIVE = "live"
#: A foreign venv was inspected. Its import resolution cannot be observed
#: from here, so REASON_RESOLVES_OUTSIDE is NEVER decided; the other four
#: reasons are pure path-level facts and stay fully decidable.
IMPORTS_UNAVAILABLE = "unavailable"


@dataclass(frozen=True)
class EditablePointer:
    """One redirect file found in site-packages, as observed.

    ``target_exists`` is deliberately tri-state: ``None`` means the stat
    itself failed (permissions, a dangling mount), which is UNKNOWN — not
    "the target is missing", which would be a fabricated BROKEN.
    """

    path: str  # the .pth / *_finder.py file itself
    shape: str  # one POINTER_* token
    target: str = ""  # absolute path it redirects to; "" = unparsable
    target_exists: bool | None = None

    @property
    def dead(self) -> bool:
        """Parsed a target, and it provably is not there."""
        return bool(self.target) and self.target_exists is False

    @property
    def live(self) -> bool:
        """Parsed a target, and it provably IS there."""
        return bool(self.target) and self.target_exists is True

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "shape": self.shape,
            "target": self.target,
            "target_exists": self.target_exists,
        }


@dataclass(frozen=True)
class DistributionEvidence:
    """Everything the probe saw on disk about ONE distribution.

    This is the decision layer's whole input surface. Building one by hand
    is how the tests reach every reason without constructing a real venv.
    """

    name: str  # canonical dist name
    dist_infos: tuple[str, ...] = ()  # every *.dist-info / *.egg-info dir
    top_level_names: tuple[str, ...] = ()  # modules the dist claims to own
    top_level_known: bool = False  # False -> ownership undeterminable
    code_paths: tuple[str, ...] = ()  # of those, what really exists INSIDE site
    pointers: tuple[EditablePointer, ...] = ()
    declared_editable: bool = False  # PEP 610 direct_url.json says editable
    imported_path: str = ""  # where `import <top-level>` really landed
    import_resolution_known: bool = False
    absent: bool = False  # asked for by name; nothing found at all
    read_error: str = ""  # this dist's metadata could not be read

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "dist_infos": list(self.dist_infos),
            "top_level_names": list(self.top_level_names),
            "top_level_known": self.top_level_known,
            "code_paths": list(self.code_paths),
            "pointers": [p.to_dict() for p in self.pointers],
            "declared_editable": self.declared_editable,
            "imported_path": self.imported_path,
            "import_resolution_known": self.import_resolution_known,
            "absent": self.absent,
            "read_error": self.read_error,
        }


@dataclass(frozen=True)
class DistributionVerdict:
    """Tri-state verdict for one distribution; UNKNOWN never reads OK.

    ``reasons`` is a TUPLE, not a single token, because the shapes really
    do co-occur: the August case is SHADOWED_POINTER *and* DEAD_POINTER at
    once, and reporting only the first would hide half the repair.
    """

    name: str
    state: str  # STATE_OK | STATE_BROKEN | STATE_UNKNOWN
    reasons: tuple[str, ...] = ()
    unknown_reasons: tuple[str, ...] = ()
    details: tuple[str, ...] = ()
    evidence: DistributionEvidence | None = None

    @property
    def broken(self) -> bool:
        return self.state == STATE_BROKEN

    @property
    def unknown(self) -> bool:
        return self.state == STATE_UNKNOWN

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "state": self.state,
            "reasons": list(self.reasons),
            "unknown_reasons": list(self.unknown_reasons),
            "details": list(self.details),
            "evidence": self.evidence.to_dict() if self.evidence else None,
        }


@dataclass(frozen=True)
class SiteEvidence:
    """One site-packages directory as observed, plus its distributions."""

    site_packages: str
    readable: bool = True
    read_error: str = ""
    venv: str = ""
    import_resolution: str = IMPORTS_UNAVAILABLE
    note: str = ""  # e.g. several site-packages matched; which one we took
    distributions: tuple[DistributionEvidence, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class InstallIntegrityReport:
    """The whole pass over one site-packages. Never a bare bool."""

    site_packages: str
    venv: str = ""
    site_state: str = STATE_OK
    site_detail: str = ""
    import_resolution: str = IMPORTS_UNAVAILABLE
    note: str = ""
    verdicts: tuple[DistributionVerdict, ...] = ()

    @property
    def broken(self) -> tuple[DistributionVerdict, ...]:
        return tuple(v for v in self.verdicts if v.broken)

    @property
    def unknown(self) -> tuple[DistributionVerdict, ...]:
        return tuple(v for v in self.verdicts if v.unknown)

    @property
    def ok(self) -> tuple[DistributionVerdict, ...]:
        return tuple(v for v in self.verdicts if v.state == STATE_OK)

    @property
    def site_unknown(self) -> bool:
        """The directory itself could not be read — no verdict is possible."""
        return self.site_state == STATE_UNKNOWN

    def reason_breakdown(self) -> dict[str, int]:
        """``{"shadowed-pointer": 1, "duplicate-dist-info": 2}`` — WHY.

        "3 broken" tells an operator nothing; the breakdown tells them
        which repair each one needs.
        """
        counts: dict[str, int] = {}
        for verdict in self.verdicts:
            for reason in verdict.reasons:
                counts[reason] = counts.get(reason, 0) + 1
        return {r: counts[r] for r in REASON_ORDER if r in counts}

    def summary_line(self) -> str:
        parts = [
            f"{len(self.verdicts)} distribution(s)",
            f"{len(self.ok)} ok",
            f"{len(self.broken)} BROKEN",
            f"{len(self.unknown)} UNKNOWN",
        ]
        if self.import_resolution != IMPORTS_LIVE:
            parts.append("import-resolution UNOBSERVABLE")
        return ", ".join(parts)

    def to_dict(self) -> dict:
        return {
            "site_packages": self.site_packages,
            "venv": self.venv,
            "site_state": self.site_state,
            "site_detail": self.site_detail,
            "import_resolution": self.import_resolution,
            "note": self.note,
            "counts": {
                "total": len(self.verdicts),
                "ok": len(self.ok),
                "broken": len(self.broken),
                "unknown": len(self.unknown),
            },
            "reason_breakdown": self.reason_breakdown(),
            "distributions": [v.to_dict() for v in self.verdicts],
        }
