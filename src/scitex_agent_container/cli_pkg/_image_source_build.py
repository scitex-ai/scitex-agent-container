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
import sysconfig
from importlib import metadata
from pathlib import Path
from typing import Callable
from urllib.parse import unquote, urlparse

import tomllib

from .._provenance._git import repo_root_for_package
from .._provenance._stamp import compute_stamp, render_module, stamp_path
from ._image_build_lock import image_build_lock

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
    # Generated metadata belongs to the distribution that produced the
    # source install.  It is not source code and must never be copied into a
    # new image build context: once .git disappears inside Apptainer, the
    # wheel build treats this file as authoritative sdist-style provenance.
    "_build_info.py",
)


# Where the staged source sibling-copy lives inside the staging dir.
# The .def files reference this exact name in their %files section, so
# changing it requires changing the .def files in lockstep.
_STAGED_SRC_NAME = "scitex-agent-container-src"


class SourceProvenanceMismatch(RuntimeError):
    """Raised when a build would stage source from a different SAC tree."""


def _environment_package_root(purelib: Path | None = None) -> Path | None:
    """Return SAC's package root installed in the active interpreter env.

    Discovery is deliberately limited to that interpreter's ``purelib``;
    searching all of ``sys.path`` would let the stale ``PYTHONPATH`` entry we
    are auditing supply its own distribution metadata too.
    """
    purelib = (
        purelib.resolve()
        if purelib is not None
        else Path(sysconfig.get_path("purelib")).resolve()
    )
    distribution = next(
        (
            item
            for item in metadata.distributions(path=[str(purelib)])
            if (item.metadata.get("Name") or "").lower().replace("_", "-")
            == "scitex-agent-container"
        ),
        None,
    )
    if distribution is None:
        return None

    direct_url_text = distribution.read_text("direct_url.json")
    if direct_url_text:
        import json

        try:
            direct_url = json.loads(direct_url_text)
            parsed = urlparse(str(direct_url.get("url", "")))
            if (
                direct_url.get("dir_info", {}).get("editable")
                and parsed.scheme == "file"
            ):
                repo_root = Path(unquote(parsed.path)).resolve()
                editable_package = repo_root / "src" / "scitex_agent_container"
                if editable_package.is_dir():
                    return editable_package.resolve()
        except (TypeError, ValueError):
            pass

    files = distribution.files or ()
    init_file = next(
        (
            item
            for item in files
            if str(item).replace("\\", "/") == "scitex_agent_container/__init__.py"
        ),
        None,
    )
    if init_file is None:
        return None
    return Path(distribution.locate_file(init_file)).resolve().parent


def assert_source_provenance(
    pkg_root: Path, *, environment_root: Path | None = None
) -> None:
    """Refuse a source-bundled build assembled from mixed SAC installs.

    ``pkg_root`` is the package tree that will be copied into the image build
    context. It must be the same package tree that supplied both the imported
    top-level package and this staging helper. This catches a stale
    ``PYTHONPATH`` winning over the environment that launched ``sac`` before
    the staging directory is reset or a container build starts.

    No repository or current-working-directory assumption is made, so an
    internally consistent editable install and an internally consistent wheel
    install are both valid.
    """
    import scitex_agent_container

    staged_root = pkg_root.resolve()
    package_file = getattr(scitex_agent_container, "__file__", None)
    if package_file is None:
        raise SourceProvenanceMismatch(
            "refusing source-bundled image build: the loaded "
            "scitex_agent_container package has no filesystem origin; "
            f"staged source root: {staged_root}. Run the build from one "
            "unambiguous SAC installation (for an editable checkout: "
            "PYTHONPATH=<checkout>/src uv run sac image build ...)."
        )

    loaded_package_root = Path(package_file).resolve().parent
    loaded_helper_root = Path(__file__).resolve().parent.parent
    installed_root = (
        environment_root.resolve()
        if environment_root is not None
        else _environment_package_root()
    )
    roots_match = (
        loaded_package_root == staged_root and loaded_helper_root == staged_root
    )
    if installed_root is not None:
        roots_match = roots_match and installed_root == staged_root
    if roots_match:
        return

    installed_line = (
        str(installed_root) if installed_root is not None else "unavailable"
    )
    raise SourceProvenanceMismatch(
        "refusing source-bundled image build: SAC source provenance is mixed.\n"
        f"  loaded package root: {loaded_package_root}\n"
        f"  loaded build-helper root: {loaded_helper_root}\n"
        f"  active-environment package root: {installed_line}\n"
        f"  staged source root: {staged_root}\n"
        "The image could otherwise contain source different from the command "
        "that built it. Remove the stale PYTHONPATH entry or pin it to the "
        "intended checkout, for example:\n"
        f"  PYTHONPATH={staged_root.parent} uv run sac image build ..."
    )


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
    at ``<repo>/scripts/hatch_build.py``. Defaults to ``name`` (the
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
    "scripts/hatch_build.py"``, and hatchling resolves that path RELATIVE TO
    THE TREE BEING BUILT. The staged tree IS that tree (the .def runs
    ``uv pip install /opt/scitex-agent-container-src``), so the hook must
    be staged next to pyproject.toml or the backend dies before reading
    one line of source: ``OSError: Build script does not exist:
    scripts/hatch_build.py``. Not hypothetical — that is how EVERY SIF build
    failed from the moment the hook landed.

    The hook is a BUILD input (same category as pyproject.toml/README.md),
    not a runtime module, so the wheel carries it in the inert
    ``_bundled/`` data dir — no ``__init__.py``, never importable as
    ``scitex_agent_container.*`` — which keeps hatch_build.py's own rule
    that an ``import hatchling`` module never reaches the runtime path.

    Editable fallback is ``<repo>/scripts/hatch_build.py``, not the repo
    root — hence ``editable_rel``. See :func:`_locate_bundled_sibling`.
    """
    return _locate_bundled_sibling(
        pkg_root, "hatch_build.py", editable_rel="scripts/hatch_build.py"
    )


def _declared_version(pyproject_path: Path) -> str:
    """Return the project version named by the staged build input.

    SAC's floor is Python 3.11, and this intentionally does not import
    ``tomllib``.  The project's version is a required, single-line PEP 621
    field; absence is a malformed image-build input and fails before the
    expensive container build starts.
    """
    in_project = False
    for raw_line in pyproject_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line == "[project]":
            in_project = True
            continue
        if in_project and line.startswith("["):
            break
        if in_project and line.startswith("version") and "=" in line:
            value = line.split("=", 1)[1].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
                return value[1:-1]
    raise ValueError(
        f"image-build pyproject has no quoted [project].version: {pyproject_path}"
    )


def _write_staged_provenance(
    *, source_package: Path, staged_package: Path, pyproject_path: Path
) -> None:
    """Stamp the exact source checkout and bytes entering Apptainer.

    ``copytree`` deliberately excludes ``_build_info.py`` because that file
    describes an older distribution, not the source being staged.  This
    function then computes a fresh stamp from the source checkout (where
    ``.git`` still exists) and from the copied package bytes.  The later
    wheel build runs without ``.git`` and inherits this freshly generated
    stamp through the same mechanism used by the supported sdist→wheel path.

    A wheel-installed SAC has no checkout; in that case ``compute_stamp``
    inherits its packaged commit but still recomputes the content hash from
    the actual source bytes.  Thus commit provenance never comes from a
    copied generated file, and content identity always describes this stage.
    """
    source_root = repo_root_for_package(source_package) or source_package.parent
    stamp = compute_stamp(
        root=source_root,
        package_dir=source_package,
        version=_declared_version(pyproject_path),
    )
    staged_stamp = compute_stamp(
        root=source_root,
        package_dir=staged_package,
        version=stamp["version"],
    )
    # For a non-checkout source, only the source package can carry the prior
    # distribution's commit.  The staged tree intentionally cannot inherit
    # it because its old generated file was excluded.
    staged_stamp["commit"] = stamp["commit"]
    staged_stamp["commit_source"] = stamp["commit_source"]
    target = stamp_path(staged_package)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render_module(staged_stamp), encoding="utf-8")


def _declared_package_sources(
    *, pyproject_path: Path, package_root: Path
) -> tuple[tuple[Path, Path], ...]:
    """Resolve every wheel package declared by the staged pyproject.

    Hatchling's explicit ``packages`` list is part of the build input just
    like its custom-hook path.  The installed packages are siblings beneath
    one import root, while their staged destinations retain the declared
    ``src/...`` layout.
    """
    data = tomllib.loads(pyproject_path.read_text(encoding="utf-8"))
    declared = (
        data.get("tool", {})
        .get("hatch", {})
        .get("build", {})
        .get("targets", {})
        .get("wheel", {})
        .get("packages")
    )
    if not isinstance(declared, list) or not declared:
        raise ValueError(
            "image-build pyproject must explicitly declare a non-empty "
            "[tool.hatch.build.targets.wheel].packages list"
        )

    import_root = package_root.parent
    resolved: list[tuple[Path, Path]] = []
    for value in declared:
        if not isinstance(value, str):
            raise ValueError(
                f"unsupported wheel package path in image-build pyproject: {value!r}"
            )
        relative = Path(value)
        if (
            relative.is_absolute()
            or len(relative.parts) < 2
            or relative.parts[0] != "src"
            or ".." in relative.parts
        ):
            raise ValueError(
                f"unsupported wheel package path in image-build pyproject: {value!r}"
            )
        source = import_root.joinpath(*relative.parts[1:])
        if not source.is_dir():
            raise FileNotFoundError(
                "declared wheel package is absent from the SAC installation: "
                f"{value} -> {source}"
            )
        resolved.append((relative, source))
    expected_main = Path("src") / package_root.name
    if expected_main not in {relative for relative, _source in resolved}:
        raise ValueError(
            "image-build pyproject does not declare the SAC package itself: "
            f"missing {expected_main}"
        )
    return tuple(resolved)


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
                scripts/
                    hatch_build.py             # pyproject's hooks.custom path
                src/
                    scitex_agent_container/    # copy of pkg_root contents
                    <other declared packages>/ # e.g. console bootstrap

    Every entry in pyproject.toml's
    ``[tool.hatch.build.targets.wheel].packages`` is copied, so ``pip
    install <X>`` resolves the same complete package set that the wheel
    ships — but pinned to the source tree that shipped this .def.

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

    # Resolve pyproject.toml + README.md + hatch_build.py + every declared
    # wheel package + (when set) bootstrap_sif BEFORE wiping dest_dir so a
    # missing-file failure doesn't strand the operator with a half-staged
    # tree.
    pyproject_src = locate_bundled_pyproject(pkg_root)
    readme_src = locate_bundled_readme(pkg_root)
    hatch_build_src = locate_bundled_hatch_build(pkg_root)
    package_sources = _declared_package_sources(
        pyproject_path=pyproject_src,
        package_root=pkg_root,
    )
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
    #   <staged_src>/scripts/hatch_build.py  (pyproject's hooks.custom path)
    #   <staged_src>/src/scitex_agent_container/...
    #
    # EVERY path pyproject NAMES must be staged, not just the package:
    # the PEP-517 backend reads pyproject FIRST and resolves its declared
    # paths against the staged root.
    staged_src = dest_dir / _STAGED_SRC_NAME
    staged_src.mkdir()
    shutil.copy2(pyproject_src, staged_src / "pyproject.toml")
    shutil.copy2(readme_src, staged_src / "README.md")
    (staged_src / "scripts").mkdir()
    shutil.copy2(hatch_build_src, staged_src / "scripts" / "hatch_build.py")
    for relative, source in package_sources:
        destination = staged_src / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(source, destination, ignore=_COPY_IGNORE)
    pkg_dest = staged_src / "src" / "scitex_agent_container"
    _write_staged_provenance(
        source_package=pkg_root,
        staged_package=pkg_dest,
        pyproject_path=staged_src / "pyproject.toml",
    )

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


def _stage_hermes_source(build_context: Path) -> Path:
    from ._hermes_source import stage_hermes_source

    return stage_hermes_source(build_context)


def _stage_cards_source(build_context: Path) -> Path:
    from ._cards_source import stage_cards_source

    return stage_cards_source(build_context)


def stage_layer_build_context(
    *,
    layer: str,
    pkg_root: Path,
    def_path: Path,
    staging_dir: Path,
    bootstrap_sif: Path | None = None,
    stage_cards: Callable[[Path], Path] | None = None,
    stage_hermes: Callable[[Path], Path] | None = None,
) -> Path:
    """Stage every source input required by one image layer.

    This is the single staging policy shared by ordinary and reproducible
    builds. Keeping the layer-to-source mapping here prevents the round-trip
    path from drifting behind the ordinary build whenever a recipe gains a
    new relative ``%files`` source.
    """
    staged_def = stage_build_context(
        pkg_root,
        def_path,
        staging_dir,
        bootstrap_sif=bootstrap_sif,
    )
    cards_stager = stage_cards or _stage_cards_source
    hermes_stager = stage_hermes or _stage_hermes_source
    if layer in {"base", "scitex"}:
        cards_stager(staging_dir)
    if layer == "base":
        hermes_stager(staging_dir)
    return staged_def


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
    prior image intact on failure. A non-blocking per-layer process lock
    covers staging through build completion; a concurrent build for the
    same layer is refused before it can reset this build's context.

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
    with image_build_lock(artifact_dir, layer=layer):
        staging_dir = artifact_dir / "build-context"
        staged_def = stage_layer_build_context(
            layer=layer,
            pkg_root=pkg_root,
            def_path=def_path,
            staging_dir=staging_dir,
            bootstrap_sif=bootstrap_sif,
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
            return Path(result)
        return artifact_dir / f"{image_name}.sif"


class BootstrapSifMissing(FileNotFoundError):
    """Raised when a layered build's prerequisite SIF is absent.

    Carries the fail-loud remediation text the CLI surfaces verbatim, so
    the layer→prerequisite policy lives with the source-build path rather
    than inline in the ``sac image build`` command.
    """


def resolve_bootstrap_sif(layer: str, output_dir: Path) -> Path | None:
    """Return the prerequisite SIF a layered ``.def`` bootstraps off.

    Layered .defs (currently only ``scitex``) start ``From: ./sac-base.sif``
    — a path RELATIVE to the build-context dir. The prerequisite is the
    prior layer's STABLE inner boot symlink,
    ``<output_dir>/sac-base/sac-base.sif`` (a symlink to the live
    timestamped SIF under scitex-container 0.3.0's atomic layout).
    :func:`build_layer_from_source` symlinks it into the staging dir so
    apptainer's relative ``From:`` resolves at build time.

    Returns ``None`` for top-of-stack layers (``base``) which bootstrap
    off a registry image, not a prior SIF.

    Raises
    ------
    BootstrapSifMissing
        When a layered build is requested but the prerequisite SIF has not
        been built. Fails loud BEFORE staging so apptainer never FATAL's
        on a half-staged context (the 2026-06-07 cohort-A rebuild stall).
        The exception message names the missing path AND the remediation
        command.
    """
    if layer != "scitex":
        return None
    bootstrap_sif = output_dir / "sac-base" / "sac-base.sif"
    if not bootstrap_sif.is_file():
        raise BootstrapSifMissing(
            f"scitex layer requires a built sac-base.sif at "
            f"{bootstrap_sif}; build the base layer first:\n"
            f"  $ sac image build base -y\n"
            f"then retry `sac image build scitex -y`."
        )
    return bootstrap_sif


__all__ = [
    "stage_build_context",
    "build_layer_from_source",
    "locate_bundled_hatch_build",
    "resolve_bootstrap_sif",
    "BootstrapSifMissing",
    "SourceProvenanceMismatch",
    "assert_source_provenance",
    "_environment_package_root",
    "_default_container_build",
    "_container_build",
]
