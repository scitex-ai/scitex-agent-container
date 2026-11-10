"""Writable sandbox creation, with a hidden compatibility spelling."""

from __future__ import annotations

import json
from copy import copy
from pathlib import Path

import click

from .._logging import render_rich


@click.command("create-sandbox")
@click.argument("source", type=str)
@click.option(
    "--output",
    "-o",
    type=click.Path(file_okay=False, path_type=Path),
    help="Output dir (default: <source-stem>.sandbox/ next to the SIF).",
)
@click.option(
    "--dry-run", is_flag=True, help="Preview arguments without resolving the image."
)
@click.option("--yes", "-y", is_flag=True, help="Approve writable sandbox creation.")
def image_create_sandbox(
    source: str, output: Path | None, dry_run: bool, yes: bool
) -> None:
    """Create a writable sandbox from SOURCE (a SIF path or layer name).

    Preview reports arguments only; it does not resolve the SIF, calculate the
    default destination, estimate disk space or check Apptainer readiness.
    Preview takes precedence over approval.

    \b
    Examples:
      $ sac image create-sandbox scitex --dry-run
      $ sac image create-sandbox scitex --yes
      $ sac image create-sandbox ./scitex.sif --output ./sandbox --yes
    """
    if click.get_current_context().info_name == "sandbox":
        click.echo("Deprecated: use 'sac image create-sandbox'.", err=True)
    if dry_run:
        click.echo(
            json.dumps(
                {
                    "action": "create-sandbox",
                    "source": source,
                    "output": str(output) if output is not None else None,
                    "dry_run": True,
                },
                indent=2,
            )
        )
        return
    if not yes:
        raise click.UsageError(
            "Sandbox creation requires --yes/-y; use --dry-run to preview."
        )

    from . import image_group as ig

    sandbox_create = ig._load_apptainer().sandbox_create
    src_path = ig._resolve_source_to_sif(source)
    result = sandbox_create(
        source=src_path, containers_dir=ig._CONTAINERS_DIR, output_dir=output
    )
    render_rich(f"[green]sandbox[/green] {result}", __name__)


def register(group: click.Group) -> None:
    group.add_command(image_create_sandbox)
    legacy = copy(image_create_sandbox)
    legacy.name = "sandbox"
    legacy.hidden = True
    group.add_command(legacy)
