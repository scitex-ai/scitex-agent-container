"""Browser-facing SciTeX Agents app (scitex-app + scitex-ui).

A server-rendered projection of the SAC host control plane
(``sac listen``). It introduces NO second state store: every observation is
read live from the committed public surface (``GET /agents`` and
``GET /agents/<name>/status``), and every lifecycle mutation is delegated back
to that same authenticated listener. See ``manifest.json`` and ``docs/gui.md``.
"""

partial_template = "scitex_agent_container/_workspace_content.html"
content_renderer = "scitex_agent_container._django._workspace_renderer.render_content"

__all__ = ["partial_template", "content_renderer"]
