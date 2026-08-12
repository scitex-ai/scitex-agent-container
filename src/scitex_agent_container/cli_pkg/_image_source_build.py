"""Source-bundled SIF build invocation for ``sac image build``.

Two halves used to live here; the staging half is now
``_image_build_context`` (split at the 512-line budget) and is re-exported
below so every existing import path keeps resolving. What remains is the
INVOCATION half:

    build_layer_from_source(layer, def_path, pkg_root, output_dir, ...)
        -> stages a build context under output_dir/<image>/build-context/
        -> delegates to ``scitex_container.build`` with cwd=staging_dir
        -> returns the stable boot symlink of the built SIF (or the sandbox dir)

    resolve_bootstrap_sif(layer, output_dir)
        -> the PARENT stage's SIF this stage bootstraps from, per the chain
           table in ``_image_layers`` — or None for a registry-rooted stage

scitex-container 0.3.0 exposes an atomic ``build(...)`` that accepts a
``cwd`` (build context, independent of ``output_dir``), a ``def_path``
(so out-of-tree callers whose recipes ship inside their own wheel bypass
``find_containers_dir``), and an ``image_name``. It builds to a
timestamped ``<output_dir>/<image_name>/<image_name>-<ts>.sif`` and then
atomically swaps two stable symlinks — the INNER boot path
``<output_dir>/<image_name>/<image_name>.sif`` and the TOP-level
``<output_dir>/<image_name>.sif`` (which resolves a layered .def's
``From: ./<image_name>.sif``). A failed build never touches the live
symlinks, so the prior image stays intact — no more in-place overwrite.
sac delegates to that helper rather than shelling ``apptainer build
--force`` itself; the source-bundled staging still owns the build-context
prep so the .def's relative ``%files`` + ``From: ./<parent>.sif`` resolve.
The backend abstraction in image_group.py is preserved for the non-build
verbs (sandbox / update / freeze / list / status / snapshot) which manage
already-built SIFs.

Testability follows the same save/restore pattern as ``image_group``'s
``_load_apptainer`` hook: ``_container_build`` is a module-level callable
that tests reassign to a real (no MagicMock) recording fake, so the unit
tests never shell a real apptainer.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from . import _image_chain_build, _image_layers
from ._image_build_context import (
    _COPY_IGNORE,
    _STAGED_SRC_NAME,
    _locate_bundled_sibling,
    locate_bundled_hatch_build,
    locate_bundled_pyproject,
    locate_bundled_readme,
    stage_build_context,
)

# ---------------------------------------------------------------------------
# Build invocation
# ---------------------------------------------------------------------------


def _default_container_build(
    *,
    def_path: Path,
    output_dir: Path,
    cwd: Path,
    image_name: str,
    sandbox: bool,
    force: bool,
) -> Path:
    """Default builder — delegates to scitex-container's atomic ``build``.

    scitex-container 0.3.0 builds to a timestamped
    ``<output_dir>/<image_name>/<image_name>-<ts>.sif`` and then swaps
    two stable symlinks all-at-once (the INNER boot path
    ``<output_dir>/<image_name>/<image_name>.sif`` and the TOP-level
    ``<output_dir>/<image_name>.sif`` that a layered .def's
    ``From: ./<image_name>.sif`` resolves against). A failed build leaves
    the prior live image + symlinks untouched — atomic, rollback-safe.

    ``cwd`` is the staged build context (independent of ``output_dir``),
    so the .def's relative ``%files scitex-agent-container-src ...`` and
    ``From: ./<parent>.sif`` (staged into ``cwd`` by
    :func:`stage_build_context`) resolve at build time. ``retain`` is
    omitted so scitex-container uses its config-resolved retention
    default. Returns the resolved timestamped SIF (SIF build) or the
    sandbox directory (sandbox build).
    """
    # Local import — scitex-container pulls its own deps and we don't
    # want to pay that cost on the cold ``sac image`` startup path until
    # the actual build runs.
    from scitex_container import build as _sc_build

    return _sc_build(
        def_path=def_path,
        output_dir=output_dir,
        cwd=cwd,
        image_name=image_name,
        sandbox=sandbox,
        force=force,
    )


# Module-level overridable reference — same swap-and-restore pattern as
# image_group._load_apptainer. Tests reassign this to a real recording
# callable (no MagicMock) so the unit suite never shells a real apptainer
# or imports scitex-container.
_container_build: Callable[..., Path] = _default_container_build


def build_layer_from_source(
    *,
    layer: str,
    def_path: Path,
    pkg_root: Path,
    output_dir: Path,
    sandbox: bool = False,
    force: bool = True,
    bootstrap_sif: Path | None = None,
) -> Path:
    """Build a sac SIF (or sandbox) from a .def that bundles its own source.

    Stages a build context under ``output_dir/<image>/build-context/``
    (so the .def's relative ``%files scitex-agent-container-src ...``
    resolves to the bundled source copy, and a layered .def's
    ``From: ./<parent>.sif`` resolves to the symlinked prerequisite SIF),
    then delegates to :func:`scitex_container.build` with that staging dir
    as the build context (``cwd``). The build is atomic: it lands a
    timestamped SIF and swaps stable symlinks all-at-once, leaving the
    prior image intact on failure.

    Parameters
    ----------
    layer : str
        Stage name (``01-system-deps`` / ``02-python-pkgs`` / ``03-base`` /
        ``04-scitex`` / ``proxy``), or a legacy alias (``base`` /
        ``scitex``). Canonicalised HERE, so an alias caller lands in the
        same artifact dir as the canonical one rather than creating a
        parallel ``sac-base/`` tree beside ``sac-03-base/``.
    def_path : Path
        Source .def file. Copied (not modified) into the staging dir.
    pkg_root : Path
        Package source root. Copied into the staging dir as
        ``scitex-agent-container-src/``.
    output_dir : Path
        Containers dir (typically ``~/.scitex/agent-container/containers``).
        scitex-container lands the artefact under ``<output_dir>/<image>/``
        and publishes the stable ``<output_dir>/<image>/<image>.sif`` boot
        symlink.
    sandbox : bool
        If True, build a writable sandbox directory rather than a SIF.
    force : bool
        Force a rebuild even when the recipe hash is unchanged.
    bootstrap_sif : Path | None
        Optional prerequisite SIF for a layered .def. Forwarded to
        :func:`stage_build_context` which symlinks it into the staging
        dir under its own name so the .def's ``Bootstrap: localimage`` /
        ``From: ./<name>.sif`` line resolves against the build context
        (``cwd``) at build time. ``None`` for registry-rooted stages
        (``01-system-deps``, ``proxy``). Required for every layered
        stage; omitting it produces a half-staged context and apptainer
        FATAL's on "no such file or directory".

    Returns
    -------
    Path
        For a SIF build, the STABLE inner boot symlink
        (``<output_dir>/<image>/<image>.sif``) — what callers
        (and downstream stages' ``bootstrap_sif``) resolve against,
        unchanged from the pre-atomic layout. For a sandbox build, the
        sandbox directory (``<output_dir>/<image>/<image>.sandbox``).

    Raises
    ------
    RuntimeError
        Propagated from :func:`scitex_container.build` if the underlying
        apptainer build fails. The live image + symlinks are left intact.
    FileNotFoundError
        Propagated from :func:`stage_build_context` if inputs are missing.
    """
    image_name = _image_layers.resolve(layer).image
    artifact_dir = output_dir / image_name
    artifact_dir.mkdir(parents=True, exist_ok=True)

    staging_dir = artifact_dir / "build-context"
    staged_def = stage_build_context(
        pkg_root, def_path, staging_dir, bootstrap_sif=bootstrap_sif
    )

    result = _container_build(
        def_path=staged_def,
        output_dir=output_dir,
        cwd=staging_dir,
        image_name=image_name,
        sandbox=sandbox,
        force=force,
    )

    if sandbox:
        # Sandbox: scitex-container returns the sandbox dir itself
        # (<artifact_dir>/<image_name>.sandbox); no symlink layer.
        return Path(result)
    # SIF: scitex-container returns the RESOLVED timestamped real SIF.
    # Callers (and the next stage's bootstrap_sif) want the STABLE inner
    # boot symlink, which is layout-invariant across rebuilds.
    return artifact_dir / f"{image_name}.sif"


class BootstrapSifMissing(FileNotFoundError):
    """Raised when a layered build's prerequisite SIF is absent.

    Carries the fail-loud remediation text the CLI surfaces verbatim, so
    the stage→prerequisite policy lives with the source-build path rather
    than inline in the ``sac image build`` command.
    """


def resolve_bootstrap_sif(layer: str, output_dir: Path) -> Path | None:
    """Return the prerequisite SIF a layered ``.def`` bootstraps off.

    A layered .def starts ``From: ./<parent-image>.sif`` — a path RELATIVE to
    the build-context dir. The prerequisite is the PARENT stage's STABLE inner
    boot symlink, ``<output_dir>/<parent-image>/<parent-image>.sif`` (a symlink
    to the live timestamped SIF under scitex-container 0.3.0's atomic layout).
    :func:`build_layer_from_source` symlinks it into the staging dir so
    apptainer's relative ``From:`` resolves at build time.

    THE PARENT COMES FROM THE CHAIN TABLE (``_image_layers``), not from a
    hardcoded name. This function used to read ``if layer != "scitex": return
    None`` with ``sac-base/sac-base.sif`` spelled inline — one of four
    independent spellings of the chain. With four stages that hardcoding is not
    merely brittle, it is wrong for two of them.

    Returns ``None`` for stages that bootstrap off a REGISTRY image rather than
    a prior SIF: ``01-system-deps`` (the bottom of the chain) and ``proxy``
    (deliberately off the chain entirely).

    Accepts a canonical stage name or a legacy alias (``base`` / ``scitex``).

    Raises
    ------
    BootstrapSifMissing
        When a layered build is requested but the prerequisite SIF has not
        been built. Fails loud BEFORE staging so apptainer never FATAL's
        on a half-staged context (the 2026-06-07 cohort-A rebuild stall).
        The message names the missing path AND the remediation — both the
        single-parent build and the whole-chain form.
    """
    resolved = _image_layers.resolve(layer)
    parent = _image_layers.parent_of(resolved.name)
    if parent is None:
        return None
    bootstrap_sif = output_dir / parent.image / f"{parent.image}.sif"
    if not bootstrap_sif.is_file():
        raise BootstrapSifMissing(
            _image_chain_build.missing_parent_message(resolved, bootstrap_sif)
        )
    return bootstrap_sif


__all__ = [
    "stage_build_context",
    "build_layer_from_source",
    "locate_bundled_hatch_build",
    "locate_bundled_pyproject",
    "locate_bundled_readme",
    "resolve_bootstrap_sif",
    "BootstrapSifMissing",
    "_COPY_IGNORE",
    "_STAGED_SRC_NAME",
    "_locate_bundled_sibling",
    "_default_container_build",
    "_container_build",
]
