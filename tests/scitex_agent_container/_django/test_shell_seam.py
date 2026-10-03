"""The documented SDK app-shell seam: one template per page, no host branch.

Every leaf page template extends ``scitex_sdk/app/app_shell.html`` and fills
``scitex_app_content``. This package names no host shell (no ``global_base``,
no ``*_hub.html``, no direct standalone-shell extends): a host maps the
content block into its own chrome by shadowing the adapter with a project
DIRS template; standalone serving uses the SDK-owned shell through the same
adapter. These tests pin that contract from source plus one live render each
way (SDK shell, host shadow), all against the controlled loopback listener.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from scitex_agent_container._django._constants import IDENTITY_ENV

TEMPLATES = (
    Path(__file__).resolve().parents[3]
    / "src"
    / "scitex_agent_container"
    / "_django"
    / "templates"
    / "scitex_agent_container"
)

PAGES = ["fleet", "timeline", "detail", "launch", "create", "a2a"]
ADAPTER = "scitex_sdk/app/app_shell.html"


def test_no_template_names_a_host_shell():
    offenders = [
        p.name
        for p in TEMPLATES.glob("*.html")
        if "global_base" in p.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_no_hub_templates_remain():
    assert list(TEMPLATES.glob("*_hub.html")) == []


@pytest.mark.parametrize("page", PAGES)
def test_page_extends_the_sdk_adapter_and_fills_its_content_block(page):
    body = (TEMPLATES / f"{page}.html").read_text(encoding="utf-8")
    assert f'{{% extends "{ADAPTER}" %}}' in body
    assert "{% block scitex_app_content %}" in body


def test_app_config_uses_the_public_sdk_contract():
    apps_py = (
        Path(__file__).resolve().parents[3]
        / "src"
        / "scitex_agent_container"
        / "_django"
        / "apps.py"
    ).read_text(encoding="utf-8")
    assert "from scitex_sdk.app.embed import ScitexAppConfig" in apps_py
    assert "scitex_app" not in apps_py


def test_fleet_renders_through_the_sdk_shell(client, loopback, env_save_restore):
    """Standalone: the adapter delegates to the SDK-owned UI shell."""
    env_save_restore.set(IDENTITY_ENV, "alice")
    html = client.get("/").content.decode()
    assert 'name="stx-mount"' in html
    assert "agents-app" in html  # the leaf content block rendered
    assert "agents.css" in html  # the leaf extra_css slot survived the adapter


def test_host_shadow_maps_the_content_block_into_host_chrome(client, loopback, env_save_restore, tmp_path):
    """Host: shadowing the adapter re-chromes the SAME leaf content.

    A project DIRS template named ``scitex_sdk/app/app_shell.html`` wins over
    the SDK's (project DIRS win), so the host owns the chrome while the leaf
    keeps serving content only — the documented generic seam, no leaf change.
    """
    from django.test import override_settings

    env_save_restore.set(IDENTITY_ENV, "alice")
    shadow_dir = tmp_path / "host_templates" / "scitex_sdk" / "app"
    shadow_dir.mkdir(parents=True)
    (shadow_dir / "app_shell.html").write_text(
        "<html><head>{% block extra_css %}{% endblock %}"
        "{% block extra_js %}{% endblock %}</head>"
        "<body data-host-chrome='1'>"
        "{% block scitex_app_content %}{% endblock %}"
        "</body></html>",
        encoding="utf-8",
    )
    base_templates = list(__import__("django").conf.settings.TEMPLATES)
    shadowed = [dict(base_templates[0], DIRS=[str(tmp_path / "host_templates")])]
    with override_settings(TEMPLATES=shadowed):
        html = client.get("/").content.decode()
    assert "data-host-chrome='1'" in html
    assert "agents-app" in html  # leaf content survived the re-chrome
    assert "agents.css" in html  # the leaf extra_css slot survived the re-chrome
    assert 'name="stx-mount"' not in html  # host owns the shell now
