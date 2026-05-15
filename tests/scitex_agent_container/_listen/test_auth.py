"""Bearer-token auth middleware coverage for ``_listen.auth``.

Drives the real :class:`BearerAuthMiddleware` mounted on a minimal real
Starlette app through :class:`starlette.testclient.TestClient`. No mocks:
the middleware, request parsing, and response codes are all the genuine
production code paths.
"""

from __future__ import annotations

import pytest
from starlette.applications import Starlette
from starlette.responses import JSONResponse
from starlette.routing import Route
from starlette.testclient import TestClient

from scitex_agent_container._listen.auth import (
    BearerAuthMiddleware,
    _extract_bearer,
)

TOKEN = "secret-token-xyz-987"


# --- Helpers ---------------------------------------------------------------


def _build_client(token: str = TOKEN) -> TestClient:
    async def ok(_request):
        return JSONResponse({"ok": True})

    async def health(_request):
        return JSONResponse({"status": "up"})

    app = Starlette(
        routes=[
            Route("/v1/echo", ok),
            Route("/v1/health", health),
        ]
    )
    app.add_middleware(BearerAuthMiddleware, token=token)
    return TestClient(app)


class _FakeRequest:
    """Tiny stand-in exposing only ``.headers`` for ``_extract_bearer``.

    This is not a mock of the unit under test; it is a real Starlette-shaped
    container that supplies the lone attribute the helper reads.
    """

    def __init__(self, auth_header: str | None) -> None:
        self.headers = {} if auth_header is None else {"authorization": auth_header}


# --- _extract_bearer unit cases -------------------------------------------


def test_extract_bearer_returns_none_when_header_absent():
    # Arrange
    req = _FakeRequest(auth_header=None)
    # Act
    got = _extract_bearer(req)  # type: ignore[arg-type]
    # Assert
    assert got is None


def test_extract_bearer_parses_valid_header_value():
    # Arrange
    req = _FakeRequest(auth_header="Bearer abc123")
    # Act
    got = _extract_bearer(req)  # type: ignore[arg-type]
    # Assert
    assert got == "abc123"


def test_extract_bearer_is_scheme_case_insensitive():
    # Arrange
    req = _FakeRequest(auth_header="bEaReR tok-2")
    # Act
    got = _extract_bearer(req)  # type: ignore[arg-type]
    # Assert
    assert got == "tok-2"


def test_extract_bearer_rejects_non_bearer_scheme():
    # Arrange
    req = _FakeRequest(auth_header="Basic dXNlcjpwYXNz")
    # Act
    got = _extract_bearer(req)  # type: ignore[arg-type]
    # Assert
    assert got is None


def test_extract_bearer_rejects_single_token_header():
    # Arrange
    req = _FakeRequest(auth_header="Bearer")
    # Act
    got = _extract_bearer(req)  # type: ignore[arg-type]
    # Assert
    assert got is None


def test_extract_bearer_rejects_empty_token_value():
    # Arrange
    req = _FakeRequest(auth_header="Bearer    ")
    # Act
    got = _extract_bearer(req)  # type: ignore[arg-type]
    # Assert
    assert got is None


# --- Middleware integration cases -----------------------------------------


@pytest.fixture
def client() -> TestClient:
    return _build_client()


def test_valid_bearer_token_allows_request(client: TestClient):
    # Arrange
    headers = {"Authorization": f"Bearer {TOKEN}"}
    # Act
    resp = client.get("/v1/echo", headers=headers)
    # Assert
    assert resp.status_code == 200


def test_missing_authorization_header_returns_401(client: TestClient):
    # Arrange
    # (no headers)
    # Act
    resp = client.get("/v1/echo")
    # Assert
    assert resp.status_code == 401


def test_missing_header_response_body_explains_missing(client: TestClient):
    # Arrange
    # Act
    resp = client.get("/v1/echo")
    # Assert
    assert resp.json() == {"error": "missing bearer token"}


def test_malformed_authorization_header_returns_401(client: TestClient):
    # Arrange
    headers = {"Authorization": "NotBearer foo"}
    # Act
    resp = client.get("/v1/echo", headers=headers)
    # Assert
    assert resp.status_code == 401


def test_empty_bearer_value_returns_401(client: TestClient):
    # Arrange
    headers = {"Authorization": "Bearer   "}
    # Act
    resp = client.get("/v1/echo", headers=headers)
    # Assert
    assert resp.status_code == 401


def test_wrong_bearer_token_returns_403(client: TestClient):
    # Arrange
    headers = {"Authorization": "Bearer wrong-token-value"}
    # Act
    resp = client.get("/v1/echo", headers=headers)
    # Assert
    assert resp.status_code == 403


def test_wrong_token_response_body_explains_invalid(client: TestClient):
    # Arrange
    headers = {"Authorization": "Bearer wrong-token-value"}
    # Act
    resp = client.get("/v1/echo", headers=headers)
    # Assert
    assert resp.json() == {"error": "invalid bearer token"}


def test_health_endpoint_skips_auth_entirely(client: TestClient):
    # Arrange
    # (no Authorization header at all)
    # Act
    resp = client.get("/v1/health")
    # Assert
    assert resp.status_code == 200


def test_token_rotation_invalidates_old_token():
    # Arrange
    client_old = _build_client(token="old-token")
    client_new = _build_client(token="new-token")
    headers_old = {"Authorization": "Bearer old-token"}
    # Act
    resp = client_new.get("/v1/echo", headers=headers_old)
    # Assert
    assert resp.status_code == 403


def test_token_rotation_accepts_new_token():
    # Arrange
    client_new = _build_client(token="new-token")
    headers_new = {"Authorization": "Bearer new-token"}
    # Act
    resp = client_new.get("/v1/echo", headers=headers_new)
    # Assert
    assert resp.status_code == 200
