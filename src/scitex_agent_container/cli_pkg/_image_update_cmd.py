"""Explicit approval and an offline plan for sandbox package upgrades."""

from __future__ import annotations

import json
from pathlib import Path

import click


@click.command("update")
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
@click.option(
    "--dry-run", is_flag=True, help="Print arguments without loading the backend."
)
@click.option("--yes", "-y", is_flag=True, help="Approve the package upgrades.")
def image_update(
    sandbox_dir: Path, packages: tuple[str, ...], dry_run: bool, yes: bool
) -> None:
    """Refresh packages inside a writable sandbox after explicit approval.

    The preview reports arguments; it does not inspect installed packages,
    resolve versions, contact package indexes or check Apptainer readiness.
    Preview takes precedence over approval.

    \b
    Examples:
      $ sac image update sandbox/ --dry-run
      $ sac image update sandbox/ --yes
      $ sac image update sandbox/ -p scitex -p numpy --yes
    """
    pkgs = packages or ("scitex[all]",)
    if dry_run:
        click.echo(
            json.dumps(
                {
                    "action": "update",
                    "dry_run": True,
                    "sandbox_dir": str(sandbox_dir),
                    "packages": list(pkgs),
                },
                indent=2,
            )
        )
        return
    if not yes:
        raise click.UsageError(
            "Package upgrades require --yes/-y; use --dry-run to preview."
        )

    from . import image_group as ig

    sandbox_update = ig._load_apptainer().sandbox_update
    result = sandbox_update(sandbox_dir=sandbox_dir, packages=pkgs)
    click.echo(json.dumps(result, indent=2, default=str))
