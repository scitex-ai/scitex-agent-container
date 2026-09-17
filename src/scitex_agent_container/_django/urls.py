"""URL contract for the SAC Agents dashboard.

Mounted hosts prefix these under the app's route (e.g. ``/apps/agents/``); the
standalone server serves them at the root. Relative navigation in the templates
uses ``../`` so both layouts work.
"""

from __future__ import annotations

from django.urls import path

from . import views

app_name = "scitex_agent_container"

urlpatterns = [
    path("", views.index, name="index"),
    path("api/fleet", views.fleet_api, name="fleet_api"),
    path("api/timeline", views.timeline_api, name="timeline_api"),
    path("timeline/", views.timeline, name="timeline"),
    path("<str:name>/", views.detail, name="detail"),
    path("<str:name>/action", views.lifecycle_action, name="lifecycle_action"),
]

__all__ = ["urlpatterns"]
