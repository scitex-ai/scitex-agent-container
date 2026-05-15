"""``sac.template`` — template-rendering verbs as bare names.

Mirrors the F-CS15 ``template`` noun group exposed by the MCP server.
The CLI surface for ``sac template render-contributor-spec`` was
retired in the F-CS17 cleanup pass, but the Python API and MCP tool
are retained per the F-CS15 noun-group contract.
"""

from .._mcp._tools._template import (
    template_render_contributor_spec as render_contributor_spec,
)

__all__ = ["render_contributor_spec"]
