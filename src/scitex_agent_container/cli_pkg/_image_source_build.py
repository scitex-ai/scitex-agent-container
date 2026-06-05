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
        -> runs ``apptainer build`` with cwd=staging_dir
        -> returns the path to the built SIF (or sandbox dir)

The scitex_container.apptainer.build helper does not expose a ``cwd``
parameter and locates .def files by name via its own search heuristic, so
the source-bundled build path bypasses it and shells out to ``apptainer``
directly. The backend abstraction in image_group.py is preserved for the
non-build verbs (sandbox / update / freeze / list / status / snapshot)
which manage already-built SIFs.

Testability follows the same save/restore pattern as ``image_group``'s
``_load_apptainer`` hook: ``_apptainer_build_runner`` is a module-level
callable that tests reassign to a real (no MagicMock) recording fake.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Callable

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


def _locate_bundled_sibling(pkg_root: Path, name: str) -> Path:
    """Return the path to a wheel-bundled or repo-root file by name.

    Resolution order — both supported because sac is installed BOTH
    ways in real use:

      1. ``pkg_root/_bundled/<name>`` — wheel install. The wheel
         ships the file via ``[tool.hatch.build.targets.wheel.
         force-include]`` in the repo's own pyproject.toml.
      2. ``pkg_root.parent.parent/<name>`` — editable install
         (``pip install -e .``). The package is at
         ``<repo>/src/scitex_agent_container/``; ``parent.parent``
         walks up through ``src/`` to the repo root.

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
    editable_repo = pkg_root.parent.parent / name
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


def stage_build_context(
    pkg_root: Path,
    def_path: Path,
    dest_dir: Path,
) -> Path:
    """Stage a build-context dir for source-bundled apptainer builds.

    Layout produced (``X = dest_dir / "scitex-agent-container-src"``)::

        dest_dir/
            <def-name>.def                     # copy of def_path
            scitex-agent-container-src/        # pip-installable source tree
                pyproject.toml                 # from locate_bundled_pyproject
                src/
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

    Returns
    -------
    Path
        The path of the staged .def file (``dest_dir / def_path.name``).
        Pass this to ``apptainer build`` after setting cwd to ``dest_dir``.

    Raises
    ------
    FileNotFoundError
        If ``pkg_root`` / ``def_path`` / pyproject.toml is missing.
    NotADirectoryError
        If ``pkg_root`` exists but isn't a directory.
    """
    if not def_path.is_file():
        raise FileNotFoundError(f"recipe not found: {def_path}")
    if not pkg_root.exists():
        raise FileNotFoundError(f"package source not found: {pkg_root}")
    if not pkg_root.is_dir():
        raise NotADirectoryError(f"package source is not a directory: {pkg_root}")

    # Resolve pyproject.toml + README.md BEFORE wiping dest_dir so a
    # missing-file failure doesn't strand the operator with a
    # half-staged tree.
    pyproject_src = locate_bundled_pyproject(pkg_root)
    readme_src = locate_bundled_readme(pkg_root)

    # Reset the staging dir. A prior failed build can leave a partial
    # tree behind; copying on top of it would silently mix old + new
    # files (e.g. a renamed module would have both names present in
    # the SIF). Cheap: it's user-state under ~/.scitex.
    if dest_dir.exists():
        shutil.rmtree(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)

    staged_def = dest_dir / def_path.name
    shutil.copy2(def_path, staged_def)

    # The staged pip-installable source tree:
    #   <staged_src>/pyproject.toml
    #   <staged_src>/README.md           (referenced by pyproject's readme=)
    #   <staged_src>/src/scitex_agent_container/...
    staged_src = dest_dir / _STAGED_SRC_NAME
    staged_src.mkdir()
    shutil.copy2(pyproject_src, staged_src / "pyproject.toml")
    shutil.copy2(readme_src, staged_src / "README.md")
    pkg_dest = staged_src / "src" / "scitex_agent_container"
    pkg_dest.parent.mkdir(parents=True)
    shutil.copytree(pkg_root, pkg_dest, ignore=_COPY_IGNORE)

    return staged_def


# ---------------------------------------------------------------------------
# Build invocation
# ---------------------------------------------------------------------------


def _default_apptainer_build_runner(
    output_path: Path,
    staged_def: Path,
    *,
    cwd: Path,
    sandbox: bool,
    force: bool,
) -> int:
    """Default runner — shells out to ``apptainer build``.

    Returns the apptainer exit code (0 on success). Sandbox builds use
    ``--fakeroot`` (apptainer's sandbox-from-def path requires it on
    rootless installs).

    SIF builds prefer ``--fakeroot`` when the user already has
    ``/etc/subuid`` + ``/etc/subgid`` mappings (the lead's hand-built
    SIF on 2026-06-05 used this path), and fall back to ``sudo
    apptainer build`` only when no mappings exist. The sudo fallback
    works on an interactive shell but fails silently in headless /
    detached / no-tty contexts (sudo prompts for a password) — which
    is exactly what bit the lead's ``sac image build`` invocation
    earlier today. The fakeroot probe lives in
    :mod:`runtimes._apptainer_build` so both the lifecycle build and
    this CLI build agree on the heuristic.
    """
    # Local import — runtimes/_apptainer_build pulls config etc. and
    # we don't want to pay that cost on the cold ``sac image`` startup
    # path until the actual build runs.
    from ..runtimes._apptainer_build import _should_use_fakeroot_for_build

    if sandbox:
        argv = [
            "apptainer",
            "build",
            "--sandbox",
            "--fakeroot",
        ]
        if force:
            argv.append("--force")
        argv += [str(output_path), str(staged_def)]
    elif _should_use_fakeroot_for_build():
        # Rootless user-namespace build — no sudo prompt. This is the
        # path the lead's hand-built SIF used today.
        argv = ["apptainer", "build", "--fakeroot"]
        if force:
            argv.append("--force")
        argv += [str(output_path), str(staged_def)]
    else:
        # No subuid mappings: fall back to sudo. Works interactively;
        # FAILS in headless contexts (silent password prompt).
        argv = ["sudo", "apptainer", "build"]
        if force:
            argv.append("--force")
        argv += [str(output_path), str(staged_def)]

    result = subprocess.run(argv, cwd=str(cwd))
    return result.returncode


# Module-level overridable reference — same swap-and-restore pattern as
# image_group._load_apptainer. Tests reassign this to a real recording
# callable (no MagicMock).
_apptainer_build_runner: Callable[..., int] = _default_apptainer_build_runner


def build_layer_from_source(
    *,
    layer: str,
    def_path: Path,
    pkg_root: Path,
    output_dir: Path,
    sandbox: bool = False,
    force: bool = True,
) -> Path:
    """Build a sac SIF (or sandbox) from a .def that bundles its own source.

    Stages a build context under ``output_dir/sac-<layer>/build-context/``,
    invokes ``apptainer build`` with cwd set to that staging dir (so the
    .def's relative ``%files scitex-agent-container-src ...`` resolves to
    the bundled source copy), and returns the path of the built artefact.

    Parameters
    ----------
    layer : str
        Layer name (``base`` / ``scitex`` / ``proxy``). Used to name the
        per-layer artefact dir and the output filename.
    def_path : Path
        Source .def file. Copied (not modified) into the staging dir.
    pkg_root : Path
        Package source root. Copied into the staging dir as
        ``scitex-agent-container-src/``.
    output_dir : Path
        Containers dir (typically ``~/.scitex/agent-container/containers``).
        The per-layer subdir is created here.
    sandbox : bool
        If True, build a writable sandbox directory rather than a SIF.
    force : bool
        Pass ``--force`` to apptainer (overwrite existing artefact).

    Returns
    -------
    Path
        Path to the built ``.sif`` (or sandbox dir).

    Raises
    ------
    RuntimeError
        If the apptainer subprocess exits non-zero. The build context
        is left in place for post-mortem inspection.
    FileNotFoundError
        Propagated from :func:`stage_build_context` if inputs are missing.
    """
    artifact_dir = output_dir / f"sac-{layer}"
    artifact_dir.mkdir(parents=True, exist_ok=True)

    staging_dir = artifact_dir / "build-context"
    staged_def = stage_build_context(pkg_root, def_path, staging_dir)

    output_path = artifact_dir / (
        f"sac-{layer}.sandbox" if sandbox else f"sac-{layer}.sif"
    )

    rc = _apptainer_build_runner(
        output_path,
        staged_def,
        cwd=staging_dir,
        sandbox=sandbox,
        force=force,
    )
    if rc != 0:
        raise RuntimeError(
            f"apptainer build failed (rc={rc}) for layer={layer} "
            f"def={staged_def} cwd={staging_dir}"
        )
    return output_path


__all__ = [
    "stage_build_context",
    "build_layer_from_source",
    "_default_apptainer_build_runner",
    "_apptainer_build_runner",
]
