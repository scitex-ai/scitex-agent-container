"""Detect agent overlays whose UPPER layer masks a base-baked package.

Engine half of the pair; the vocabulary, dataclasses and the incident
narrative live in :mod:`._overlay_masking_model`.

THE DETECTION RULE (validated by an actual fleet sweep — do not "improve")
--------------------------------------------------------------------------
Count ``*.dist-info`` directories under the overlay upper's
``<venv>/lib/python*/site-packages``. A dist-info present in the upper for
a package the base provides = MASKED.

CRITICAL false-scare guard: a bare package directory containing ONLY
``__pycache__`` and ZERO top-level ``*.py`` files is a BENIGN overlayfs
copy-up (a byte-compile wrote a ``.pyc``, overlayfs copied the directory
entry up), NOT masking. Only a dist-info in the upper indicates a real
shadowing install. This distinction already cost one false fleet alarm.

Three-valued throughout: "could not look" (overlay root missing, unreadable
upper, unreadable base set) reports UNKNOWN and never reads as clean.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Callable

from .._drift.versions import (
    DEFAULT_VENV,
    _apptainer_exec,
    agent_base_image_name,
    discover_base_sifs,
    read_base_manifest,
)
from ..runtimes._apptainer_overlay import (
    OVERLAY_UPPER_DIRNAME,
    is_image_overlay,
    resolve_overlay_declaration,
)
from ._overlay_masking_model import (
    REASON_BASE_UNKNOWN,
    REASON_IMAGE_OVERLAY,
    REASON_INSPECT_ERROR,
    REASON_MASKED,
    REASON_NO_OVERLAY,
    REASON_NO_SHADOW_INSTALLS,
    REASON_OVERLAY_MISSING,
    REASON_OVERLAY_ONLY,
    REASON_UPPER_UNREADABLE,
    REASON_UPPER_VENV_UNTOUCHED,
    SHADOW_BASE_UNKNOWN,
    SHADOW_MASKED,
    SHADOW_OVERLAY_ONLY,
    VERDICT_CLEAN,
    VERDICT_MASKED,
    VERDICT_UNKNOWN,
    BasePackageSet,
    OverlayMaskVerdict,
    ShadowInstall,
    canonical_dist_name,
)

logger = logging.getLogger(__name__)

__all__ = [
    "base_package_set_for",
    "inspect_agent_overlay",
    "inspect_overlay",
    "sweep_agent_overlays",
]

_DIST_INFO_SUFFIX = ".dist-info"


# ---------------------------------------------------------------------------
# the scan (pure filesystem; raises OSError so the caller can say UNKNOWN)
# ---------------------------------------------------------------------------
def _parse_dist_info_name(dirname: str) -> tuple[str, str]:
    """``Name-Version.dist-info`` -> ``(canonical_name, version)`` or ``("", "")``."""
    stem = dirname[: -len(_DIST_INFO_SUFFIX)]
    name, _, version = stem.rpartition("-")
    if name and version:
        return canonical_dist_name(name), version
    return "", ""


def _dir_has_toplevel_py(pkg_dir: Path) -> bool:
    """True iff ``pkg_dir`` carries at least one TOP-LEVEL ``*.py``.

    Raises OSError when unreadable — the caller must not guess.
    """
    return any(child.suffix == ".py" for child in pkg_dir.iterdir())


def _scan_site_packages(
    site: Path,
) -> tuple[list[tuple[str, str, str]], list[str], list[str]]:
    """Scan ONE upper site-packages dir per the validated detection rule.

    Returns ``(dist_infos, copyups, strays)``:

    * ``dist_infos`` — ``(canonical_name, version, path)`` per ``*.dist-info``
      found: the ONLY signal that drives a MASKED verdict.
    * ``copyups`` — package dirs with NO matching dist-info and ZERO
      top-level ``*.py``: the benign overlayfs copy-up shape. Reported as
      evidence precisely because counting these as masking caused a false
      fleet scare.
    * ``strays`` — package dirs with no dist-info but WITH top-level
      ``*.py``. Outside the validated taxonomy, so they NEVER drive the
      verdict; listed so an operator reading the report sees everything the
      scan saw.

    Raises OSError when the directory cannot be read (caller -> UNKNOWN).
    """
    dist_infos: list[tuple[str, str, str]] = []
    copyups: list[str] = []
    strays: list[str] = []
    entries = sorted(site.iterdir())
    owned: set[str] = set()
    for entry in entries:
        if entry.is_dir() and entry.name.endswith(_DIST_INFO_SUFFIX):
            name, version = _parse_dist_info_name(entry.name)
            if name:
                dist_infos.append((name, version, str(entry)))
                owned.add(name)
    for entry in entries:
        if not entry.is_dir():
            continue
        if entry.name.endswith((_DIST_INFO_SUFFIX, ".egg-info")):
            continue
        if entry.name == "__pycache__":
            continue
        if canonical_dist_name(entry.name) in owned:
            continue  # the dist-info row above already reports this package
        if _dir_has_toplevel_py(entry):
            strays.append(entry.name)
        else:
            copyups.append(entry.name)
    return dist_infos, copyups, strays


def _scan_upper_venv(
    upper_venv_root: Path,
) -> tuple[list[tuple[str, str, str]], list[str], list[str]]:
    """Scan every ``lib/python*/site-packages`` under the upper venv root."""
    dist_infos: list[tuple[str, str, str]] = []
    copyups: list[str] = []
    strays: list[str] = []
    for site in sorted(upper_venv_root.glob("lib/python*/site-packages")):
        d, c, s = _scan_site_packages(site)
        dist_infos.extend(d)
        copyups.extend(c)
        strays.extend(s)
    return dist_infos, copyups, strays


# ---------------------------------------------------------------------------
# classification + the tri-state verdict
# ---------------------------------------------------------------------------
def _classify(name: str, base: BasePackageSet | None) -> tuple[str, str]:
    """Classify one upper dist-info against the base set -> (status, base_ver).

    Three-valued by construction: with no base set (or a PARTIAL one that
    lacks the name) the answer is :data:`SHADOW_BASE_UNKNOWN` — we could
    not tell, and "could not tell" must never read as clean. Note the rule
    has NO version qualifier: a same-version dist-info in the upper is
    still MASKED — it shadows every FUTURE base rebuild just the same.
    """
    if base is None:
        return SHADOW_BASE_UNKNOWN, ""
    base_version = base.packages.get(name)
    if base_version is not None:
        return SHADOW_MASKED, str(base_version)
    if base.complete:
        return SHADOW_OVERLAY_ONLY, ""
    return SHADOW_BASE_UNKNOWN, ""


def _shadow_verdict(
    agent: str,
    root: Path,
    dist_infos: list[tuple[str, str, str]],
    copyups: list[str],
    strays: list[str],
    base: BasePackageSet | None,
) -> OverlayMaskVerdict:
    """Fold classified shadows into the agent-level verdict."""
    shadows = tuple(
        ShadowInstall(
            package=name,
            version=version,
            dist_info=path,
            status=status,
            base_version=base_version,
        )
        for name, version, path in dist_infos
        for status, base_version in (_classify(name, base),)
    )
    n_masked = sum(1 for s in shadows if s.status == SHADOW_MASKED)
    n_unknown = sum(1 for s in shadows if s.status == SHADOW_BASE_UNKNOWN)
    if n_masked:
        verdict, reason = VERDICT_MASKED, REASON_MASKED
        detail = (
            f"{n_masked} base-baked package(s) shadowed by an overlay-upper "
            "dist-info: "
            + ", ".join(
                f"{s.package} {s.version} (base {s.base_version})"
                for s in shadows
                if s.status == SHADOW_MASKED
            )
        )
    elif n_unknown:
        verdict, reason = VERDICT_UNKNOWN, REASON_BASE_UNKNOWN
        detail = (
            f"{n_unknown} dist-info(s) in the upper but the base package set "
            "could not settle them — NOT clean, cannot tell"
        )
    else:
        verdict, reason = VERDICT_CLEAN, REASON_OVERLAY_ONLY
        detail = (
            f"{len(shadows)} overlay-only install(s); the complete base set "
            "provides none of them"
        )
    return OverlayMaskVerdict(
        agent=agent,
        overlay_root=str(root),
        verdict=verdict,
        reason=reason,
        detail=detail,
        shadows=shadows,
        copyups=tuple(copyups),
        stray_dirs=tuple(strays),
        base_source=base.source if base is not None else "",
    )


def inspect_overlay(
    agent: str,
    overlay_root: Path | str | None,
    base_provider: Callable[[], BasePackageSet | None] | BasePackageSet | None,
    *,
    venv: str = DEFAULT_VENV,
    overlay_size: str = "",
) -> OverlayMaskVerdict:
    """The detector: tri-state masking verdict for ONE agent overlay.

    ``base_provider`` may be a :class:`BasePackageSet`, ``None`` ("could not
    read the base"), or a zero-arg callable returning either. The callable
    form is the production shape and is invoked LAZILY — only when the upper
    actually carries a dist-info — so the overwhelmingly common clean agent
    costs a pure filesystem scan and never an ``apptainer exec``.
    """
    if overlay_root is None:
        return OverlayMaskVerdict(
            agent=agent,
            overlay_root="",
            verdict=VERDICT_CLEAN,
            reason=REASON_NO_OVERLAY,
            detail="spec declares no overlay; nothing can mask the base",
        )
    root = Path(overlay_root)
    if is_image_overlay(root, overlay_size):
        return OverlayMaskVerdict(
            agent=agent,
            overlay_root=str(root),
            verdict=VERDICT_UNKNOWN,
            reason=REASON_IMAGE_OVERLAY,
            detail=(
                "loopback image overlay — its upper layer is not host-"
                "readable, so masking can NOT be ruled out from here"
            ),
        )
    if not root.is_dir():
        # Missing is UNKNOWN, not clean: we cannot distinguish "never
        # provisioned" from "we are looking on the wrong host / at the
        # wrong HOME".
        return OverlayMaskVerdict(
            agent=agent,
            overlay_root=str(root),
            verdict=VERDICT_UNKNOWN,
            reason=REASON_OVERLAY_MISSING,
            detail=f"declared overlay root {root} does not exist here",
        )
    upper_venv = root / OVERLAY_UPPER_DIRNAME / venv.lstrip("/")
    if not upper_venv.is_dir():
        # We READ the overlay root and the upper carries no venv subtree at
        # all — the fleet-normal state. This is an observed clean, not an
        # unknown: the readdir succeeded and there is nothing to mask with.
        return OverlayMaskVerdict(
            agent=agent,
            overlay_root=str(root),
            verdict=VERDICT_CLEAN,
            reason=REASON_UPPER_VENV_UNTOUCHED,
            detail=f"overlay upper carries no {venv} subtree",
        )
    try:
        dist_infos, copyups, strays = _scan_upper_venv(upper_venv)
    except OSError as exc:
        return OverlayMaskVerdict(
            agent=agent,
            overlay_root=str(root),
            verdict=VERDICT_UNKNOWN,
            reason=REASON_UPPER_UNREADABLE,
            detail=f"could not read {upper_venv}: {exc}",
        )
    if not dist_infos:
        return OverlayMaskVerdict(
            agent=agent,
            overlay_root=str(root),
            verdict=VERDICT_CLEAN,
            reason=REASON_NO_SHADOW_INSTALLS,
            detail="no dist-info in the upper site-packages",
            copyups=tuple(copyups),
            stray_dirs=tuple(strays),
        )
    base = base_provider() if callable(base_provider) else base_provider
    return _shadow_verdict(agent, root, dist_infos, copyups, strays, base)


# ---------------------------------------------------------------------------
# production glue: base-set acquisition + per-agent / fleet entry points
# ---------------------------------------------------------------------------
def _read_base_full_live(sif: Path) -> dict[str, str] | None:
    """UNFILTERED ``pip list --format=json`` of the base venv via apptainer.

    Unlike :func:`.._drift.versions.read_base_live` (scitex-* rows only,
    for the drift aggregator), masking detection needs the WHOLE base set:
    an overlay dist-info for ``click`` masks exactly as hard as one for
    ``scitex-cards``. Returns canonical ``{name: version}`` or ``None``.
    """
    cp = _apptainer_exec(sif, [f"{DEFAULT_VENV}/bin/pip", "list", "--format=json"])
    if cp is None or cp.returncode != 0 or not (cp.stdout or "").strip():
        return None
    try:
        rows = json.loads(cp.stdout)
    except json.JSONDecodeError:
        return None
    out: dict[str, str] = {}
    for row in rows if isinstance(rows, list) else []:
        try:
            out[canonical_dist_name(str(row["name"]))] = str(row["version"])
        except (KeyError, TypeError):
            continue
    return out or None


def _resolve_base_sif(config) -> Path | None:
    """The base SIF this agent runs on, or ``None`` when unresolvable."""
    ap = getattr(config, "apptainer", None)
    image = (getattr(ap, "image", "") or "").strip() if ap is not None else ""
    candidate = Path(image).expanduser() if image else None
    if candidate is not None and candidate.suffix == ".sif" and candidate.is_file():
        return candidate
    wanted = agent_base_image_name(config)
    for name, sif in discover_base_sifs():
        if name == wanted:
            return sif
    return None


def base_package_set_for(config) -> BasePackageSet | None:
    """Read the base package set for ``config``'s image, honestly labelled.

    Order: full live ``pip list`` (complete — ground truth) first, because
    this function is only ever consulted AFTER a shadow install was found
    and completeness now matters more than latency; then the baked scitex-*
    manifest (partial); then ``None`` — the caller reports UNKNOWN.
    """
    sif = _resolve_base_sif(config)
    if sif is None:
        return None
    full = _read_base_full_live(sif)
    if full is not None:
        return BasePackageSet(packages=full, complete=True, source="live")
    partial = read_base_manifest(sif)
    if partial is not None:
        return BasePackageSet(
            packages={r["package"]: r["version"] for r in partial},
            complete=False,
            source="manifest",
        )
    return None


def inspect_agent_overlay(
    name: str,
    config,
    base_provider: Callable[[], BasePackageSet | None] | None = None,
) -> OverlayMaskVerdict:
    """Per-agent entry: resolve the overlay from the SPEC, then inspect.

    Uses :func:`..runtimes._apptainer_overlay.resolve_overlay_declaration`
    — the resolver that answers "what overlay does apptainer actually get?"
    in every spelling — so the detector looks at the same path the launch
    does. ``base_provider`` is an injection seam for tests; production
    leaves it ``None`` (lazy live-then-manifest read of the agent's SIF).
    """
    ap = getattr(config, "apptainer", None)
    overlay_size = (getattr(ap, "overlay_size", "") or "") if ap is not None else ""

    def _default_provider() -> BasePackageSet | None:
        return base_package_set_for(config)

    return inspect_overlay(
        name,
        resolve_overlay_declaration(config),
        base_provider if base_provider is not None else _default_provider,
        overlay_size=overlay_size,
    )


def sweep_agent_overlays(agent_configs=None) -> list[OverlayMaskVerdict]:
    """Fleet sweep: one verdict per registered agent. Never raises.

    ``agent_configs`` is an iterable of ``(name, AgentConfig)``; ``None``
    enumerates the registry (same seam as ``sac versions``). One agent's
    failure degrades to an UNKNOWN verdict carrying the reason — a sweep
    that dies on agent 3 of 40 silently under-reports the other 37.
    """
    if agent_configs is None:
        from .._drift.versions import _load_agent_configs

        agent_configs = _load_agent_configs()
    out: list[OverlayMaskVerdict] = []
    for name, config in agent_configs:
        try:
            out.append(inspect_agent_overlay(name, config))
        except Exception as exc:  # stx-allow: fallback (reason: one agent's malformed spec must degrade to an UNKNOWN row, never kill the fleet sweep)
            logger.debug("overlay-masking: inspect failed for %s: %s", name, exc)
            out.append(
                OverlayMaskVerdict(
                    agent=name,
                    overlay_root="",
                    verdict=VERDICT_UNKNOWN,
                    reason=REASON_INSPECT_ERROR,
                    detail=f"{type(exc).__name__}: {exc}",
                )
            )
    return out
