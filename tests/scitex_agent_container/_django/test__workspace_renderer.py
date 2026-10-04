"""Workspace parity over the original real loopback listener and fixtures."""

import pytest
from django.test import RequestFactory

from scitex_agent_container._django._constants import IDENTITY_ENV, OPERATORS_ENV
from scitex_agent_container._django._workspace_renderer import render_content


def _warm(client):
    response = client.get("/api/fleet")
    if response.status_code != 200:
        raise AssertionError("The original loopback fleet API did not succeed")


def test_workspace_mount_does_not_follow_workspace_page_location(
    client, loopback, env_save_restore
):
    # Arrange
    # The same acting identity and real fleet feed both surfaces.
    env_save_restore.set(IDENTITY_ENV, "alice")
    env_save_restore.delete(OPERATORS_ENV)
    _warm(client)
    standalone = client.get("/").content.decode()
    request = RequestFactory().get("/workspace/?app=agents")
    # Act
    # A workspace project adds no fleet grant or URL authority.
    workspace = render_content(
        request, object(), stx_mount="/custom/agents/"
    ).content.decode()
    # Assert
    # Both retain the same own-scope rows; links use the admitted mount.
    assert (
        "alpha" in standalone
        and "alpha" in workspace
        and "/gamma/" not in standalone
        and "/gamma/" not in workspace
        and 'href="/custom/agents/alpha/"' in workspace
        and 'href="/custom/agents/a2a/"' in workspace
        and request.path == "/workspace/"
        and "workspace-three-col" not in workspace
        and "agents.css" in workspace
    )


def test_cold_workspace_poll_uses_registered_mount(loopback, env_save_restore):
    # Arrange
    # Cold cache, unrelated workspace URL.
    env_save_restore.set(IDENTITY_ENV, "alice")
    request = RequestFactory().get("/workspace/")
    # Act
    html = render_content(request, None, stx_mount="/apps/agents/").content.decode()
    # Assert
    # The loading request uses the actual leaf API, not /workspace/api.
    assert 'var base = "/apps/agents";' in html and "/workspace/api" not in html


@pytest.mark.parametrize(
    "mount",
    [
        "https://host/agents",
        "//host/agents",
        "/../agents",
        "/agents?x=1",
        "/agents#x",
        "/agents%2f",
        "/agents\\x",
        "/agents\n",
    ],
)
def test_invalid_mount_raises_value_error(mount, listener_requests):
    # Arrange
    request = RequestFactory().get("/workspace/")
    # Act
    # Assert
    # Exactly one behavior: the invalid mount raises.
    with pytest.raises(ValueError):
        render_content(request, None, stx_mount=mount)


@pytest.mark.parametrize(
    "mount",
    [
        "https://host/agents",
        "//host/agents",
        "/../agents",
        "/agents?x=1",
        "/agents#x",
        "/agents%2f",
        "/agents\\x",
        "/agents\n",
    ],
)
def test_invalid_mount_performs_no_control_plane_read(mount, listener_requests):
    # Arrange
    request = RequestFactory().get("/workspace/")
    # Act: swallow the expected refusal; what matters is the listener log.
    try:
        render_content(request, None, stx_mount=mount)
    except ValueError:
        pass
    # Assert
    assert listener_requests == []


def test_workspace_renderer_rejects_post_before_read(listener_requests):
    # Arrange
    request = RequestFactory().post("/workspace/")
    # Act
    response = render_content(request, None, stx_mount="/apps/agents/")
    # Assert
    assert response.status_code == 405 and listener_requests == []
