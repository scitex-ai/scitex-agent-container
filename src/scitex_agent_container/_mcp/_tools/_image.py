"""``sac image ...`` MCP tools (F-CS15)."""

from __future__ import annotations

from typing import Any

from ._helpers import invoke_cli_text


def register_image_tools(mcp) -> None:
    @mcp.tool()
    def sac_image_build(
        runtime: str = "docker",
        target: str = "sdk-persistent",
        image: str | None = None,
        dry_run: bool = False,
    ) -> dict[str, Any]:
        """Build the agent container image. Mirrors ``sac image build``.

        ``runtime``: docker | apptainer.
        ``target``: only ``sdk-persistent`` after F-CS17 stage 3d.
        """
        argv = ["image", "build", "--runtime", runtime, "--target", target, "--yes"]
        if image:
            argv += ["--image", image]
        if dry_run:
            argv.append("--dry-run")
        return invoke_cli_text(argv)


__all__ = ["register_image_tools"]
