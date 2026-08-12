"""OBSERVATION for install-integrity — everything that touches the world.

Isolated from the decision logic (:mod:`._install_integrity_predicate`) on
purpose: every read here can fail for reasons that have nothing to do with
the install (an unreadable site-packages, a truncated RECORD, a dangling
mount), and each one converts that failure into an honest UNKNOWN leg on
the evidence rather than into a convenient boolean. The predicate then only
has to know how to report an unknown — it never has to guess whether an
answer is real.

READ-ONLY, ALWAYS. This module inspects; it never repairs. Repairing a
venv is a separate, deliberate act that belongs behind this guard, not
inside it.
"""

from __future__ import annotations

import json
import logging
import sysconfig
from pathlib import Path

from ._install_integrity_model import (
    IMPORTS_LIVE,
    IMPORTS_UNAVAILABLE,
    DistributionEvidence,
    EditablePointer,
    InstallIntegrityReport,
    SiteEvidence,
    canonical_dist_name,
)
from ._install_integrity_pointers import candidate_dist_names, collect_pointers
from ._install_integrity_predicate import build_report

logger = logging.getLogger(__name__)

__all__ = [
    "inspect_install",
    "read_site_evidence",
    "resolve_site_packages",
    "running_site_packages",
]

_DIST_SUFFIXES = (".dist-info", ".egg-info")
_SKIP_TOP_LEVEL = {"__pycache__", "", "."}

#: pip renames a distribution's first character to ``~`` while it replaces
#: it, and leaves the directory behind if the run is interrupted. Such a
#: dir is debris BY CONSTRUCTION: no distribution is named ``~foo``, so it
#: can never be imported, and its RECORD describes the OTHER (real)
#: distribution's modules. Attributing that RECORD to it would credit it
#: with code it does not own — which is exactly how the fossil hides.
_PIP_DEBRIS_PREFIX = "~"


def running_site_packages() -> str:
    """The site-packages of the venv the RUNNING interpreter belongs to."""
    return sysconfig.get_paths()["purelib"]


def resolve_site_packages(target: str | Path | None) -> tuple[str, str]:
    """``target`` -> ``(site_packages, note)``. ``None`` = this interpreter's.

    Accepts a venv root, a site-packages directory directly, or ``None``.
    A venv with several ``lib/python*/site-packages`` (two interpreters
    installed into one prefix) is NOT merged — merging would invent
    duplicate dist-infos that do not exist for either interpreter. The
    highest version is inspected and the rest are named in the note.
    """
    if target is None:
        return running_site_packages(), ""
    root = Path(target).expanduser()
    if root.name == "site-packages":
        return str(root), ""
    matches = sorted(root.glob("lib/python*/site-packages")) + sorted(
        root.glob("Lib/site-packages")
    )
    if not matches:
        return str(root / "lib" / "pythonX.Y" / "site-packages"), (
            f"no lib/python*/site-packages under {root}"
        )
    chosen = matches[-1]
    if len(matches) == 1:
        return str(chosen), ""
    others = ", ".join(str(m) for m in matches[:-1])
    return str(chosen), (
        f"{len(matches)} site-packages dirs under {root}; inspected {chosen} "
        f"(not merged with: {others})"
    )


def _parse_dist_dirname(dirname: str) -> str:
    """``Name-Version.dist-info`` / ``Name.egg-info`` -> canonical name."""
    for suffix in _DIST_SUFFIXES:
        if dirname.endswith(suffix):
            stem = dirname[: -len(suffix)]
            break
    else:
        return ""
    name, _, version = stem.rpartition("-")
    return canonical_dist_name(name if name and version else stem)


def _read_text(path: Path) -> str | None:
    # stx-allow: fallback (reason: unreadable metadata is an UNKNOWN leg on the evidence, never a crash of the check that reads it)
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.debug("install-integrity: cannot read %s: %s", path, exc)
        return None


def _top_level_from_record(text: str) -> list[str]:
    """First path segment of every RECORD row that is not metadata."""
    names: list[str] = []
    for raw in text.splitlines():
        row = raw.split(",", 1)[0].strip()
        if not row or row.startswith(("../", "/")):
            continue
        head = row.split("/", 1)[0]
        if head.endswith(_DIST_SUFFIXES) or head.endswith(".data"):
            continue
        if head in _SKIP_TOP_LEVEL:
            continue
        names.append(head[:-3] if head.endswith(".py") else head)
    return list(dict.fromkeys(names))


def _owned_modules(dist_infos: list[Path]) -> tuple[tuple[str, ...], bool]:
    """What a dist claims to own -> ``(names, known)``.

    ``top_level.txt`` first (explicit), then ``RECORD`` (derived). When
    NEITHER answers, ``known`` is False and the caller must NOT decide
    "has code behind it" — that is the difference between reporting an
    orphan and inventing one.
    """
    names: list[str] = []
    known = False
    for dist_info in dist_infos:
        top_level = _read_text(dist_info / "top_level.txt")
        if top_level and top_level.split():
            names.extend(top_level.split())
            known = True
            continue
        record = _read_text(dist_info / "RECORD")
        if record is not None:
            derived = _top_level_from_record(record)
            if derived:
                names.extend(derived)
                known = True
    return tuple(dict.fromkeys(names)), known


def _declared_editable(dist_infos: list[Path]) -> bool:
    """PEP 610: does any dist-info say this was installed ``-e``?"""
    for dist_info in dist_infos:
        raw = _read_text(dist_info / "direct_url.json")
        if not raw:
            continue
        # stx-allow: fallback (reason: malformed direct_url.json means "cannot tell it is editable", not a crash)
        try:
            data = json.loads(raw)
        except ValueError:
            continue
        if isinstance(data, dict) and data.get("dir_info", {}).get("editable"):
            return True
    return False


def _code_paths(
    site: Path, names: tuple[str, ...], entries: set[str]
) -> tuple[str, ...]:
    """Which of ``names`` really exist as code INSIDE site-packages."""
    found: list[str] = []
    for name in names:
        if name in entries and (site / name).is_dir():
            found.append(str(site / name))
        elif f"{name}.py" in entries:
            found.append(str(site / f"{name}.py"))
    return tuple(found)


def _resolve_import(name: str) -> str:
    """Where ``import <name>`` really lands, or ``""`` if unresolvable.

    ``find_spec`` on a TOP-LEVEL name runs the path finders without
    executing the module, so this observes resolution without importing
    the very code we suspect.
    """
    import importlib.util

    # stx-allow: fallback (reason: a broken third-party meta-path finder must leave the import leg UNKNOWN, never crash the inspection)
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, AttributeError, ValueError, OSError) as exc:
        logger.debug("install-integrity: find_spec(%s) failed: %s", name, exc)
        return ""
    if spec is None:
        return ""
    if spec.origin and spec.origin not in ("built-in", "frozen"):
        return str(Path(spec.origin).parent)
    locations = list(getattr(spec, "submodule_search_locations", None) or [])
    return str(locations[0]) if locations else ""


def _attribute_pointers(
    pointers: list[EditablePointer], known: set[str]
) -> dict[str, list[EditablePointer]]:
    """Group pointers by the distribution their FILENAME points at.

    A candidate that matches no dist we saw keeps its longest form and
    becomes its own row, so a pointer for a distribution that no longer
    has a dist-info is still reported rather than dropped.
    """
    grouped: dict[str, list[EditablePointer]] = {}
    for pointer in pointers:
        candidates = candidate_dist_names(Path(pointer.path).name)
        match = next((c for c in candidates if c in known), "")
        key = match or (candidates[0] if candidates else "unattributed-pointer")
        grouped.setdefault(key, []).append(pointer)
    return grouped


def read_site_evidence(
    site_packages: str, *, live_imports: bool, note: str = "", venv: str = ""
) -> SiteEvidence:
    """Observe one site-packages directory. Never raises."""
    site = Path(site_packages)
    # stx-allow: fallback (reason: an unreadable site-packages is the headline UNKNOWN this check exists to report honestly)
    try:
        entry_names = [e.name for e in site.iterdir()]
    except OSError as exc:
        return SiteEvidence(
            site_packages=site_packages,
            readable=False,
            read_error=f"{type(exc).__name__}: {exc}",
            venv=venv,
            note=note,
            import_resolution=IMPORTS_LIVE if live_imports else IMPORTS_UNAVAILABLE,
        )

    entries = set(entry_names)
    dist_dirs: dict[str, list[Path]] = {}
    for name in sorted(entry_names):
        if not name.endswith(_DIST_SUFFIXES):
            continue
        canonical = _parse_dist_dirname(name)
        if canonical:
            dist_dirs.setdefault(canonical, []).append(site / name)

    pointers = collect_pointers(site, entry_names)
    by_dist = _attribute_pointers(pointers, set(dist_dirs))

    evidence: list[DistributionEvidence] = []
    for canonical in sorted(set(dist_dirs) | set(by_dist)):
        dist_infos = dist_dirs.get(canonical, [])
        if canonical.startswith(_PIP_DEBRIS_PREFIX):
            # Interrupted-pip debris: it owns no modules OF ITS OWN (its
            # RECORD describes the distribution it was replacing), and
            # nothing can import it. Stating that plainly makes the
            # orphan check fire on it instead of crediting it with the
            # real distribution's code.
            evidence.append(
                DistributionEvidence(
                    name=canonical,
                    dist_infos=tuple(str(p) for p in dist_infos),
                    top_level_known=True,
                )
            )
            continue
        owned, known = _owned_modules(dist_infos)
        if not owned:
            # No dist-info (pointer-only row): the filename is all we have.
            owned = (canonical.replace("-", "_"),)
        code = _code_paths(site, owned, entries)
        imported = _resolve_import(owned[0]) if live_imports and owned else ""
        evidence.append(
            DistributionEvidence(
                name=canonical,
                dist_infos=tuple(str(p) for p in dist_infos),
                top_level_names=owned,
                top_level_known=known,
                code_paths=code,
                pointers=tuple(by_dist.get(canonical, [])),
                declared_editable=_declared_editable(dist_infos),
                imported_path=imported,
                import_resolution_known=live_imports,
            )
        )
    return SiteEvidence(
        site_packages=site_packages,
        readable=True,
        venv=venv,
        note=note,
        import_resolution=IMPORTS_LIVE if live_imports else IMPORTS_UNAVAILABLE,
        distributions=tuple(evidence),
    )


def _absent(name: str) -> DistributionEvidence:
    return DistributionEvidence(name=canonical_dist_name(name), absent=True)


def inspect_install(
    venv: str | Path | None = None, *, dists: tuple[str, ...] = ()
) -> InstallIntegrityReport:
    """Inspect a venv's install layout. READ-ONLY. Never raises.

    ``venv=None`` inspects the venv the running interpreter belongs to,
    which is the only case where import resolution is observable; a venv
    given explicitly is inspected at the path level only, and the report
    says so rather than implying the import leg came back clean.

    ``dists`` narrows the report to named distributions. One that is not
    present comes back UNKNOWN (distribution-absent) — never OK, because
    "I did not find it" is not "it is fine".
    """
    site_packages, note = resolve_site_packages(venv)
    # Import resolution is observable ONLY for our own interpreter's
    # site-packages. Anything else is a foreign venv we can read but not run.
    live = site_packages.rstrip("/") == running_site_packages().rstrip("/")
    site = read_site_evidence(
        site_packages,
        live_imports=live,
        note=note,
        venv=str(Path(venv).expanduser()) if venv is not None else "",
    )
    if dists and site.readable:
        wanted = [canonical_dist_name(d) for d in dists]
        seen = {ev.name: ev for ev in site.distributions}
        site = SiteEvidence(
            site_packages=site.site_packages,
            readable=True,
            venv=site.venv,
            note=site.note,
            import_resolution=site.import_resolution,
            distributions=tuple(seen.get(name) or _absent(name) for name in wanted),
        )
    return build_report(site)
