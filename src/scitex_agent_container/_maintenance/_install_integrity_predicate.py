"""DECISION for install-integrity. PURE — takes evidence, returns verdicts.

Nothing here touches the filesystem: every function consumes the
dataclasses :mod:`._install_integrity_model` defines and returns a
verdict, so the tests reach all five reasons by constructing evidence
rather than by building real venvs. Observation lives next door, in
:mod:`._install_integrity_probe`.

The precedence rule, stated once and applied everywhere below:

* a positively-observed BROKEN reason OUTRANKS an UNKNOWN leg, because a
  definite finding is more actionable than a gap — but the gap is still
  carried in ``unknown_reasons``, so the report never implies the picture
  was complete;
* UNKNOWN outranks OK, for the same reason in reverse: never claim clean
  off evidence we do not have.
"""

from __future__ import annotations

from ._install_integrity_model import (
    REASON_DEAD_POINTER,
    REASON_DUPLICATE_DIST_INFO,
    REASON_ORPHANED_DIST_INFO,
    REASON_RESOLVES_OUTSIDE,
    REASON_SHADOWED_POINTER,
    STATE_BROKEN,
    STATE_OK,
    STATE_UNKNOWN,
    UNKNOWN_DIST_ABSENT,
    UNKNOWN_NO_DIST_INFO,
    UNKNOWN_OWNERSHIP_UNKNOWN,
    UNKNOWN_POINTER_UNPARSABLE,
    UNKNOWN_READ_ERROR,
    UNKNOWN_TARGET_UNSTATABLE,
    DistributionEvidence,
    DistributionVerdict,
    InstallIntegrityReport,
    SiteEvidence,
)

__all__ = [
    "build_report",
    "classify_distribution",
    "exit_code_for",
    "is_under",
]


def is_under(child: str, parent: str) -> bool:
    """Path containment on plain strings — no filesystem access.

    Both sides are compared with a trailing separator appended, so
    ``/a/bc`` is never read as living under ``/a/b``.
    """
    if not child or not parent:
        return False
    child_n = child.rstrip("/")
    parent_n = parent.rstrip("/")
    return child_n == parent_n or child_n.startswith(parent_n + "/")


def _explained_by_live_pointer(ev: DistributionEvidence) -> bool:
    """Is the out-of-site import path served by this dist's OWN live pointer?

    That is exactly what a healthy editable install looks like, and it
    must not be reported as an unexplained redirect.
    """
    return any(p.live and is_under(ev.imported_path, p.target) for p in ev.pointers)


def _check_resolves_outside(
    ev: DistributionEvidence, site_packages: str
) -> tuple[str, str] | None:
    if not ev.import_resolution_known or not ev.imported_path:
        return None
    if is_under(ev.imported_path, site_packages):
        return None
    if _explained_by_live_pointer(ev):
        return None
    return (
        REASON_RESOLVES_OUTSIDE,
        f"imports resolve to {ev.imported_path}, outside {site_packages}, and no "
        f"live editable pointer for {ev.name} explains it — the running code is "
        f"not the installed code",
    )


def _check_dead_pointer(ev: DistributionEvidence) -> tuple[str, str] | None:
    dead = [p for p in ev.pointers if p.dead]
    if not dead:
        return None
    listed = ", ".join(f"{p.path} -> {p.target}" for p in dead)
    return (
        REASON_DEAD_POINTER,
        f"{len(dead)} editable pointer(s) target a path that does not exist: "
        f"{listed} — whatever they were meant to serve is served by nothing",
    )


def _check_shadowed_pointer(ev: DistributionEvidence) -> tuple[str, str] | None:
    if not ev.pointers or not ev.code_paths:
        return None
    pointers = ", ".join(p.path for p in ev.pointers)
    copies = ", ".join(ev.code_paths)
    return (
        REASON_SHADOWED_POINTER,
        f"an editable pointer ({pointers}) AND a real package directory "
        f"({copies}) both exist for {ev.name} — the copy WINS, so the pointer is "
        f"inert and nothing it targets ever propagates, while imports keep "
        f"succeeding and --version keeps looking right",
    )


def _check_orphaned_dist_info(ev: DistributionEvidence) -> tuple[str, str] | None:
    if not ev.dist_infos or not ev.top_level_known or ev.code_paths:
        return None
    # Only a pointer PROVEN dead leaves the dist-info unserved. A live one
    # means an editable install legitimately keeps its code elsewhere; an
    # UNSETTLED one (target unparsable, or a path we could not stat) might
    # be serving it too, and calling that an orphan would launder an
    # unknown into a definite verdict — the exact move this module exists
    # to prevent. The unsettled leg is still reported, as an UNKNOWN.
    if any(not pointer.dead for pointer in ev.pointers):
        return None
    owned = ", ".join(ev.top_level_names) or "(nothing declared)"
    return (
        REASON_ORPHANED_DIST_INFO,
        f"dist-info present ({', '.join(ev.dist_infos)}) but none of the modules "
        f"it claims to own ({owned}) exist in site-packages, and no live pointer "
        f"serves them — it advertises a version whose code is gone",
    )


def _check_duplicate_dist_info(ev: DistributionEvidence) -> tuple[str, str] | None:
    if len(ev.dist_infos) < 2:
        return None
    return (
        REASON_DUPLICATE_DIST_INFO,
        f"{len(ev.dist_infos)} dist-info dirs for {ev.name}: "
        f"{', '.join(ev.dist_infos)} — at least one is a fossil",
    )


def _unknown_legs(ev: DistributionEvidence) -> list[tuple[str, str]]:
    """Every leg we could NOT settle. Reported alongside any BROKEN reason."""
    legs: list[tuple[str, str]] = []
    if ev.read_error:
        legs.append((UNKNOWN_READ_ERROR, f"metadata unreadable: {ev.read_error}"))
    if ev.dist_infos and not ev.top_level_known:
        legs.append(
            (
                UNKNOWN_OWNERSHIP_UNKNOWN,
                "neither top_level.txt nor RECORD said which modules this dist "
                "owns, so 'has code behind it' cannot be decided",
            )
        )
    if not ev.dist_infos and ev.pointers:
        legs.append(
            (
                UNKNOWN_NO_DIST_INFO,
                "an editable pointer exists with no dist-info to describe it — "
                "what it is meant to serve cannot be established",
            )
        )
    for pointer in ev.pointers:
        if not pointer.target:
            legs.append(
                (
                    UNKNOWN_POINTER_UNPARSABLE,
                    f"no target could be parsed out of {pointer.path}",
                )
            )
        elif pointer.target_exists is None:
            legs.append(
                (
                    UNKNOWN_TARGET_UNSTATABLE,
                    f"could not stat {pointer.target} (from {pointer.path})",
                )
            )
    return legs


def classify_distribution(
    ev: DistributionEvidence, site_packages: str
) -> DistributionVerdict:
    """One distribution's evidence -> one tri-state verdict. PURE."""
    if ev.absent:
        return DistributionVerdict(
            name=ev.name,
            state=STATE_UNKNOWN,
            unknown_reasons=(UNKNOWN_DIST_ABSENT,),
            details=(f"no dist-info, pointer, or code found for {ev.name}",),
            evidence=ev,
        )

    checks = (
        _check_resolves_outside(ev, site_packages),
        _check_dead_pointer(ev),
        _check_shadowed_pointer(ev),
        _check_orphaned_dist_info(ev),
        _check_duplicate_dist_info(ev),
    )
    hits = [c for c in checks if c is not None]
    unknowns = _unknown_legs(ev)

    reasons = tuple(r for r, _ in hits)
    details = tuple(d for _, d in hits) + tuple(d for _, d in unknowns)
    unknown_reasons = tuple(dict.fromkeys(r for r, _ in unknowns))

    if reasons:
        state = STATE_BROKEN
    elif unknown_reasons:
        state = STATE_UNKNOWN
    else:
        state = STATE_OK
    return DistributionVerdict(
        name=ev.name,
        state=state,
        reasons=reasons,
        unknown_reasons=unknown_reasons,
        details=details,
        evidence=ev,
    )


def build_report(site: SiteEvidence) -> InstallIntegrityReport:
    """Whole-site evidence -> the report the CLI renders. PURE."""
    if not site.readable:
        return InstallIntegrityReport(
            site_packages=site.site_packages,
            venv=site.venv,
            site_state=STATE_UNKNOWN,
            site_detail=(
                f"could not read {site.site_packages}: {site.read_error} — that is "
                f"UNKNOWN, not clean: nothing about this install was observed"
            ),
            import_resolution=site.import_resolution,
            note=site.note,
        )
    verdicts = tuple(
        classify_distribution(ev, site.site_packages)
        for ev in sorted(site.distributions, key=lambda e: e.name)
    )
    return InstallIntegrityReport(
        site_packages=site.site_packages,
        venv=site.venv,
        site_state=STATE_OK,
        import_resolution=site.import_resolution,
        note=site.note,
        verdicts=verdicts,
    )


def exit_code_for(report: InstallIntegrityReport, *, strict: bool = False) -> int:
    """0 clean, 1 any distribution BROKEN, 2 UNKNOWN under ``strict``.

    UNKNOWN alone is NOT a failure by default: it is an absence of
    evidence, and turning "I could not look" into a red build teaches
    people to append ``|| true``. It is still printed as loudly as a
    break, and ``strict=True`` exists for callers who genuinely require a
    COMPLETE answer (a deploy gate) rather than merely no bad news.
    """
    if report.broken:
        return 1
    if strict and (report.site_unknown or report.unknown):
        return 2
    return 0
