"""``sac image ...`` tools (F-CS15) — Python API + MCP wrappers.

NO ``runtime`` PARAMETER — and it is worth naming what was wrong, because
it was not one stale default but three:

  1. The signature advertised ``runtime: str = "docker"``. docker/podman
     were ripped out 2026-05-13, so the MCP surface offered an engine that
     does not exist AND made it the DEFAULT — the only value guaranteed to
     be wrong.
  2. ``sac image build`` has no ``--runtime`` flag, and no ``--target`` or
     ``--image`` either. The old argv passed all three, so EVERY call
     through this tool died on "no such option" before building anything.
     A tool whose documented behaviour cannot be reached is worse than a
     missing tool: it reads as a working capability.
  3. It named a ``target`` ("sdk-persistent") the CLI has not modelled
     since F-CS17; the real selector is a positional LAYER.

Now it mirrors ``sac image build`` exactly: positional layer, ``--sandbox``,
``--dry-run``. The engine is apptainer, unconditionally
(:mod:`config._container_engine`), so there is nothing to select — and the
parameter is REMOVED rather than pinned to one value, because a one-value
knob is still a knob to wonder about.
"""

from __future__ import annotations

from typing import Any

from ._helpers import invoke_cli_text


def image_build(
    layer: str = "base",
    sandbox: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Build an agent container SIF. Mirrors ``sac image build``.

    ``layer``: which recipe to build — ``base`` (default; OS + dev tools)
    or ``scitex`` (FROM :base + scitex[all]). Unknown layers are rejected
    by the CLI, which owns the layer list.

    ``sandbox``: build a writable sandbox directory instead of a SIF.
    ``dry_run``: print what would build, build nothing.

    There is no engine parameter: apptainer is the only container engine.
    """
    argv = ["image", "build", layer, "--yes"]
    if sandbox:
        argv.append("--sandbox")
    if dry_run:
        argv.append("--dry-run")
    return invoke_cli_text(argv)


def register_image_tools(mcp) -> None:
    mcp.tool()(image_build)


__all__ = ["image_build", "register_image_tools"]
