"""Django settings for the Agents GUI test suite.

Used only when the GUI tests are run (``DJANGO_SETTINGS_MODULE`` is pointed at
this module). It mirrors what ``scitex_app._standalone.run_standalone`` builds
for the live server, so tests exercise the same app, not a divergent config.
"""

from __future__ import annotations

SECRET_KEY = "test-secret-not-for-production"
DEBUG = True
ALLOWED_HOSTS = ["*"]
USE_TZ = True

INSTALLED_APPS = [
    "django.contrib.staticfiles",
    "scitex_ui",
    "scitex_agent_container._django.apps.AgentContainerDashboardConfig",
]
MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    # No auth/CSRF in standalone; a mounted host adds those around the app.
]
ROOT_URLCONF = "scitex_agent_container._django.urls"
TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "scitex_ui.context_processors.element_inspector",
            ],
        },
    },
]
DATABASES: dict[str, dict[str, str]] = {}
STATIC_URL = "/static/"
STATICFILES_DIRS = []
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
