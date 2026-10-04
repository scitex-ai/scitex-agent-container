"""Leaf-owned fleet content for an admitted SDK workspace mount."""

from __future__ import annotations

from django.shortcuts import render
from django.views.decorators.http import require_GET

from .views import _fleet_context


def _mount_base(prefix: str) -> str:
    """Accept a path prefix supplied by the host's registered mount contract."""
    if not isinstance(prefix, str):
        raise ValueError("The registered SAC mount must be a path prefix")
    if prefix in ("", "/"):
        return ""
    if (
        not prefix.startswith("/")
        or prefix.startswith("//")
        or any(char in prefix for char in "?%#\\")
        or any(char.isspace() for char in prefix)
        or any(ord(char) < 32 or ord(char) == 127 for char in prefix)
        or any(part in {"", ".", ".."} for part in prefix.rstrip("/")[1:].split("/"))
    ):
        raise ValueError("The registered SAC mount must be a path prefix")
    return prefix.rstrip("/")


@require_GET
def render_content(request, current_project=None, *, stx_mount: str):
    """Render the same scoped fleet without selecting a host shell.

    The acting request identity determines visibility and control. A workspace
    project conveys no SAC grant. The host supplies its admitted API mount;
    the workspace page's location does not determine fleet links.
    """
    base = _mount_base(stx_mount)
    context = _fleet_context(request)
    context["url_base"] = base
    return render(request, "scitex_agent_container/_workspace_content.html", context)
