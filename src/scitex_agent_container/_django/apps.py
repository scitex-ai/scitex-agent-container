"""Django app configuration for the SAC Agents dashboard.

Subclasses the public SDK contract ``scitex_sdk.app.embed.ScitexAppConfig``
(the mounted-host case: the host lists the app on its launcher from
``manifest.json`` and mounts its urls through the generic ``scitex.apps``
plugin mount), and falls back to Django's plain ``AppConfig`` otherwise — the
standalone dashboard keeps working without a hard SDK dependency. The SDK
contract loads ``manifest.json`` (slug, icon, scope, installed version via
``pip_package``), so the SDK's version/scope/locale discovery recognizes this
config by ``isinstance``.

``scitex_sdk.ui`` is always listed in INSTALLED_APPS alongside this app (the
standalone launcher adds it automatically; a mounted host does the same).
"""

from __future__ import annotations

try:
    from scitex_sdk.app.embed import ScitexAppConfig
except ImportError:  # scitex-sdk not installed — standalone still works
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
