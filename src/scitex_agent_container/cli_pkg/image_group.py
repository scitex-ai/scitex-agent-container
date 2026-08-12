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

from .. import _build_priority
from . import (
    _image_inventory_cmds,
    _image_remote_bake,
    _image_repro_build,
    _image_source_build,
)
from ._helpers import HelpRecursiveGroup, console
from ._helpers._console import logger

# Module-level overridable reference for the source-bundled build path.
# Tests reassign this to a real recording callable (same swap-and-restore
# pattern as ``_load_apptainer``); production code calls through it so
# tests don't need to patch the cli_pkg._image_source_build module.
_build_layer_from_source = _image_source_build.build_layer_from_source

# Same seam for the reproducible round-trip path (``--reproducible``).
_run_reproducible_build = _image_repro_build.run_build

# Same seam pattern for the low-priority self-demotion
# (incident-local-heavy-build) — tests swap in a recording fake so the
# pytest process itself never gets demoted (demotion is one-way).
_demote_build_priority = _build_priority.demote_current_process_to_low_priority

# Same seam pattern for the remote-first load advisory — tests swap in a
# canned-string fake so the decision doesn't depend on the CI host's
# live loadavg.
_remote_build_advisory = _build_priority.remote_build_advisory

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
#
# ``proxy`` ships a recipe (containers/apptainer-proxy.def, force-included in
# the wheel) and the source-build path already treats it as a first-class
# layer — ``build_layer_from_source`` documents ``base``/``scitex``/``proxy``
# and ``resolve_bootstrap_sif`` names ``proxy`` among the top-of-stack layers
# that bootstrap off a registry image rather than a prior SIF. Only this map
# omitted it, which left sac shipping one recipe nothing could build.
_LAYERS = {
    "base": "apptainer-base.def",
    "scitex": "apptainer-scitex.def",
    "proxy": "apptainer-proxy.def",
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


# Read-only reporting verbs (list / status / snapshot) — extracted to
# _image_inventory_cmds (512-line budget); registered here so the CLI
# surface is unchanged.
image_group.add_command(_image_inventory_cmds.image_list)
image_group.add_command(_image_inventory_cmds.image_status)
image_group.add_command(_image_inventory_cmds.image_snapshot)

# Periodic remote bake (Spartan lease) + pull/verify/atomic-swap —
# extracted to _image_remote_bake / _remote_bake_core (512-line budget).
image_group.add_command(_image_remote_bake.image_bake_remote)


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
@click.option(
    "--no-nice",
    is_flag=True,
    default=False,
    help="Build at normal priority (skip the default nice-19 + ionice "
    "best-effort-low self-demotion; for dedicated build machines / CI).",
)
@click.option(
    "--reproducible",
    is_flag=True,
    default=False,
    help="Run the reproducible round trip: capture this build's version "
    "set into a .lock, emit a version-pinned .def, rebuild from it, "
    "compare the two version sets, and mark .verified/.unverified. "
    "Costs a SECOND full build.",
)
@click.option(
    "--skip-verify",
    is_flag=True,
    default=False,
    help="With --reproducible: capture the lock + pinned .def but skip the "
    "verify rebuild. Leaves the build UNMARKED (the use-time gate reads "
    "that as unverified).",
)
def image_build(
    layer: str,
    sandbox: bool,
    dry_run: bool,
    yes: bool,
    no_nice: bool,
    reproducible: bool,
    skip_verify: bool,
) -> None:
    """Build the :LAYER Apptainer SIF (default: base).

    Sac is apptainer-only since the 2026-05-13 docker/podman ripout.
    Builds self-demote to low CPU/IO priority by default so a bake
    can't starve an interactive host; ``--no-nice`` restores full speed.

    ``--reproducible`` additionally PROVES the image can be produced
    again: it freezes the versions that actually landed, generates a
    pinned recipe, rebuilds from that recipe, and compares the two
    version sets. Identical → ``.verified``; drift → ``.unverified``
    carrying the diff (the image stays usable — a mismatch is a finding,
    not a build failure). "Reproducible" here means ENVIRONMENT IDENTITY
    (same version set), not byte-identical digests.

    \b
    Examples:
      $ sac image build                # apptainer :base SIF (default; OS + dev tools, ~15-25 min)
      $ sac image build scitex         # apptainer :scitex SIF (FROM :base + scitex[all], ~10-20 min)
      $ sac image build --sandbox      # writable sandbox dir
      $ sac image build --reproducible # round trip + .verified marker (~2x build time)
    """
    flag_error = _image_repro_build.validate_flags(
        reproducible=reproducible, sandbox=sandbox, skip_verify=skip_verify
    )
    if flag_error:
        click.echo(f"error: {flag_error}", err=True)
        sys.exit(2)
    out_dir = _ensure_containers_dir()
    # Existing-artefact notice. A SIF rebuild is now ATOMIC (delegated to
    # scitex-container's ``build``): it lands a fresh timestamped SIF and
    # swaps the stable ``sac-<layer>.sif`` boot symlink all-at-once, so on
    # success the live image is replaced but on failure the prior one is
    # left intact (no in-place clobber). A sandbox rebuild is still in
    # place. ``existing`` is the stable inner boot symlink for SIFs;
    # ``.stat()`` follows it to the live timestamped target.
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
        verb = "overwritten" if sandbox else "replaced (atomic swap)"
        click.echo(
            f"⚠  Existing {kind} at {existing} "
            f"({size_mb:.0f} MB, built {mtime}) will be {verb}.",
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
    # sibling copy under <out>/sac-<layer>/build-context/, then delegates
    # to scitex-container 0.3.0's atomic ``build`` with ``cwd`` set to
    # that staging dir so the .def's relative %files + ``From: ./sac-
    # base.sif`` resolve. ``build`` lands a timestamped SIF and swaps the
    # stable ``sac-<layer>.sif`` boot symlink all-at-once — a failed
    # build leaves the prior image intact (atomic, rollback-safe). The
    # non-build verbs (sandbox, update, freeze, list, status, snapshot)
    # also delegate to the scitex-container backend.
    pkg_root = _RECIPES_DIR.parent

    # Layered .defs (currently: ``scitex``) bootstrap off a prior layer's
    # SIF (``From: ./sac-base.sif``). Resolve the prerequisite here — the
    # helper FAILS LOUD when it is missing so apptainer never FATAL's on a
    # half-staged context (the 2026-06-07 cohort-A rebuild stall).
    try:
        bootstrap_sif = _image_source_build.resolve_bootstrap_sif(layer, out_dir)
    except _image_source_build.BootstrapSifMissing as exc:
        click.echo(f"error: {exc}", err=True)
        sys.exit(1)

    # incident-local-heavy-build closure #3 (remote-first): when this
    # host is already busy (loadavg above LOAD_ADVISORY_FACTOR x cores),
    # say LOUDLY that a remote / dedicated build host (Spartan) is the
    # right place for the bake — then still proceed, demoted.
    # scitex-logging WARNING so it is colour-coded and unmissable
    # (PR #607 convention).
    advisory = _remote_build_advisory()
    if advisory:
        logger.warning(advisory)

    # incident-local-heavy-build: self-demote NOW — after every cheap
    # validation/refusal path, right before the heavy work — so the whole
    # bake (staging copytree + scitex-container's apptainer build →
    # %post apt/pip → mksquashfs, which all inherit this process's
    # priority) runs at low CPU/IO priority by default. Best-effort-low
    # IO, not idle class — idle starved/killed a real mksquashfs stage
    # under load (see _build_priority module docstring).
    for line in _demote_build_priority(skip=no_nice):
        click.echo(line)

    if reproducible:
        # Builds TWICE: the rough build, then a rebuild from the generated
        # pinned recipe. Both go through the same staged build context —
        # the reason scitex-container needed a ``cwd`` before sac could
        # call this at all.
        _run_reproducible_build(
            layer=layer,
            def_path=def_path,
            pkg_root=pkg_root,
            output_dir=out_dir,
            bootstrap_sif=bootstrap_sif,
            verify=not skip_verify,
        )
        return

    try:
        output = _build_layer_from_source(
            layer=layer,
            def_path=def_path,
            pkg_root=pkg_root,
            output_dir=out_dir,
            sandbox=sandbox,
            force=True,  # -y already gated above
            bootstrap_sif=bootstrap_sif,
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
