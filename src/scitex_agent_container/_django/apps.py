"""Django app configuration for the SAC Agents dashboard.

``scitex_ui`` is always listed in INSTALLED_APPS alongside this app (the
standalone launcher adds it automatically; a mounted host does the same).
``scitex_ui`` auto-wires its element-inspector middleware on ``ready()``, so
this config needs no body.

Subclasses ``scitex_app._django.ScitexAppConfig`` when scitex-app is
installed (the mounted-host case: the host lists the app on its launcher
from ``manifest.json`` and mounts its urls through the generic
``scitex.apps`` plugin mount), and falls back to Django's plain
``AppConfig`` otherwise — the standalone dashboard keeps working without a
hard scitex-app dependency (same idiom as scitex-cards).
"""

from __future__ import annotations

try:
    from scitex_app._django import ScitexAppConfig
except ImportError:  # scitex-app not installed — standalone still works
    try:
        from django.apps import AppConfig as ScitexAppConfig
    except ImportError as exc:
        raise ImportError(
            "SAC GUI dependencies are unavailable; install "
            "scitex-agent-container[gui]."
        ) from exc


class AgentContainerDashboardConfig(ScitexAppConfig):
    default = True
    name = "scitex_agent_container._django"
    label = "scitex_agent_container_django"
    verbose_name = "SciTeX Agents"


__all__ = ["AgentContainerDashboardConfig"]
