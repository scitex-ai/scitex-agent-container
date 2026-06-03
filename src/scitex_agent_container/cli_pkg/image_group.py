"""``sac image`` noun-group — container image lifecycle.

Apptainer-first (HPC compatibility), docker remains for dev-laptop convenience.
Build/sandbox/version/rollback verbs delegate to ``scitex-container``;
``sac`` only owns the user-facing CLI shape and the .def files.

Verbs:
  build        Build SIF or sandbox from a layer's .def
  sandbox      Create a writable sandbox from a SIF (mutable rootfs)
  update       Refresh packages inside a sandbox (pip install --upgrade)
  freeze       Bake a sandbox back to an immutable SIF
  list         List installed SIF versions
  switch       Atomically switch to a different version
  rollback     Restore the previous version
  status       Unified container dashboard
  snapshot     Capture a reproducibility snapshot (pip + apt + git + ...)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import click

from . import _image_source_build
from ._helpers import HelpRecursiveGroup, console

# Module-level overridable reference for the source-bundled build path.
# Tests reassign this to a real recording callable (same swap-and-restore
# pattern as ``_load_apptainer``); production code calls through it so
# tests don't need to patch the cli_pkg._image_source_build module.
_build_layer_from_source = _image_source_build.build_layer_from_source

# Recipes ship inside the wheel (read-only, package-relative).
_RECIPES_DIR = Path(__file__).resolve().parent.parent / "containers"

# Built artifacts live in user state (persistent, never in the repo).
#
# sac OWNS this directory for its own (base / scitex) artifacts. Other
# scitex-* packages own their own siblings under ``~/.scitex/<pkg>/``
# per the ``~/.scitex/<pkg>/{containers,bin}`` convention (operator
# design 8566): scitex-writer → ``~/.scitex/writer/containers/``,
# scitex-neurovista → ``~/.scitex/neurovista/containers/``, etc. sac
# does NOT host other packages' SIFs in its own namespace — minimal
# scope per the ecosystem doctrine.
_CONTAINERS_DIR = Path.home() / ".scitex" / "agent-container" / "containers"

# Generic cross-package discovery root. ``sac image list`` globs
# ``<root>/*/containers/*.sif`` so any package following the convention
# becomes visible without sac knowing the package by name. Mirrors the
# ``scitex_dev.*`` entry_points discovery pattern (each package owns its
# own surface; the aggregator never hard-codes package names).
_SCITEX_USER_STATE_ROOT = Path.home() / ".scitex"

# Layer → .def filename mapping.
_LAYERS = {
    "base": "apptainer-base.def",
    "scitex": "apptainer-scitex.def",
}
_DEFAULT_LAYER = "base"


# ---------------------------------------------------------------------------
# Backend loaders (public seam for the Python API + tests)
#
# The verbs in this group delegate to ``scitex-container``, which is a
# separately installed package on real systems but absent from this
# repo's dev / CI tree. We surface the backend lookup as two callable
# module attributes so:
#
#   * external Python users can swap in a different backend (mirror an
#     in-memory builder, a remote daemon, etc.) by assigning their own
#     callable, the same way ``logging.getLogger`` can be redirected,
#   * tests can install a real, hand-rolled fake backend class via the
#     normal save/restore pattern without monkeypatching deep imports
#     or fabricating ``sys.modules`` entries.
#
# Both loaders raise the real ``ImportError`` if scitex-container is
# absent — no silent fallbacks.
# ---------------------------------------------------------------------------
def _default_load_apptainer():
    """Default ``apptainer`` backend loader — real import from scitex-container."""
    from scitex_container import apptainer

    return apptainer


def _default_load_env_snapshot():
    """Default ``env_snapshot`` loader — real import from scitex-container."""
    from scitex_container import env_snapshot

    return env_snapshot


# Module-level overridable references. Reassign these (and restore!) to
# swap the backend.
_load_apptainer = _default_load_apptainer
_load_env_snapshot = _default_load_env_snapshot


def _ensure_containers_dir() -> Path:
    """Create ``~/.scitex/agent-container/containers/`` if needed; return it.

    Also seeds the agent-container root ``.gitignore`` so the
    multi-GB Apptainer SIFs / sandboxes / build logs about to land
    here aren't accidentally tracked by an enclosing dotfiles repo.
    Idempotent; never raises.
    """
    _CONTAINERS_DIR.mkdir(parents=True, exist_ok=True)
    from .._state._bootstrap import ensure_root_gitignore

    ensure_root_gitignore(_CONTAINERS_DIR.parent)
    return _CONTAINERS_DIR


def _resolve_def_name(layer: str) -> str:
    """Layer name → bare .def stem (no extension), as scitex-container expects."""
    if layer not in _LAYERS:
        raise click.UsageError(
            f"Unknown layer '{layer}'. Choose from: {', '.join(_LAYERS)}"
        )
    return _LAYERS[layer].removesuffix(".def")


@click.group(name="image", cls=HelpRecursiveGroup)
def image_group() -> None:
    """Container image lifecycle (apptainer + docker; delegates to scitex-container)."""


# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------
@image_group.command("build")
@click.argument("layer", type=click.Choice(list(_LAYERS)), default=_DEFAULT_LAYER)
@click.option(
    "--sandbox",
    is_flag=True,
    default=False,
    help="Build as a writable sandbox directory.",
)
@click.option("--dry-run", is_flag=True, default=False, help="Print what would build.")
@click.option(
    "-y",
    "--yes",
    is_flag=True,
    default=False,
    help="Skip confirmation. Also implies overwrite of any existing SIF.",
)
def image_build(layer: str, sandbox: bool, dry_run: bool, yes: bool) -> None:
    """Build the :LAYER Apptainer SIF (default: base).

    Sac is apptainer-only since the 2026-05-13 docker/podman ripout.

    \b
    Examples:
      $ sac image build                # apptainer :base SIF (default; OS + dev tools, ~15-25 min)
      $ sac image build scitex         # apptainer :scitex SIF (FROM :base + scitex[all], ~10-20 min)
      $ sac image build --sandbox      # writable sandbox dir
    """
    out_dir = _ensure_containers_dir()
    # Existing artefact warning — operators forget that `sac image
    # build` overwrites the SIF in place. Surface the target path
    # (and its current size + mtime, if any) BEFORE the
    # refuse-without-yes gate so a `-y` re-invocation knows what it's
    # about to clobber.
    artifact_dir = out_dir / f"sac-{layer}"
    existing = artifact_dir / (
        f"sac-{layer}.sandbox" if sandbox else f"sac-{layer}.sif"
    )
    if existing.exists():
        import datetime as _dt

        size_mb = existing.stat().st_size / (1024 * 1024) if existing.is_file() else 0
        mtime = _dt.datetime.fromtimestamp(existing.stat().st_mtime).isoformat(
            timespec="seconds"
        )
        kind = "sandbox dir" if sandbox else "SIF"
        click.echo(
            f"⚠  Existing {kind} at {existing} "
            f"({size_mb:.0f} MB, built {mtime}) will be overwritten.",
            err=True,
        )

    if dry_run:
        click.echo(f"[dry-run] would build apptainer layer={layer} sandbox={sandbox}")
        return

    if not yes:
        click.echo(
            f"Refusing to build (layer={layer}) without --yes/-y.",
            err=True,
        )
        sys.exit(2)
    def_path = _RECIPES_DIR / _LAYERS[layer]
    if not def_path.is_file():
        click.echo(f"error: recipe not found in wheel: {def_path}", err=True)
        sys.exit(1)

    # Source-bundled build: the shipped .def files install sac from
    # /opt/scitex-agent-container-src, which gets there via a %files
    # copy of a sibling directory next to the .def at build time. The
    # staging helper (cli_pkg/_image_source_build.py) creates that
    # sibling copy under <out>/sac-<layer>/build-context/ and runs
    # ``apptainer build`` with cwd set there so the .def's relative
    # %files path resolves correctly.
    #
    # We bypass scitex-container's build helper for this path because
    # it doesn't expose a ``cwd`` parameter — without cwd control,
    # apptainer would resolve the .def's relative %files against
    # whatever directory the operator happened to ``sac image build``
    # from, which is not predictable. The non-build verbs (sandbox,
    # update, freeze, list, status, snapshot) still delegate to the
    # scitex-container backend since they operate on already-built
    # SIFs and don't need build-context staging.
    pkg_root = _RECIPES_DIR.parent
    try:
        output = _build_layer_from_source(
            layer=layer,
            def_path=def_path,
            pkg_root=pkg_root,
            output_dir=out_dir,
            sandbox=sandbox,
            force=True,  # -y already gated above
        )
    except (FileNotFoundError, RuntimeError) as exc:
        click.echo(f"error: apptainer build failed: {exc}", err=True)
        sys.exit(1)
    console.print(f"[green]built[/green] {output}")


# ---------------------------------------------------------------------------
# sandbox
# ---------------------------------------------------------------------------
@image_group.command("sandbox")
@click.argument("source", type=str)
@click.option(
    "--output",
    "-o",
    type=click.Path(file_okay=False, path_type=Path),
    default=None,
    help="Output dir (default: <source-stem>.sandbox/ next to the SIF).",
)
def image_sandbox(source: str, output: Path | None) -> None:
    """Create a writable sandbox from SOURCE (a SIF path or layer name).

    \b
    Examples:
      $ sac image sandbox scitex                          # use the :scitex SIF
      $ sac image sandbox /path/to/scitex.sif --output /tmp/sandbox/
    """
    sandbox_create = _load_apptainer().sandbox_create

    src_path = _resolve_source_to_sif(source)
    result = sandbox_create(
        source=src_path, containers_dir=_CONTAINERS_DIR, output_dir=output
    )
    console.print(f"[green]sandbox[/green] {result}")


# ---------------------------------------------------------------------------
# update (refresh packages inside a sandbox)
# ---------------------------------------------------------------------------
@image_group.command("update")
@click.argument(
    "sandbox_dir", type=click.Path(exists=True, file_okay=False, path_type=Path)
)
@click.option(
    "--package",
    "-p",
    "packages",
    multiple=True,
    help="Specific package(s) to upgrade. Default: scitex[all].",
)
def image_update(sandbox_dir: Path, packages: tuple[str, ...]) -> None:
    """Refresh packages inside a writable sandbox.

    \b
    Examples:
      $ sac image update /opt/containers/scitex.sandbox/
      $ sac image update sandbox/ -p scitex -p numpy
    """
    sandbox_update = _load_apptainer().sandbox_update

    pkgs = packages or ("scitex[all]",)
    result = sandbox_update(sandbox_dir=sandbox_dir, packages=pkgs)
    click.echo(json.dumps(result, indent=2, default=str))


# ---------------------------------------------------------------------------
# freeze (sandbox → SIF)
# ---------------------------------------------------------------------------
@image_group.command("freeze")
@click.argument(
    "sandbox_dir", type=click.Path(exists=True, file_okay=False, path_type=Path)
)
@click.argument("output_sif", type=click.Path(dir_okay=False, path_type=Path))
def image_freeze(sandbox_dir: Path, output_sif: Path) -> None:
    """Bake a sandbox back into an immutable SIF.

    \b
    Example:
      $ sac image freeze sandbox/ scitex-agent-container-2.28.15.sif
    """
    sandbox_to_sif = _load_apptainer().sandbox_to_sif

    result = sandbox_to_sif(sandbox_dir=sandbox_dir, output_sif=output_sif)
    console.print(f"[green]frozen[/green] {result}")


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------
@image_group.command("list")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def image_list(as_json: bool) -> None:
    """List installed SIFs across every scitex-* package.

    Discovers via the ``~/.scitex/<pkg>/containers/*.sif`` convention
    (operator design 8566) — sac does NOT know any other package by
    name; new packages light up automatically.

    \b
    Example:
      $ sac image list
      $ sac image list --json
    """
    _ensure_containers_dir()
    entries: list[Path] = []
    entries.extend(sorted(_SCITEX_USER_STATE_ROOT.glob("*/containers/*.sif")))
    entries.extend(
        sorted(
            p
            for p in _SCITEX_USER_STATE_ROOT.glob("*/containers/*.sandbox")
            if p.is_dir()
        )
    )

    def _dir_size_bytes(d: Path) -> int:
        total = 0
        for p in d.rglob("*"):
            try:
                if p.is_file() and not p.is_symlink():
                    total += p.stat().st_size
            except OSError:
                pass
        return total

    versions = []
    for p in entries:
        is_sandbox = p.is_dir()
        size_bytes = _dir_size_bytes(p) if is_sandbox else p.stat().st_size
        versions.append(
            {
                "package": p.parent.parent.name,
                "name": p.name,
                "path": str(p),
                "kind": "sandbox" if is_sandbox else "sif",
                "size_bytes": size_bytes,
                "mtime": p.stat().st_mtime,
            }
        )
    console.print(f"[dim]scan root: {_SCITEX_USER_STATE_ROOT}/*/containers/[/dim]")
    if as_json:
        click.echo(json.dumps(versions, indent=2, default=str))
        return
    if not versions:
        console.print(
            f"[dim](no SIFs under {_SCITEX_USER_STATE_ROOT}/*/containers/ — "
            f"run `sac image build base -y && sac image build scitex -y` to "
            f"populate; downstream packages populate their own siblings)[/dim]"
        )
        return
    for v in versions:
        size_mb = v["size_bytes"] / (1024 * 1024)
        tag = "sandbox" if v["kind"] == "sandbox" else "sif"
        label = f"{v['package']}/{v['name']}"
        console.print(f"  {tag:<7s}  {label:50s} {size_mb:>8.1f} MB")


# ---------------------------------------------------------------------------
# switch
# ---------------------------------------------------------------------------
@image_group.command("switch")
@click.argument("version", type=str)
def image_switch(version: str) -> None:
    """Atomically switch to a different SIF version.

    \b
    Example:
      $ sac image switch 2.28.15
    """
    switch_version = _load_apptainer().switch_version

    switch_version(version=version, containers_dir=_CONTAINERS_DIR)
    console.print(f"[green]switched[/green] -> {version}")


# ---------------------------------------------------------------------------
# rollback
# ---------------------------------------------------------------------------
@image_group.command("rollback")
def image_rollback() -> None:
    """Restore the previous SIF version.

    \b
    Example:
      $ sac image rollback
    """
    rollback = _load_apptainer().rollback

    prev = rollback(containers_dir=_CONTAINERS_DIR)
    console.print(f"[green]rolled back[/green] -> {prev}")


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------
@image_group.command("status")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def image_status(as_json: bool) -> None:
    """Unified container dashboard (active version, sandboxes, sizes).

    \b
    Example:
      $ sac image status
      $ sac image status --json
    """
    sc_status = _load_apptainer().status

    info = sc_status(containers_dir=_CONTAINERS_DIR)
    if as_json:
        click.echo(json.dumps(info, indent=2, default=str))
        return
    if not info:
        console.print(f"[dim](no containers in {_CONTAINERS_DIR})[/dim]")
        return
    for entry in info:
        name = entry.get("name", "?")
        size = entry.get("sif_size", "-")
        rebuild = "REBUILD" if entry.get("needs_rebuild") else "ok"
        console.print(f"  {name:30s}  {size!s:>10}  {rebuild}")


# ---------------------------------------------------------------------------
# snapshot
# ---------------------------------------------------------------------------
@image_group.command("snapshot")
@click.option(
    "--output",
    "-o",
    type=click.Path(dir_okay=False, path_type=Path),
    default=None,
    help="Write JSON to this path instead of stdout.",
)
def image_snapshot(output: Path | None) -> None:
    """Capture a reproducibility snapshot (pip + apt + conda + git + ...).

    \b
    Example:
      $ sac image snapshot
      $ sac image snapshot -o env.json
    """
    env_snapshot = _load_env_snapshot()

    snap = env_snapshot(containers_dir=_CONTAINERS_DIR)
    payload = json.dumps(snap, indent=2, default=str)
    if output:
        output.write_text(payload)
        console.print(f"[green]wrote[/green] {output}")
    else:
        click.echo(payload)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _resolve_source_to_sif(source: str) -> Path:
    """Layer name (e.g. 'scitex') or path → resolved SIF path."""
    p = Path(source).expanduser()
    if p.exists():
        return p.resolve()
    if source in _LAYERS:
        sif = _CONTAINERS_DIR / f"{_resolve_def_name(source)}.sif"
        if sif.exists():
            return sif
        raise click.UsageError(
            f"Layer '{source}' SIF not found at {sif}. "
            f"Build it first: sac image build {source}"
        )
    raise click.UsageError(f"Source '{source}' is neither a path nor a known layer.")


__all__ = ["image_group"]
