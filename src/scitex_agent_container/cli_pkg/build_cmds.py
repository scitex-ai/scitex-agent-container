"""Build/validation commands: check, validate, build."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import click

from ..config import load_config, resolve_config, validate_config
from ._helpers import agent_name_complete, console


@click.command()
@click.argument("name_or_path", type=str, shell_complete=agent_name_complete)
def check(name_or_path: str) -> None:
    """Run preflight checks for an agent deployment.

    Validates the YAML spec, then probes runtime dependencies
    (container backend, python). Accepts either a bare agent name
    (resolved against the search chain) or an explicit path to
    ``spec.yaml``.

    \b
    Example:
      $ sac agent check orchestrator
      $ sac agent check ~/.scitex/agent-container/agents/foo/spec.yaml
    """
    # stx-allow: fallback (reason: config file may not exist or contain invalid YAML; CLI exits with code 1 to signal preflight failure)
    try:
        config_path = resolve_config(name_or_path)
    except Exception as exc:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        console.print(f"[red]Error: {exc}[/red]")
        sys.exit(1)

    errors = validate_config(config_path)
    if errors:
        console.print(f"[red]Config validation failed: {config_path}[/red]")
        for error in errors:
            console.print(f"  [red]- {error}[/red]")
        sys.exit(1)

    # stx-allow: fallback (reason: load_config may fail post-validation in rare schema-evolution scenarios; CLI exits cleanly)
    try:
        config = load_config(config_path)
    except Exception as exc:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        console.print(f"[red]Error loading config: {exc}[/red]")
        sys.exit(1)

    console.print(f"[blue]Checking {config.name} ({config.runtime})...[/blue]")

    all_ok = True

    # Container backend binary (docker / podman / apptainer)
    backend = config.runtime or "docker"
    backend_bin = shutil.which(backend)
    if backend_bin:
        console.print(f"  {backend + ':':30s} [green]OK ({backend_bin})[/green]")
    else:
        all_ok = False
        console.print(f"  {backend + ':':30s} [red]FAIL ({backend} not found)[/red]")

    # Python (used by hooks / pre-start scripts)
    try:
        proc = subprocess.run(
            ["python3", "--version"], capture_output=True, text=True, timeout=5
        )
        if proc.returncode == 0:
            console.print(
                f"  {'python:':30s} [green]OK ({proc.stdout.strip()})[/green]"
            )
        else:
            all_ok = False
            console.print(f"  {'python:':30s} [red]FAIL[/red]")
    except (
        FileNotFoundError
    ):  # stx-allow: fallback (reason: file may not exist on first use)
        all_ok = False
        console.print(f"  {'python:':30s} [red]FAIL (python3 not found)[/red]")

    if all_ok:
        console.print("[green]Ready to deploy.[/green]")
    else:
        console.print(
            "[red]Preflight checks failed. Fix the issues above before deploying.[/red]"
        )
        sys.exit(1)


@click.command()
@click.argument("name_or_path", type=str)
def validate(name_or_path: str) -> None:
    """Validate a YAML config file.

    Accepts either a bare agent name (resolved against the search chain)
    or an explicit path to ``spec.yaml``.

    \b
    Example:
      $ sac agent validate orchestrator
      $ sac agent validate ~/.scitex/agent-container/agents/foo/spec.yaml
    """
    try:
        config_path = resolve_config(name_or_path)
    except Exception as exc:  # stx-allow: fallback (reason: not-found / unresolvable name surfaced to user)
        console.print(f"[red]Error: {exc}[/red]")
        sys.exit(1)
    errors = validate_config(config_path)
    if not errors:
        console.print(f"[green]Config is valid: {config_path}[/green]")
    else:
        console.print(f"[red]Config validation failed: {config_path}[/red]")
        for error in errors:
            console.print(f"  [red]- {error}[/red]")
        sys.exit(1)


# Layered runtime images — :base is the OS+dev-tools layer, :scitex
# is the default user-facing image (FROM :base + scitex[all] + sac).
_TARGET_DOCKERFILES = {
    "base": "Dockerfile.base",
    "scitex": "Dockerfile.scitex",
}

# Container engines all default to :scitex.
_RUNTIME_TO_TARGET = {
    "docker": "scitex",
    "podman": "scitex",
    "apptainer": "scitex",
}


@click.command(name="build-image")
@click.option(
    "--runtime",
    type=click.Choice(["docker", "apptainer"]),
    default="docker",
    help="Container engine to build for.",
)
@click.option(
    "--target",
    type=click.Choice(sorted(_TARGET_DOCKERFILES)),
    default="scitex",
    help="Which layered image to build (base or scitex).",
)
@click.option(
    "--image",
    default=None,
    help="Image name/tag (default: scitex-agent-container:<target>).",
)
@click.option(
    "--dry-run",
    "dry_run",
    is_flag=True,
    default=False,
    help="Print what would be built without invoking the container runtime.",
)
@click.option(
    "-y",
    "--yes",
    "yes",
    is_flag=True,
    default=False,
    help="Skip confirmation prompt.",
)
def build(
    runtime: str,
    target: str,
    image: str | None,
    dry_run: bool,
    yes: bool,
) -> None:
    """Build a layered runtime image.

    \b
    Example:
      $ sac image build                              # scitex (default)
      $ sac image build --target base                # OS + tools only
      $ sac image build --runtime apptainer
      $ sac image build --dry-run
    """
    if image is None:
        image = f"scitex-agent-container:{target}"

    if dry_run:
        click.echo(f"[dry-run] would build {runtime} image '{image}' (target={target})")
        return
    if not yes:
        click.echo(
            f"Refusing to build {runtime} image '{image}' (target={target}) without --yes/-y.",
            err=True,
        )
        raise SystemExit(2)
    containers_dir = Path(__file__).resolve().parent.parent.parent.parent / "containers"
    dockerfile = containers_dir / _TARGET_DOCKERFILES[target]

    if runtime == "docker":
        from ..runtimes.docker import DockerRuntime

        console.print(f"[blue]Building Docker image: {image} from {dockerfile}[/blue]")
        success = DockerRuntime.build_image(
            image=image,
            context=str(containers_dir),
            dockerfile=str(dockerfile),
        )
        if success:
            console.print(f"[green]Docker image built: {image}[/green]")
        else:
            console.print("[red]Docker build failed[/red]")
            sys.exit(1)
    elif runtime == "apptainer":
        import subprocess as _sp

        def_file = str(containers_dir / "apptainer.def")
        sif_path = str(containers_dir / "scitex-agent-container.sif")
        console.print(f"[blue]Building Apptainer image: {sif_path}[/blue]")
        result = _sp.run(["apptainer", "build", sif_path, def_file], text=True)
        if result.returncode == 0:
            console.print(f"[green]Apptainer image built: {sif_path}[/green]")
        else:
            console.print("[red]Apptainer build failed[/red]")
            sys.exit(1)


def target_for_runtime(runtime: str | None) -> str | None:
    """Return the ``--target`` name for a yaml ``spec.runtime`` value.

    Used by F-CS16 phase 2's container dispatch to pick the right
    image when the yaml didn't specify ``spec.container.image`` itself.
    Returns ``None`` for runtimes that don't map (slurm / slurm-tenant /
    unknown).
    """
    return _RUNTIME_TO_TARGET.get(runtime or "")
