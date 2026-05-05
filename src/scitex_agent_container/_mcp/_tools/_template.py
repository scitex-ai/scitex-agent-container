"""``sac template ...`` MCP tools (F-CS15)."""

from __future__ import annotations

from typing import Any

from ._helpers import invoke_cli_text


def register_template_tools(mcp) -> None:
    @mcp.tool()
    def sac_template_render_contributor_spec(
        project: str,
        branch_kind: str = "feature",
        branch_short: str | None = None,
        hosts: list[str] | None = None,
    ) -> dict[str, Any]:
        """Render a contributor agent YAML from the canonical template.
        Mirrors ``sac template render-contributor-spec``."""
        argv = [
            "template",
            "render-contributor-spec",
            "--project",
            project,
            "--branch-kind",
            branch_kind,
        ]
        if branch_short:
            argv += ["--branch-short", branch_short]
        for host in hosts or []:
            argv += ["--host", host]
        return invoke_cli_text(argv)


__all__ = ["register_template_tools"]
