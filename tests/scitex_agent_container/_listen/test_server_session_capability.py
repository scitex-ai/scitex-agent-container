"""Typed session discovery uses the real existing bearer and health boundary."""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.routing import Route
from starlette.testclient import TestClient

from scitex_agent_container._listen.auth import BearerAuthMiddleware
from scitex_agent_container._listen.server import health


def health_client():
    app = Starlette(routes=[Route("/v1/health", health)])
    app.add_middleware(BearerAuthMiddleware, token="synthetic-host")
    return TestClient(app)


@pytest.mark.parametrize("headers", ({}, {"Authorization": "Bearer wrong-host"}))
def test_public_health_preserves_basic_response_without_mutation_capability(headers):
    # Arrange
    with health_client() as client:
        # Act
        result = client.get("/v1/health", headers=headers)
    # Assert
    assert (result.status_code, result.json()) == (
        200,
        {"ok": True, "service": "sac-listen", "v": 1},
    )


def test_verified_host_bearer_exposes_the_typed_session_protocol():
    # Arrange
    with health_client() as client:
        # Act
        result = client.get(
            "/v1/health", headers={"Authorization": "Bearer synthetic-host"}
        )
    # Assert
    assert result.json()["capabilities"] == {
        "start_session": {
            "protocol": "sac.start-session/v1",
            "modes": ["continue", "resume", "fresh"],
        }
    }
