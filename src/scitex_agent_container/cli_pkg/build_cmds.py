"""Build/validation commands: check, validate, build."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import click

from ..config import load_config, validate_config
from ._helpers import console


@click.command()
@click.argument("config_path", type=str)
def check(config_path: str) -> None:
    """Run preflight checks for an agent deployment.

    Verifies that all dependencies (SSH, screen, python, etc.) are
    available before starting the agent. Useful for debugging deployment
    failures.

    \b
    Example:
      $ sac check ~/.scitex/agent-container/agents/foo/foo.yaml
    """
    # stx-allow: fallback (reason: config file may not exist or contain invalid YAML; CLI exits with code 1 to signal preflight failure)
    try:
        config = load_config(config_path)
    except Exception as exc:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        console.print(f"[red]Error loading config: {exc}[/red]")
        sys.exit(1)

    console.print(
        f"[blue]Checking {config.name}"
        + (
            f" (remote: {config.remote.host})"
            if config.remote.is_remote
            else " (local)"
        )
        + "...[/blue]"
    )

    all_ok = True

    if config.remote.is_remote:
        from ..runtimes.claude_code import _SSHRemote

        results = _SSHRemote.preflight(config)
        for name, passed, detail in results:
            if passed:
                console.print(f"  {name + ':':30s} [green]{detail}[/green]")
            else:
                all_ok = False
                console.print(f"  {name + ':':30s} [red]FAIL[/red]")
                for line in detail.split("\n"):
                    console.print(f"    [red]{line}[/red]")
    else:
        # Local checks
        screen_bin = shutil.which("screen")
        if screen_bin:
            console.print(f"  {'screen:':30s} [green]OK ({screen_bin})[/green]")
        else:
            all_ok = False
            console.print(f"  {'screen:':30s} [red]FAIL[/red]")
            console.print("    [red]GNU screen not found[/red]")
            console.print("    [red]  Fix: sudo apt install screen[/red]")

        try:
            proc = subprocess.run(
                ["python3", "--version"],
                capture_output=True,
                text=True,
                timeout=5,
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

        sac_bin = shutil.which("scitex-agent-container")
        if sac_bin:
            # stx-allow: fallback (reason: subprocess to get version may fail due to permission or env issues; "unknown" version is safe for the preflight display)
            try:
                proc = subprocess.run(
                    ["scitex-agent-container", "--version"],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                ver = proc.stdout.strip() if proc.returncode == 0 else "unknown"
            except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
                ver = "unknown"
            console.print(
                f"  {'scitex-agent-container:':30s} [green]OK ({ver})[/green]"
            )
        else:
            all_ok = False
            console.print(f"  {'scitex-agent-container:':30s} [red]FAIL[/red]")
            console.print("    [red]  Fix: pip install scitex-agent-container[/red]")

        # stx-allow: fallback (reason: df may be unavailable or timeout in restricted environments; showing "unknown" disk status is acceptable for a preflight report)
        try:
            proc = subprocess.run(
                ["df", "-h", "/"], capture_output=True, text=True, timeout=5
            )
            if proc.returncode == 0:
                lines = proc.stdout.strip().split("\n")
                if len(lines) >= 2:
                    parts = lines[1].split()
                    usage = parts[4] if len(parts) >= 5 else "unknown"
                    console.print(
                        f"  {'disk space:':30s} [green]OK ({usage} used)[/green]"
                    )
        except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
            console.print(f"  {'disk space:':30s} [dim]unknown[/dim]")

    if all_ok:
        console.print("[green]Ready to deploy.[/green]")
    else:
        console.print(
            "[red]Preflight checks failed. Fix the issues above before deploying.[/red]"
        )
        sys.exit(1)


@click.command()
@click.argument("config_path", type=str)
def validate(config_path: str) -> None:
    """Validate a YAML config file.

    \b
    Example:
      $ sac validate ~/.scitex/agent-container/agents/foo/foo.yaml
    """
    errors = validate_config(config_path)
    if not errors:
        console.print(f"[green]Config is valid: {config_path}[/green]")
    else:
        console.print(f"[red]Config validation failed: {config_path}[/red]")
        for error in errors:
            console.print(f"  [red]- {error}[/red]")
        sys.exit(1)


@click.command()
@click.option(
    "--runtime",
    type=click.Choice(["docker", "apptainer"]),
    default="docker",
    help="Container runtime to build for.",
)
@click.option(
    "--image",
    default="scitex-agent-container:latest",
    help="Image name/tag.",
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
def build(runtime: str, image: str, dry_run: bool, yes: bool) -> None:
    """Build container base image.

    \b
    Example:
      $ sac build
      $ sac build --runtime apptainer
      $ sac build --dry-run
    """
    if dry_run:
        click.echo(f"[dry-run] would build {runtime} image '{image}'")
        return
    if not yes and not click.confirm(f"Build {runtime} image '{image}'?", default=True):
        click.echo("Aborted.")
        return
    containers_dir = Path(__file__).resolve().parent.parent.parent.parent / "containers"

    if runtime == "docker":
        from ..runtimes.docker import DockerRuntime

        console.print(f"[blue]Building Docker image: {image}[/blue]")
        success = DockerRuntime.build_image(image=image, context=str(containers_dir))
        if success:
            console.print(f"[green]Docker image built: {image}[/green]")
        else:
            console.print("[red]Docker build failed[/red]")
            sys.exit(1)
    elif runtime == "apptainer":
        from ..runtimes.apptainer import ApptainerRuntime

        def_file = str(containers_dir / "apptainer.def")
        sif_path = str(containers_dir / "claude-code-container.sif")
        console.print(f"[blue]Building Apptainer image: {sif_path}[/blue]")
        success = ApptainerRuntime.build_image(def_file=def_file, sif_path=sif_path)
        if success:
            console.print(f"[green]Apptainer image built: {sif_path}[/green]")
        else:
            console.print("[red]Apptainer build failed[/red]")
            sys.exit(1)
