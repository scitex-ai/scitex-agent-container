"""Dual-mode (standalone vs Hub-mounted) rendering contract.

The package must render EXACTLY ONE shell per mode:
  * standalone (root mount)     -> extends scitex_ui/standalone_shell.html
  * Hub-mounted (/apps/agents/) -> extends the Hub's global_base.html
Mounting must NOT duplicate the header or add a project switcher.
"""

from __future__ import annotations

import pytest
from django.test import Client, override_settings


@pytest.fixture
def hub_client(tmp_path, fake_fleet):
    """A client whose urlconf mounts the app under /apps/agents/ and whose
    global_base.html is a minimal stub proving the content lands in the Hub
    shell (not the standalone shell)."""
    # Stub the Hub's global_base so we can assert which template chain ran.
    templates_dir = tmp_path / "hub_templates"
    templates_dir.mkdir()
    (templates_dir / "global_base.html").write_text(
        "<html><head>{% block head_extra %}{% endblock %}{% block extra_css %}{% endblock %}"
        "</head><body><div id=\"hub-global-header\">HUB-SHELL</div>"
        "{% block content %}{% endblock %}</body></html>",
        encoding="utf-8",
    )

    # A test urlconf that prefixes the app, mirroring scitex-hub's /apps/<slug>/.
    test_urls = tmp_path / "test_urls.py"
    test_urls.write_text(
        "from django.urls import include, path\n"
        "urlpatterns = [path('apps/agents/', include('scitex_agent_container._django.urls'))]\n",
        encoding="utf-8",
    )
    import sys

    sys.path.insert(0, str(tmp_path))
    try:
        with override_settings(
            TEMPLATES=[
                {
                    "BACKEND": "django.template.backends.django.DjangoTemplates",
                    "DIRS": [str(templates_dir)],
                    "APP_DIRS": True,
                    "OPTIONS": {"context_processors": ["django.template.context_processors.request"]},
                }
            ],
            ROOT_URLCONF="test_urls",
        ):
            yield Client()
    finally:
        sys.path.remove(str(tmp_path))


def test_mounted_index_uses_hub_shell_not_standalone(hub_client, monkeypatch):
    monkeypatch.setenv("SCITEX_AGENT_CONTAINER_GUI_IDENTITY", "alice")
    resp = hub_client.get("/apps/agents/")
    assert resp.status_code == 200
    html = resp.content.decode()
    # The Hub's global_base header is present...
    assert 'id="hub-global-header"' in html
    # ...and the standalone shell's workspace markup is NOT (no duplicated
    # header / three-column workspace from standalone_shell.html).
    assert "workspace-three-col" not in html
    # Content rendered into the Hub shell.
    assert 'class="agents-app"' in html
    assert "Agents" in html


def test_mounted_links_are_prefix_aware(hub_client, monkeypatch):
    monkeypatch.setenv("SCITEX_AGENT_CONTAINER_GUI_IDENTITY", "alice")
    html = hub_client.get("/apps/agents/").content.decode()
    # Per-row inspect links must be absolute-to-prefix, not bare "/alpha/".
    assert 'href="/apps/agents/alpha/"' in html
    assert 'href="/apps/agents/beta/"' in html


def test_mounted_detail_uses_hub_shell(hub_client, monkeypatch):
    monkeypatch.setenv("SCITEX_AGENT_CONTAINER_GUI_IDENTITY", "alice")
    html = hub_client.get("/apps/agents/alpha/").content.decode()
    assert 'id="hub-global-header"' in html
    assert "workspace-three-col" not in html
    assert 'class="agents-app" data-page="detail"' in html
    # back link points at the mounted fleet root
    assert 'href="/apps/agents/"' in html


def test_standalone_still_uses_standalone_shell(fake_fleet, monkeypatch):
    """Regression: at a root mount the standalone shell is used (one shell)."""
    import os

    os.environ["SCITEX_AGENT_CONTAINER_GUI_IDENTITY"] = "alice"
    from django.test import Client as _C

    resp = _C().get("/")
    html = resp.content.decode()
    assert "workspace-three-col" in html  # the standalone shell's marker
    assert 'class="agents-app"' in html
