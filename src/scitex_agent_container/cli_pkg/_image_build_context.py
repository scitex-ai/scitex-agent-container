"""Build-context staging for source-bundled apptainer builds.

The shipped Apptainer .def files install sac from a RELATIVE path that lives
next to the .def at build time::

    %files
        scitex-agent-container-src /opt/scitex-agent-container-src

    %post
        ...
        uv pip install /opt/scitex-agent-container-src
        ...

This pins the in-SIF sac version to the source tree that shipped the .def —
no ``git+...@main`` snapshot drift. Whatever sac source contains the .def is
what lands in the SIF.

For that to work, ``apptainer build`` must run with its CWD set to a directory
that contains BOTH the .def and a ``scitex-agent-container-src/`` copy of the
package root (plus, for a layered stage, the parent SIF its ``From:`` names).
This module owns that staging step; ``_image_source_build`` owns the build
invocation that consumes it.

Split out of ``_image_source_build`` when that module crossed the 512-line
budget: staging and invoking are two responsibilities that only share a caller.
Every name here is re-exported from ``_image_source_build`` so existing import
paths keep resolving.
"""

from __future__ import annotations

import shutil
from pathlib import Path

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
        build lifecycle. ``None`` for stages that bootstrap off a
        registry image (``01-system-deps``, ``proxy``). Required for
        every layered stage (``02-python-pkgs`` / ``03-base`` /
        ``04-scitex``); omitting it produces a half-staged build context
        that apptainer FATAL's on with "no such file or directory" —
        that was the bug behind the 2026-06-07 cohort-A rebuild stall.

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
            "stage first (e.g. `sac image build 03-base -y` before `sac image "
            "build 04-scitex -y`, or `sac image build 04-scitex --chain -y` for "
            "the whole chain), then retry."
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
    # absolute resolved target: instant (a multi-GB parent SIF would otherwise
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


__all__ = [
    "locate_bundled_hatch_build",
    "locate_bundled_pyproject",
    "locate_bundled_readme",
    "stage_build_context",
]
