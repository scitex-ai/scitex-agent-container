"""``sac image switch / rollback`` for SAC's layered image store."""

from __future__ import annotations

import json

import click

from .._logging import render_rich
from . import _image_activation
from ._helpers import _json_flag


def _preview_or_require_approval(
    action: str, *, layer: str, version: str | None, dry_run: bool, yes: bool
) -> bool:
    if dry_run and yes:
        raise click.UsageError("--dry-run cannot be combined with --yes/-y")
    if dry_run:
        click.echo(
            json.dumps(
                {
                    "action": action,
                    "layer": layer,
                    "version": version,
                    "dry_run": True,
                    "scope": "arguments_only",
                    "verified": False,
                },
                indent=2,
            )
        )
        return True
    if not yes:
        raise click.UsageError(
            "Image activation requires --yes/-y; use --dry-run to preview."
        )
    return False


@click.command("switch")
@click.argument("version", type=str)
@click.option(
    "--layer",
    type=click.Choice(_image_activation.LAYERS),
    default="base",
    show_default=True,
    help="SAC image layer to switch.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Describe arguments without inspecting or locking the store.",
)
@click.option("--yes", "-y", is_flag=True, help="Approve the live-link change.")
@click.option(
    "--json", "as_json", is_flag=True, help="Emit the activation result as JSON."
)
@click.pass_context
def image_switch(
    ctx: click.Context,
    version: str,
    layer: str,
    dry_run: bool,
    yes: bool,
    as_json: bool,
) -> None:
    """Atomically switch one SAC layer to VERSION after approval.

    --dry-run describes arguments only; it does not inspect artifacts, acquire
    a store lock or validate version availability. Approval conflicts with
    --dry-run. Actual switching retains the existing backend checks.

    \b
    Example:
      $ sac image switch 2026-0914-152140 --layer base --yes
    """
    if _preview_or_require_approval(
        "switch", layer=layer, version=version, dry_run=dry_run, yes=yes
    ):
        return
    from . import image_group as ig

    switched = _image_activation.switch_layer_version(
        containers_dir=ig._CONTAINERS_DIR, layer=layer, version=version
    )
    if _json_flag(ctx, as_json):
        click.echo(
            json.dumps({"action": "switch", "layer": layer, "version": switched})
        )
        return
    render_rich(f"[green]switched[/green] {layer} -> {switched}", __name__)


@click.command("rollback")
@click.option(
    "--layer",
    type=click.Choice(_image_activation.LAYERS),
    default="base",
    show_default=True,
    help="SAC image layer to roll back.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Describe arguments without selecting or locking an artifact.",
)
@click.option("--yes", "-y", is_flag=True, help="Approve the live-link rollback.")
@click.option(
    "--json", "as_json", is_flag=True, help="Emit the activation result as JSON."
)
@click.pass_context
def image_rollback(
    ctx: click.Context, layer: str, dry_run: bool, yes: bool, as_json: bool
) -> None:
    """Restore the previous version of one SAC layer after approval.

    --dry-run describes arguments only; it does not inspect live links, select
    an older artifact or acquire a lock. Approval conflicts with --dry-run.
    The backend chooses the immediately older artifact when applying.

    \b
    Example:
      $ sac image rollback --layer base --yes
    """
    if _preview_or_require_approval(
        "rollback", layer=layer, version=None, dry_run=dry_run, yes=yes
    ):
        return
    from . import image_group as ig

    previous = _image_activation.rollback_layer(
        containers_dir=ig._CONTAINERS_DIR, layer=layer
    )
    if _json_flag(ctx, as_json):
        click.echo(
            json.dumps({"action": "rollback", "layer": layer, "version": previous})
        )
        return
    render_rich(f"[green]rolled back[/green] {layer} -> {previous}", __name__)


__all__ = ["image_rollback", "image_switch"]
