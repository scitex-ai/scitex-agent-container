"""scitex-* version introspection across the image layers sac owns.

scitex-dev's ``ecosystem check-versions`` reports scitex-* package
versions across 6 layers (PyPI, GitHub, both hosts' venvs, CI, editable)
but cannot see the 2 layers only sac owns:

* **base-image** — the shared ``/opt/venv-sac`` venv baked into each base
  SIF (``sac-base`` / ``sac-scitex``). One set per base image, ``agent="*"``.
* **agent-overlay** — the scitex-* packages an individual agent's writable
  overlay *adds or overrides* vs its base venv. Usually empty — most agents
  run straight on the shared base venv, so their overlay upper carries no
  extra site-packages.

This module exposes both as a flat list of rows so scitex-dev's drift
aggregator can fold sac's 2 layers in with the other 6. Each row::

    {agent, layer, image, package, version, source}

* ``layer``  ∈ {"base-image", "agent-overlay"}
* ``image``  — the base SIF name on EVERY row (disambiguates multiple base
  images and maps agent → base).
* ``source`` ∈ {"manifest", "live"} — "manifest" reads a baked version
  manifest (near-zero cost, the routine path); "live" is produced by an
  actual ``pip list`` / site-packages scan (ground truth).

sac emits RAW base + overlay rows; it does NOT compute the "effective"
(overlay-else-base) view — that derivation is the aggregator's job.

Resilience doctrine (mirrors :mod:`._local`): never raise. A missing
apptainer binary, an unbuilt SIF, an unreadable overlay — all degrade to
"skip that image / agent", never a crash. ``sac versions --json --live``
must always emit valid JSON.
"""

from __future__ import annotations

import json
import logging
import subprocess
from pathlib import Path
from typing import Iterable

logger = logging.getLogger(__name__)

# Path of the baked manifest INSIDE a base SIF, written by the apptainer
# ``.def`` ``%post`` step (``containers/apptainer-*.def``). Read via
# ``apptainer exec <sif> cat <this>`` — near-zero cost vs a full pip list.
BAKED_MANIFEST_PATH = "/opt/scitex-versions.json"

# The venv every sac SIF ships its Python stack in (see apptainer-*.def:
# ``python3 -m venv /opt/venv-sac``).
DEFAULT_VENV = "/opt/venv-sac"

# Base image assumed when an agent's spec leaves ``apptainer.image`` empty
# (ApptainerSpec.image docstring: "Empty = fall back to the default
# sac-scitex SIF").
DEFAULT_BASE_IMAGE = "sac-scitex"

# Per-agent overlay manifest filename, recorded next to the overlay dir at
# overlay-setup time (see :func:`record_overlay_manifest`).
OVERLAY_MANIFEST_NAME = "scitex-overlay-versions.json"

# Apptainer directory overlays keep the writable layer under ``upper/``.
_OVERLAY_UPPER = "upper"

# Overridable so a caller / test can point at a different runtime binary.
_APPTAINER_BIN = "apptainer"
_EXEC_TIMEOUT_S = 30

LAYER_BASE = "base-image"
LAYER_OVERLAY = "agent-overlay"
SOURCE_MANIFEST = "manifest"
SOURCE_LIVE = "live"


# ---------------------------------------------------------------------------
# scitex-* filtering + normalisation (pure helpers)
# ---------------------------------------------------------------------------
def is_scitex_package(name: str) -> bool:
    """True for ``scitex`` and any ``scitex-*`` / ``scitex_*`` dist name."""
    n = (name or "").strip().lower().replace("_", "-")
    return n == "scitex" or n.startswith("scitex-")


def _canonical(name: str) -> str:
    return (name or "").strip().lower().replace("_", "-")


def normalize_pkg_list(raw) -> list[dict]:
    """Normalise a ``pip list --format=json`` list (or a baked manifest)
    down to a sorted ``[{"package", "version"}]`` of scitex-* rows only.

    Accepts either pip's ``[{"name", "version"}, ...]`` shape or a plain
    ``{name: version}`` mapping (tolerant of hand-written manifests).
    """
    if isinstance(raw, dict):
        items: Iterable = ({"name": k, "version": v} for k, v in raw.items())
    else:
        items = raw or []
    out: dict[str, str] = {}
    for row in items:
        try:
            name = str(row["name"])
            version = str(row["version"])
        except (KeyError, TypeError):
            continue
        if is_scitex_package(name):
            out[_canonical(name)] = version
    return [{"package": p, "version": out[p]} for p in sorted(out)]


def _pkg_map(rows: list[dict]) -> dict[str, str]:
    return {r["package"]: r["version"] for r in rows}


def overlay_adds(overlay_pkgs: list[dict], base_map: dict[str, str]) -> list[dict]:
    """Rows in ``overlay_pkgs`` that ADD or OVERRIDE vs ``base_map``.

    A row survives when its ``(package, version)`` is absent from the base
    set OR present at a different version — i.e. the overlay genuinely
    changed it. Packages the overlay carries identically to base are
    dropped (no drift to report).
    """
    return [r for r in overlay_pkgs if base_map.get(r["package"]) != r["version"]]


# ---------------------------------------------------------------------------
# apptainer exec seam (real subprocess; PATH-driven so tests use a shim)
# ---------------------------------------------------------------------------
def _apptainer_exec(sif, inner_argv: list[str], *, timeout: int = _EXEC_TIMEOUT_S):
    """Run ``apptainer exec <sif> <inner...>``; return CompletedProcess or None.

    Real subprocess — no mocks. PATH resolves the ``apptainer`` binary; tests
    install a fake ``apptainer`` on PATH. Never raises: a missing binary,
    timeout, or OS error degrades to ``None`` (the caller treats that the
    same as "could not read").
    """
    try:
        return subprocess.run(
            [_APPTAINER_BIN, "exec", str(sif), *inner_argv],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("apptainer exec failed for %s: %s", sif, exc)
        return None


def _parse_json_stdout(cp) -> list[dict] | None:
    if cp is None or cp.returncode != 0 or not (cp.stdout or "").strip():
        return None
    try:
        return normalize_pkg_list(json.loads(cp.stdout))
    except json.JSONDecodeError:
        return None


def read_base_manifest(sif) -> list[dict] | None:
    """Near-zero-cost read of the baked ``/opt/scitex-versions.json``.

    Returns the scitex-* rows, or ``None`` when the manifest is absent
    (older images not yet rebuilt) so the caller falls back to live.
    """
    return _parse_json_stdout(_apptainer_exec(sif, ["cat", BAKED_MANIFEST_PATH]))


def read_base_live(sif, venv: str = DEFAULT_VENV) -> list[dict] | None:
    """Ground-truth read via ``<venv>/bin/pip list --format=json`` in the SIF."""
    return _parse_json_stdout(
        _apptainer_exec(sif, [f"{venv}/bin/pip", "list", "--format=json"])
    )


# ---------------------------------------------------------------------------
# base-image layer
# ---------------------------------------------------------------------------
def default_containers_dir() -> Path:
    """sac's built-artifact dir (SSoT: ``cli_pkg.image_group._CONTAINERS_DIR``)."""
    return Path.home() / ".scitex" / "agent-container" / "containers"


def discover_base_sifs(containers_dir=None) -> list[tuple[str, Path]]:
    """Discover base SIFs as sorted ``(image_name, sif_path)`` tuples.

    Mirrors ``sac image list`` discovery. The atomic builder lands, for each
    layer, BOTH a top-level ``<containers>/sac-<layer>.sif`` symlink (what a
    layered ``.def``'s ``From: ./sac-base.sif`` and the ``sac create``
    template resolve against) AND a nested ``<containers>/sac-<layer>/
    sac-<layer>.sif`` boot symlink, both pointing at the same timestamped
    target. We glob both, key by the SIF stem (e.g. ``"sac-base"``,
    ``"sac-scitex"``), and emit ONE entry per name — the top-level symlink
    wins so we return the canonical path agents reference. Falls back to any
    ``*.sif`` inside a per-layer subdir when no stable symlink exists.
    """
    root = Path(containers_dir) if containers_dir else default_containers_dir()
    if not root.is_dir():
        return []
    found: dict[str, Path] = {}
    # 1. Top-level symlinks (canonical, agent-referenced) win.
    for sif in sorted(root.glob("*.sif")):
        found.setdefault(sif.stem, sif)
    # 2. Nested per-layer dirs fill in anything not already seen.
    for sub in sorted(p for p in root.iterdir() if p.is_dir()):
        stable = sub / f"{sub.name}.sif"
        if stable.exists():
            found.setdefault(sub.name, stable)
            continue
        others = sorted(sub.glob("*.sif"))
        if others:
            found.setdefault(sub.name, others[0])
    return [(name, found[name]) for name in sorted(found)]


def base_image_rows(containers_dir=None, *, live: bool = False):
    """Return ``(rows, base_maps)`` for the base-image layer.

    ``base_maps[image_name] = {package: version}`` is fed to the overlay
    diff so agent-overlay rows only show genuine adds/overrides.

    When ``live`` is False the routine manifest path is tried first
    (source="manifest") and falls back to a live ``pip list`` per image.
    ``live=True`` forces the ground-truth pip-list read (source="live") —
    the path that produces real output on images not yet rebuilt with the
    baked manifest.
    """
    rows: list[dict] = []
    base_maps: dict[str, dict] = {}
    for name, sif in discover_base_sifs(containers_dir):
        pkgs = None
        source = SOURCE_LIVE
        if not live:
            pkgs = read_base_manifest(sif)
            if pkgs is not None:
                source = SOURCE_MANIFEST
        if pkgs is None:
            pkgs = read_base_live(sif)
            source = SOURCE_LIVE
        if pkgs is None:
            logger.warning(
                "versions: could not read scitex-* versions from base image "
                "%s (%s) — apptainer missing or SIF unbuilt?",
                name,
                sif,
            )
            base_maps[name] = {}
            continue
        base_maps[name] = _pkg_map(pkgs)
        for row in pkgs:
            rows.append(
                {
                    "agent": "*",
                    "layer": LAYER_BASE,
                    "image": name,
                    "package": row["package"],
                    "version": row["version"],
                    "source": source,
                }
            )
    return rows, base_maps


# ---------------------------------------------------------------------------
# agent-overlay layer
# ---------------------------------------------------------------------------
def agent_base_image_name(config, default: str = DEFAULT_BASE_IMAGE) -> str:
    """Map an ``AgentConfig`` to the base SIF *name* it runs on.

    ``apptainer.image`` may be empty (→ default), a path
    (``/x/sac-base.sif`` → ``sac-base``), or a ``docker://`` ref
    (``docker://org/sac-scitex:tag`` → ``sac-scitex``).
    """
    ap = getattr(config, "apptainer", None)
    image = (getattr(ap, "image", "") or "").strip() if ap is not None else ""
    if not image:
        return default
    ref = image.split("://", 1)[-1]
    stem = Path(ref).name
    for suffix in (".sif", ".sandbox"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
    stem = stem.split(":", 1)[0]  # strip a docker tag
    return stem or default


def _overlay_dir(config) -> Path | None:
    """Resolve the agent's directory-form overlay path, or None.

    Reuses ``runtimes._to_home_overlay._resolve_overlay_dir`` (the exact
    resolver the launch path uses) so there is no second source of truth
    for "where is this agent's overlay".
    """
    from ..runtimes._to_home_overlay import _resolve_overlay_dir

    return _resolve_overlay_dir(config)


def _overlay_venv_root(overlay_dir: Path) -> Path:
    """Host-side path where writes to ``/opt/venv-sac`` land in the overlay."""
    return overlay_dir / _OVERLAY_UPPER / DEFAULT_VENV.lstrip("/")


def _overlay_manifest_path(overlay_dir: Path) -> Path:
    return overlay_dir / OVERLAY_MANIFEST_NAME


def scan_venv_scitex(venv_root: Path) -> list[dict]:
    """Scan a host-visible venv dir for scitex-* ``*.dist-info``.

    Reads ``<venv_root>/lib/python*/site-packages/*.dist-info`` directory
    names (``<Name>-<Version>.dist-info``) — no subprocess, since an agent's
    overlay upperdir is a plain host directory. Returns ``[]`` when the venv
    root is absent (the common case: agents share the read-only base venv,
    so their overlay upper carries no ``/opt/venv-sac``).
    """
    if not venv_root.is_dir():
        return []
    out: dict[str, str] = {}
    for site in venv_root.glob("lib/python*/site-packages"):
        for dist_info in site.glob("*.dist-info"):
            stem = dist_info.name[: -len(".dist-info")]
            name, _, version = stem.rpartition("-")
            if name and version and is_scitex_package(name):
                out[_canonical(name)] = version
    return [{"package": p, "version": out[p]} for p in sorted(out)]


def _read_overlay_pkgs(overlay_dir: Path, *, live: bool) -> tuple[list[dict], str]:
    """Return ``(overlay_scitex_pkgs, source)`` for one agent's overlay."""
    if not live:
        manifest = _overlay_manifest_path(overlay_dir)
        if manifest.is_file():
            try:
                return (
                    normalize_pkg_list(json.loads(manifest.read_text())),
                    SOURCE_MANIFEST,
                )
            except (OSError, json.JSONDecodeError) as exc:
                logger.debug("versions: bad overlay manifest %s: %s", manifest, exc)
    return scan_venv_scitex(_overlay_venv_root(overlay_dir)), SOURCE_LIVE


def agent_overlay_rows(agent_configs, base_maps, *, live: bool = False) -> list[dict]:
    """Overlay rows for ``agent_configs`` (iterable of ``(name, config)``).

    Emits ONLY the scitex-* packages an agent's overlay adds or overrides
    vs the base image it runs on. Agents with no overlay, or whose overlay
    matches base, contribute no rows.
    """
    rows: list[dict] = []
    for name, config in agent_configs:
        try:
            overlay_dir = _overlay_dir(config)
        except Exception as exc:  # stx-allow: fallback (reason: never crash the sweep on one agent's malformed spec)
            logger.debug("versions: overlay resolve failed for %s: %s", name, exc)
            continue
        if overlay_dir is None:
            continue
        image = agent_base_image_name(config)
        overlay_pkgs, source = _read_overlay_pkgs(overlay_dir, live=live)
        for row in overlay_adds(overlay_pkgs, base_maps.get(image, {})):
            rows.append(
                {
                    "agent": name,
                    "layer": LAYER_OVERLAY,
                    "image": image,
                    "package": row["package"],
                    "version": row["version"],
                    "source": source,
                }
            )
    return rows


# ---------------------------------------------------------------------------
# record side — per-agent overlay manifest (near-zero manifest path forward)
# ---------------------------------------------------------------------------
def record_overlay_manifest(config) -> Path | None:
    """Record the overlay's scitex-* set to ``<overlay>/<manifest>``.

    Wired into the overlay-setup path (``runtimes._to_home_overlay
    .deploy_to_home_overlay``) so a later ``sac versions`` reads it as
    ``source="manifest"`` instead of re-scanning. Captures whatever the
    overlay venv holds at record time (empty for a fresh overlay). Returns
    the written path or ``None``; best-effort, never raises.
    """
    try:
        overlay_dir = _overlay_dir(config)
        if overlay_dir is None:
            return None
        pkgs = scan_venv_scitex(_overlay_venv_root(overlay_dir))
        overlay_dir.mkdir(parents=True, exist_ok=True)
        path = _overlay_manifest_path(overlay_dir)
        path.write_text(json.dumps(pkgs, indent=2))
        return path
    except Exception as exc:  # stx-allow: fallback (reason: provenance recording must never break a launch)
        logger.debug("versions: record_overlay_manifest failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# top-level assembly
# ---------------------------------------------------------------------------
def _load_agent_configs() -> list[tuple[str, object]]:
    """Enumerate every discoverable agent as ``(name, AgentConfig)``.

    Best-effort: a spec that fails to load is skipped (logged at debug),
    never fatal — the sweep must survive one malformed agent.
    """
    from ..config import load_config, resolve_config
    from ..config._resolve import enumerate_agent_names

    out: list[tuple[str, object]] = []
    try:
        names = enumerate_agent_names()
    except Exception as exc:  # stx-allow: fallback (reason: an ambiguous/missing registry yields zero agents, not a crash)
        logger.debug("versions: enumerate_agent_names failed: %s", exc)
        return out
    for name in names:
        try:
            out.append((name, load_config(resolve_config(name))))
        except Exception as exc:  # stx-allow: fallback (reason: skip one bad spec, keep sweeping)
            logger.debug("versions: skip agent %s (config load failed): %s", name, exc)
    return out


def collect_versions(
    *, live: bool = False, containers_dir=None, agent_configs=None
) -> list[dict]:
    """The flat ``[{agent, layer, image, package, version, source}]`` list.

    ``live`` forces ground-truth pip-list / venv-scan reads (source="live").
    ``containers_dir`` / ``agent_configs`` are injectable seams for tests;
    production leaves them ``None`` (real containers dir + real registry).
    """
    rows, base_maps = base_image_rows(containers_dir, live=live)
    if agent_configs is None:
        agent_configs = _load_agent_configs()
    rows.extend(agent_overlay_rows(agent_configs, base_maps, live=live))
    return rows


__all__ = [
    "BAKED_MANIFEST_PATH",
    "DEFAULT_BASE_IMAGE",
    "DEFAULT_VENV",
    "LAYER_BASE",
    "LAYER_OVERLAY",
    "OVERLAY_MANIFEST_NAME",
    "SOURCE_LIVE",
    "SOURCE_MANIFEST",
    "agent_base_image_name",
    "agent_overlay_rows",
    "base_image_rows",
    "collect_versions",
    "discover_base_sifs",
    "is_scitex_package",
    "normalize_pkg_list",
    "overlay_adds",
    "read_base_live",
    "read_base_manifest",
    "record_overlay_manifest",
    "scan_venv_scitex",
]
