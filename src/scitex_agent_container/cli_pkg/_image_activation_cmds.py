"""``sac image switch / rollback`` for SAC's layered image store."""

from __future__ import annotations

import click

from . import _image_activation
from ._helpers import console


@click.command("switch")
@click.argument("version", type=str)
@click.option(
    "--layer",
    type=click.Choice(_image_activation.LAYERS),
    default="base",
    show_default=True,
    help="SAC image layer to switch.",
)
def image_switch(version: str, layer: str) -> None:
    """Atomically switch one SAC layer to VERSION.

    \b
    Example:
      $ sac image switch 2026-0914-152140 --layer base
    """
    from . import image_group as ig

    switched = _image_activation.switch_layer_version(
        containers_dir=ig._CONTAINERS_DIR, layer=layer, version=version
    )
    console.print(f"[green]switched[/green] {layer} -> {switched}")


@click.command("rollback")
@click.option(
    "--layer",
    type=click.Choice(_image_activation.LAYERS),
    default="base",
    show_default=True,
    help="SAC image layer to roll back.",
)
def image_rollback(layer: str) -> None:
    """Restore the previous version of one SAC layer.

    \b
    Example:
      $ sac image rollback --layer base
    """
    from . import image_group as ig

    previous = _image_activation.rollback_layer(
        containers_dir=ig._CONTAINERS_DIR, layer=layer
    )
    console.print(f"[green]rolled back[/green] {layer} -> {previous}")


__all__ = ["image_rollback", "image_switch"]
