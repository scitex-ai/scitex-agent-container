"""URL contract for the SAC Agents dashboard.

Mounted hosts prefix these under the app's route (e.g. ``/apps/agents/``); the
standalone server serves them at the root. Relative navigation in the templates
uses ``../`` so both layouts work.
"""

from __future__ import annotations

try:
    from django.urls import path
except ImportError as exc:
    raise ImportError(
        "SAC GUI dependencies are unavailable; install "
        "scitex-agent-container[gui]."
    ) from exc

from . import views

app_name = "scitex_agent_container"

urlpatterns = [
    path("", views.index, name="index"),
    path("api/fleet", views.fleet_api, name="fleet_api"),
    path("api/timeline", views.timeline_api, name="timeline_api"),
    path("timeline/", views.timeline, name="timeline"),
    # Static routes BEFORE <str:name>/: otherwise "launch"/"a2a" resolve as an
    # agent name and those surfaces are unreachable (a wiring bug).
    path("launch/", views.launch, name="launch"),
    path("create/", views.create_agent, name="create_agent"),
    path("a2a/", views.a2a_panel, name="a2a_panel"),
    path("a2a/action", views.a2a_action, name="a2a_action"),
    path("<str:name>/", views.detail, name="detail"),
    path("<str:name>/action", views.lifecycle_action, name="lifecycle_action"),
    path("<str:name>/message", views.message_action, name="message_action"),
    path("<str:name>/forget", views.forget_action, name="forget_action"),
    path("<str:name>/delete", views.delete_action, name="delete_action"),
]

__all__ = ["urlpatterns"]
