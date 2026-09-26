"""Django app configuration for the SAC Agents dashboard.

``scitex_ui`` is always listed in INSTALLED_APPS alongside this app (the
standalone launcher adds it automatically; a mounted host does the same).
``scitex_ui`` auto-wires its element-inspector middleware on ``ready()``, so
this config needs no body.
"""

from __future__ import annotations

from django.apps import AppConfig


class AgentContainerDashboardConfig(AppConfig):
    default = True
    name = "scitex_agent_container._django"
    label = "scitex_agent_container_django"
    verbose_name = "SciTeX Agents"


__all__ = ["AgentContainerDashboardConfig"]
