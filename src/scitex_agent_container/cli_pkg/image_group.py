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

from ._helpers import HelpRecursiveGroup, console

# Recipes ship inside the wheel (read-only, package-relative).
_RECIPES_DIR = Path(__file__).resolve().parent.parent / "containers"

# Built artifacts live in user state (persistent, never in the repo).
_CONTAINERS_DIR = Path.home() / ".scitex" / "agent-container" / "containers"

# Layer → .def filename mapping.
_LAYERS = {
    "base": "apptainer-base.def",
    "scitex": "apptainer-scitex.def",
}
_DEFAULT_LAYER = "scitex"


def _ensure_containers_dir() -> Path:
    """Create ``~/.scitex/agent-container/containers/`` if needed; return it."""
    _CONTAINERS_DIR.mkdir(parents=True, exist_ok=True)
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
    "--runtime",
    type=click.Choice(["apptainer", "docker"]),
    default="apptainer",
    show_default=True,
    help="Container runtime to build for.",
)
@click.option(
    "--sandbox",
    is_flag=True,
    default=False,
    help="Build as a writable sandbox directory (apptainer only).",
)
@click.option("--dry-run", is_flag=True, default=False, help="Print what would build.")
@click.option(
    "-y",
    "--yes",
    is_flag=True,
    default=False,
    help="Skip confirmation. Also implies overwrite of any existing SIF.",
)
def image_build(
    layer: str, runtime: str, sandbox: bool, dry_run: bool, yes: bool
) -> None:
    """Build the :LAYER image (default: scitex).

    \b
    Examples:
      $ sac image build                       # apptainer :scitex SIF
      $ sac image build base                  # apptainer :base SIF
      $ sac image build scitex --sandbox      # writable sandbox dir
      $ sac image build --runtime docker      # docker :scitex
    """
    if dry_run:
        click.echo(
            f"[dry-run] would build runtime={runtime} layer={layer} sandbox={sandbox}"
        )
        return

    if not yes:
        click.echo(
            f"Refusing to build (runtime={runtime}, layer={layer}) without --yes/-y.",
            err=True,
        )
        sys.exit(2)

    out_dir = _ensure_containers_dir()
    def_path = _RECIPES_DIR / _LAYERS[layer]
    if not def_path.is_file():
        click.echo(f"error: recipe not found in wheel: {def_path}", err=True)
        sys.exit(1)

    if runtime == "apptainer":
        # apptainer-scitex.def has `Bootstrap: localimage` `From: ./<base>.sif`
        # which resolves against cwd — so we cd into the user-state dir
        # where built SIFs live before invoking apptainer.
        # ``-y`` (yes, already gating sac's own confirmation above) also
        # implies ``--force`` for apptainer so its built-in "build target
        # already exists" prompt doesn't double-block on rebuilds.
        if sandbox:
            output = out_dir / f"scitex-agent-container-{layer}-sandbox"
            argv = [
                "apptainer",
                "build",
                "--sandbox",
                "--fakeroot",
                "--force",
                str(output),
                str(def_path),
            ]
        else:
            output = out_dir / f"scitex-agent-container-{layer}.sif"
            argv = ["apptainer", "build", "--force", str(output), str(def_path)]
        import subprocess

        result = subprocess.run(argv, cwd=str(out_dir))
        if result.returncode != 0:
            click.echo("error: apptainer build failed", err=True)
            sys.exit(result.returncode)
        console.print(f"[green]built[/green] {output}")
        return

    # docker runtime — also from the wheel-bundled Dockerfile
    image_tag = f"scitex-agent-container:{layer}"
    dockerfile = _RECIPES_DIR / f"Dockerfile.{layer}"
    if not dockerfile.is_file():
        click.echo(f"error: dockerfile not found: {dockerfile}", err=True)
        sys.exit(1)
    import subprocess

    argv = [
        "docker",
        "build",
        "-t",
        image_tag,
        "-f",
        str(dockerfile),
        str(_RECIPES_DIR),
    ]
    result = subprocess.run(argv)
    if result.returncode != 0:
        click.echo("error: docker build failed", err=True)
        sys.exit(result.returncode)
    console.print(f"[green]built[/green] {image_tag}")


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
    help="Output dir (default: <source-stem>-sandbox/ next to the SIF).",
)
def image_sandbox(source: str, output: Path | None) -> None:
    """Create a writable sandbox from SOURCE (a SIF path or layer name).

    \b
    Examples:
      $ sac image sandbox scitex                          # use the :scitex SIF
      $ sac image sandbox /path/to/scitex.sif --output /tmp/sandbox/
    """
    from scitex_container.apptainer import sandbox_create

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
      $ sac image update /opt/containers/scitex-sandbox/
      $ sac image update sandbox/ -p scitex -p numpy
    """
    from scitex_container.apptainer import sandbox_update

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
    from scitex_container.apptainer import sandbox_to_sif

    result = sandbox_to_sif(sandbox_dir=sandbox_dir, output_sif=output_sif)
    console.print(f"[green]frozen[/green] {result}")


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------
@image_group.command("list")
@click.option("--json", "as_json", is_flag=True, help="Output as JSON.")
def image_list(as_json: bool) -> None:
    """List installed SIF versions.

    \b
    Example:
      $ sac image list
      $ sac image list --json
    """
    from scitex_container.apptainer import list_versions

    _ensure_containers_dir()
    versions = list_versions(_CONTAINERS_DIR)
    console.print(f"[dim]containers dir: {_CONTAINERS_DIR}[/dim]")
    if as_json:
        click.echo(json.dumps(versions, indent=2, default=str))
        return
    if not versions:
        console.print(
            f"[dim](no SIFs in {_CONTAINERS_DIR} — run "
            f"`sac image build base -y && sac image build scitex -y` to populate)[/dim]"
        )
        return
    for v in versions:
        console.print(f"  {v}")


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
    from scitex_container.apptainer import switch_version

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
    from scitex_container.apptainer import rollback

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
    from scitex_container.apptainer import status as sc_status

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
    from scitex_container import env_snapshot

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
