"""Source-bundled SIF build helper for ``sac image build``.

The shipped Apptainer .def files (containers/apptainer-{base,scitex,proxy}.def)
install sac from a RELATIVE path that lives next to the .def at build time:

    %files
        scitex-agent-container-src /opt/scitex-agent-container-src

    %post
        ...
        uv pip install /opt/scitex-agent-container-src
        ...

This pins the in-SIF sac version to the source tree that shipped the .def —
no ``git+...@main`` snapshot drift. Whatever sac source contains the .def is
what lands in the SIF.

For that to work, ``apptainer build`` must run with its CWD set to a
directory that contains BOTH the .def and a ``scitex-agent-container-src/``
copy of the package root. This module owns that staging step:

    stage_build_context(pkg_root, def_path, dest_dir)
        -> creates dest_dir/<def-name>, dest_dir/scitex-agent-container-src/
        -> returns the staged .def path

    build_layer_from_source(layer, def_path, pkg_root, output_dir, ...)
        -> stages a build context under output_dir/sac-<layer>/build-context/
        -> delegates to ``scitex_container.build`` with cwd=staging_dir
        -> returns the stable boot symlink of the built SIF (or the sandbox dir)

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
sac now delegates to that helper rather than shelling ``apptainer build
--force`` itself; the source-bundled staging (this module) still owns the
build-context prep so the .def's relative ``%files`` +
``From: ./sac-base.sif`` resolve. The backend abstraction in
image_group.py is preserved for the non-build verbs (sandbox / update /
freeze / list / status / snapshot) which manage already-built SIFs.

Testability follows the same save/restore pattern as ``image_group``'s
``_load_apptainer`` hook: ``_container_build`` is a module-level callable
that tests reassign to a real (no MagicMock) recording fake, so the unit
tests never shell a real apptainer.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable

# Layer topology lives in its own module (see _image_layer_chain). Re-exported
# here — and listed in this module's ``__all__`` — because callers and tests
# have imported these two names from _image_source_build since before the
# stack grew past one link; moving the code should not move the import.
from ._image_layer_chain import (  # noqa: F401  (re-export)
    BootstrapSifMissing,
    resolve_bootstrap_sif,
)

# ---------------------------------------------------------------------------
# Staging
# ---------------------------------------------------------------------------

# Source-tree subtrees that have no business inside the SIF. ``__pycache__``
# is per-Python-version stale; ``.pytest_cache`` / ``.mypy_cache`` / ``htmlcov``
# are dev-loop artefacts; ``.git`` is the operator's local state; tests/ and
# docs/ are not under src/ so they're not at risk via _RECIPES_DIR.parent
# (which IS the package root, not the repo root).
_COPY_IGNORE = shutil.ignore_patterns(
    "__pycache__",
    "*.pyc",
    "*.pyo",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "htmlcov",
    ".coverage",
    ".coverage.*",
    ".DS_Store",
)


# Where the staged source sibling-copy lives inside the staging dir.
# The .def files reference this exact name in their %files section, so
# changing it requires changing the .def files in lockstep.
_STAGED_SRC_NAME = "scitex-agent-container-src"


def _locate_bundled_sibling(
    pkg_root: Path, name: str, *, editable_rel: str | None = None
) -> Path:
    """Return the path to a wheel-bundled or repo-root file by name.

    Resolution order — both supported because sac is installed BOTH
    ways in real use:

      1. ``pkg_root/_bundled/<name>`` — wheel install. The wheel
         ships the file via ``[tool.hatch.build.targets.wheel.
         force-include]`` in the repo's own pyproject.toml.
      2. ``pkg_root.parent.parent/<editable_rel or name>`` — editable
         install (``pip install -e .``). The package is at
         ``<repo>/src/scitex_agent_container/``; ``parent.parent``
         walks up through ``src/`` to the repo root.

    ``editable_rel`` exists because a file's slot in the wheel's FLAT
    ``_bundled/`` dir need not mirror its path in the repo:
    ``hatch_build.py`` bundles to ``_bundled/hatch_build.py`` but lives
    at ``<repo>/src/hatch_build.py``. Defaults to ``name`` (the
    repo-root case: pyproject.toml, README.md).

    Raises
    ------
    FileNotFoundError
        If neither location holds the file. Hard failure — the SIF
        build can't produce a pip-installable source tree without it.
        No silent fallback.
    """
    bundled = pkg_root / "_bundled" / name
    if bundled.is_file():
        return bundled
    editable_repo = pkg_root.parent.parent / (editable_rel or name)
    if editable_repo.is_file():
        return editable_repo
    raise FileNotFoundError(
        f"could not locate {name} for source-bundled SIF build. "
        f"checked:\n  - {bundled} (wheel install / force-included)\n"
        f"  - {editable_repo} (editable install / repo root)\n"
        f"ensure the wheel ships {name} under _bundled/ or run "
        "from an editable install."
    )


def locate_bundled_pyproject(pkg_root: Path) -> Path:
    """Return the pyproject.toml that ships with this package install.

    See :func:`_locate_bundled_sibling` for the resolution order.
    """
    return _locate_bundled_sibling(pkg_root, "pyproject.toml")


def locate_bundled_readme(pkg_root: Path) -> Path:
    """Return the README.md that ships with this package install.

    pyproject.toml declares ``readme = "README.md"`` so hatchling
    needs the file alongside pyproject.toml when ``pip install`` runs
    on the staged tree. See :func:`_locate_bundled_sibling`.
    """
    return _locate_bundled_sibling(pkg_root, "README.md")


def locate_bundled_hatch_build(pkg_root: Path) -> Path:
    """Return the custom hatchling BUILD HOOK pyproject.toml declares.

    pyproject wires ``[tool.hatch.build.targets.*.hooks.custom] path =
    "src/hatch_build.py"``, and hatchling resolves that path RELATIVE TO
    THE TREE BEING BUILT. The staged tree IS that tree (the .def runs
    ``uv pip install /opt/scitex-agent-container-src``), so the hook must
    be staged next to pyproject.toml or the backend dies before reading
    one line of source: ``OSError: Build script does not exist:
    src/hatch_build.py``. Not hypothetical — that is how EVERY SIF build
    failed from the moment the hook landed.

    The hook is a BUILD input (same category as pyproject.toml/README.md),
    not a runtime module, so the wheel carries it in the inert
    ``_bundled/`` data dir — no ``__init__.py``, never importable as
    ``scitex_agent_container.*`` — which keeps hatch_build.py's own rule
    that an ``import hatchling`` module never reaches the runtime path.

    Editable fallback is ``<repo>/src/hatch_build.py``, not the repo
    root — hence ``editable_rel``. See :func:`_locate_bundled_sibling`.
    """
    return _locate_bundled_sibling(
        pkg_root, "hatch_build.py", editable_rel="src/hatch_build.py"
    )


def stage_build_context(
    pkg_root: Path,
    def_path: Path,
    dest_dir: Path,
    *,
    bootstrap_sif: Path | None = None,
) -> Path:
    """Stage a build-context dir for source-bundled apptainer builds.

    Layout produced (``X = dest_dir / "scitex-agent-container-src"``)::

        dest_dir/
            <def-name>.def                     # copy of def_path
            <bootstrap_sif.name>               # symlink to bootstrap_sif (if any)
            scitex-agent-container-src/        # pip-installable source tree
                pyproject.toml                 # from locate_bundled_pyproject
                src/
                    hatch_build.py             # pyproject's hooks.custom path
                    scitex_agent_container/    # copy of pkg_root contents

    The ``src/scitex_agent_container/`` layout matches what
    pyproject.toml's ``[tool.hatch.build.targets.wheel].packages``
    declares, so ``pip install <X>`` resolves the same package that
    the wheel ships — but pinned to the source tree that shipped this
    .def.

    The staging dir is reset (rm -rf'd) before each call so a stale
    half-built tree from a previous failed build can't silently mix
    renamed/moved modules into the next build.

    Parameters
    ----------
    pkg_root : Path
        The installed ``scitex_agent_container`` package directory
        (``Path(scitex_agent_container.__file__).parent`` at runtime).
    def_path : Path
        The .def file to stage. Must exist and be a file.
    dest_dir : Path
        The staging directory. Created (or reset) by this function.
    bootstrap_sif : Path | None
        Optional path to a prerequisite SIF that the .def's
        ``Bootstrap: localimage`` / ``From: ./<name>.sif`` line
        references. When set, the SIF is symlinked into ``dest_dir``
        under its own filename so apptainer's relative ``From: ./...``
        resolves at build time. The symlink uses the absolute resolved
        target path so it survives the staging dir's rmtree-on-next-
        build lifecycle. ``None`` for top-of-stack defs (``Bootstrap:
        docker`` / ``From: ubuntu:24.04`` etc.). Required for layered
        defs like ``apptainer-scitex.def`` (``From: ./sac-base.sif``);
        omitting it produces a half-staged build context that
        apptainer FATAL's on with "no such file or directory" — that
        was the bug behind the 2026-06-07 cohort-A rebuild stall.

    Returns
    -------
    Path
        The path of the staged .def file (``dest_dir / def_path.name``).
        Pass this to ``apptainer build`` after setting cwd to ``dest_dir``.

    Raises
    ------
    FileNotFoundError
        If ``pkg_root`` / ``def_path`` / pyproject.toml / (when set)
        ``bootstrap_sif`` is missing.
    NotADirectoryError
        If ``pkg_root`` exists but isn't a directory.
    """
    if not def_path.is_file():
        raise FileNotFoundError(f"recipe not found: {def_path}")
    if not pkg_root.exists():
        raise FileNotFoundError(f"package source not found: {pkg_root}")
    if not pkg_root.is_dir():
        raise NotADirectoryError(f"package source is not a directory: {pkg_root}")

    # Resolve pyproject.toml + README.md + hatch_build.py + (when set)
    # bootstrap_sif BEFORE wiping dest_dir so a missing-file failure
    # doesn't strand the operator with a half-staged tree.
    pyproject_src = locate_bundled_pyproject(pkg_root)
    readme_src = locate_bundled_readme(pkg_root)
    hatch_build_src = locate_bundled_hatch_build(pkg_root)
    if bootstrap_sif is not None and not bootstrap_sif.is_file():
        raise FileNotFoundError(
            f"bootstrap SIF not found: {bootstrap_sif} — build the prerequisite "
            "layer first (e.g. `sac image build base` before `sac image build "
            "scitex`), then retry."
        )

    # Reset the staging dir. A prior failed build can leave a partial
    # tree behind; copying on top of it would silently mix old + new
    # files (e.g. a renamed module would have both names present in
    # the SIF). Cheap: it's user-state under ~/.scitex.
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    staged_def = dest_dir / def_path.name
    shutil.copy2(def_path, staged_def)

    # Stage the prerequisite SIF (layered build). Use a symlink to the
    # absolute resolved target: instant (3GB base SIF would otherwise
    # cost ~30s to copy on SSD, longer on spinning), and apptainer
    # follows symlinks for the ``Bootstrap: localimage`` / ``From: .
    # /<name>.sif`` reference at build time. Absolute target means the
    # link stays valid across cwd changes during the build invocation.
    if bootstrap_sif is not None:
        link_path = dest_dir / bootstrap_sif.name
        link_path.symlink_to(bootstrap_sif.resolve())

    # The staged pip-installable source tree:
    #   <staged_src>/pyproject.toml
    #   <staged_src>/README.md           (pyproject's readme=)
    #   <staged_src>/src/hatch_build.py  (pyproject's hooks.custom path)
    #   <staged_src>/src/scitex_agent_container/...
    #
    # EVERY path pyproject NAMES must be staged, not just the package:
    # the PEP-517 backend reads pyproject FIRST and resolves its declared
    # paths against the staged root.
    staged_src = dest_dir / _STAGED_SRC_NAME
    staged_src.mkdir()
    shutil.copy2(pyproject_src, staged_src / "pyproject.toml")
    shutil.copy2(readme_src, staged_src / "README.md")
    pkg_dest = staged_src / "src" / "scitex_agent_container"
    pkg_dest.parent.mkdir(parents=True)
    shutil.copy2(hatch_build_src, staged_src / "src" / "hatch_build.py")
    shutil.copytree(pkg_root, pkg_dest, ignore=_COPY_IGNORE)

    return staged_def


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
    ``From: ./sac-base.sif`` (staged into ``cwd`` by
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

    Stages a build context under ``output_dir/sac-<layer>/build-context/``
    (so the .def's relative ``%files scitex-agent-container-src ...``
    resolves to the bundled source copy, and a layered .def's
    ``From: ./sac-base.sif`` resolves to the symlinked prerequisite SIF),
    then delegates to :func:`scitex_container.build` with that staging dir
    as the build context (``cwd``). The build is atomic: it lands a
    timestamped SIF and swaps stable symlinks all-at-once, leaving the
    prior image intact on failure.

    Parameters
    ----------
    layer : str
        Layer name (``base`` / ``scitex`` / ``proxy``). Maps to the
        ``sac-<layer>`` image name (per-image subdir + artefact stem).
    def_path : Path
        Source .def file. Copied (not modified) into the staging dir.
    pkg_root : Path
        Package source root. Copied into the staging dir as
        ``scitex-agent-container-src/``.
    output_dir : Path
        Containers dir (typically ``~/.scitex/agent-container/containers``).
        scitex-container lands the artefact under
        ``<output_dir>/sac-<layer>/`` and publishes the stable
        ``<output_dir>/sac-<layer>/sac-<layer>.sif`` boot symlink.
    sandbox : bool
        If True, build a writable sandbox directory rather than a SIF.
    force : bool
        Force a rebuild even when the recipe hash is unchanged.
    bootstrap_sif : Path | None
        Optional prerequisite SIF for a layered .def. Forwarded to
        :func:`stage_build_context` which symlinks it into the staging
        dir under its own name so the .def's ``Bootstrap: localimage`` /
        ``From: ./<name>.sif`` line resolves against the build context
        (``cwd``) at build time. ``None`` for top-of-stack defs
        (``base``, ``proxy``). Required for ``scitex`` (bootstraps off
        ``sac-base.sif``); omitting it produces a half-staged context
        and apptainer FATAL's on "no such file or directory".

    Returns
    -------
    Path
        For a SIF build, the STABLE inner boot symlink
        (``<output_dir>/sac-<layer>/sac-<layer>.sif``) — what callers
        (and downstream layers' ``bootstrap_sif``) resolve against,
        unchanged from the pre-atomic layout. For a sandbox build, the
        sandbox directory (``<output_dir>/sac-<layer>/sac-<layer>.sandbox``).

    Raises
    ------
    RuntimeError
        Propagated from :func:`scitex_container.build` if the underlying
        apptainer build fails. The live image + symlinks are left intact.
    FileNotFoundError
        Propagated from :func:`stage_build_context` if inputs are missing.
    """
    artifact_dir = output_dir / f"sac-{layer}"
    artifact_dir.mkdir(parents=True, exist_ok=True)

    staging_dir = artifact_dir / "build-context"
    staged_def = stage_build_context(
        pkg_root, def_path, staging_dir, bootstrap_sif=bootstrap_sif
    )

    image_name = f"sac-{layer}"
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
    # Callers (and the next layer's bootstrap_sif) want the STABLE inner
    # boot symlink, which is layout-invariant across rebuilds.
    return artifact_dir / f"{image_name}.sif"


# ``BootstrapSifMissing`` and ``resolve_bootstrap_sif`` MOVED to
# ``_image_layer_chain`` when the monolithic :base recipe became the
# four-link ``system-deps -> python-pkgs -> base -> scitex`` stack: the
# layer topology is its own responsibility and is read by three modules,
# while THIS module stays about staging and building. Re-exported at the
# top of this file (and in ``__all__`` below) so every existing importer
# keeps resolving unchanged.


__all__ = [
    "stage_build_context",
    "build_layer_from_source",
    "locate_bundled_hatch_build",
    "resolve_bootstrap_sif",
    "BootstrapSifMissing",
    "_default_container_build",
    "_container_build",
]
