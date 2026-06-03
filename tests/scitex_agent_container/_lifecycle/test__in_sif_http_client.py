"""Tests for :mod:`scitex_agent_container._lifecycle._in_sif_http_client`.

PR-3 Checkpoint 3 transport layer. Exercises the env / fail-loud /
non-2xx / transport-error branches without real network round-trips
by injecting a fake ``opener`` (no monkeypatching — PA-306). AAA +
one assert per test (PA-307).
"""

from __future__ import annotations

import io
from typing import Any
from urllib import error as urlerror

import pytest

from scitex_agent_container._lifecycle._in_sif_http_client import (
    HostListenTransportError,
    host_listen_call,
)

# ---------------------------------------------------------------------------
# Test doubles — fake urlopen-shaped opener
# ---------------------------------------------------------------------------


class _FakeResponse:
    """Minimal urllib-response-shaped object."""

    def __init__(self, status: int, body: bytes) -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *exc: Any) -> None:
        return None


def _opener_returning(status: int, body: bytes):
    """Build an opener that returns one canned response."""

    def _opener(_req, timeout=None):  # noqa: ARG001
        return _FakeResponse(status, body)

    return _opener


def _opener_raising(exc: Exception):
    """Build an opener that raises a transport-style exception."""

    def _opener(_req, timeout=None):  # noqa: ARG001
        raise exc

    return _opener


def _opener_raising_http(status: int, body: bytes):
    """Build an opener that raises ``urllib.error.HTTPError`` (urllib's
    way of surfacing non-2xx responses that DO carry a body).

    Tests need this because the real ``urlopen`` raises HTTPError on
    4xx/5xx with a readable body — :func:`host_listen_call` MUST
    catch that and return the ``(status, body)`` tuple, not bubble
    the exception.
    """

    def _opener(_req, timeout=None):  # noqa: ARG001
        raise urlerror.HTTPError(
            url="http://localhost/test",
            code=status,
            msg="error",
            hdrs={},  # type: ignore[arg-type]
            fp=io.BytesIO(body),
        )

    return _opener


# ---------------------------------------------------------------------------
# Happy path — 2xx
# ---------------------------------------------------------------------------


def test_returns_status_200_for_success_response() -> None:
    # Arrange
    opener = _opener_returning(200, b'{"ok": true}')
    # Act
    status, _ = host_listen_call(
        "GET", "/v1/health", base_url="http://x", bearer="", opener=opener
    )
    # Assert
    assert status == 200


def test_returns_parsed_json_body_on_success() -> None:
    # Arrange
    opener = _opener_returning(200, b'{"name": "child", "started": true}')
    # Act
    _, body = host_listen_call(
        "GET", "/agents/child", base_url="http://x", bearer="", opener=opener
    )
    # Assert
    assert body == {"name": "child", "started": True}


def test_returns_none_body_when_response_empty() -> None:
    # Arrange
    opener = _opener_returning(204, b"")
    # Act
    _, body = host_listen_call(
        "DELETE", "/agents/x", base_url="http://x", bearer="", opener=opener
    )
    # Assert
    assert body is None


# ---------------------------------------------------------------------------
# Non-2xx HTTP responses → returned, NOT raised
# ---------------------------------------------------------------------------


def test_non_2xx_status_is_returned_not_raised() -> None:
    # Arrange — 403 with a structured ACL deny body; the verb
    # layer needs both the status and the body to map an
    # InSifOutcome, so we must NOT raise.
    opener = _opener_raising_http(
        403, b'{"error": "ACL deny", "kind": "acl_deny", "reason": "..."}'
    )
    # Act
    status, _ = host_listen_call(
        "DELETE", "/agents/x", base_url="http://x", bearer="", opener=opener
    )
    # Assert
    assert status == 403


def test_non_2xx_body_is_returned_parsed() -> None:
    # Arrange — preflight-shaped 400 body.
    body_bytes = b'{"error": "...", "kind": "bind_unresolvable", "details": {}}'
    opener = _opener_raising_http(400, body_bytes)
    # Act
    _, body = host_listen_call(
        "POST",
        "/agents",
        body={"name": "x"},
        base_url="http://x",
        bearer="",
        opener=opener,
    )
    # Assert
    assert body["kind"] == "bind_unresolvable"


# ---------------------------------------------------------------------------
# Transport errors → HostListenTransportError with url echoed
# ---------------------------------------------------------------------------


def test_url_error_raises_transport_error() -> None:
    # Arrange — host listen refused / DNS / etc.
    opener = _opener_raising(urlerror.URLError("Connection refused"))
    # Act
    # Assert
    with pytest.raises(HostListenTransportError):
        host_listen_call(
            "GET", "/v1/health", base_url="http://x", bearer="", opener=opener
        )


def test_url_error_transport_error_carries_url() -> None:
    # Arrange — the operator needs to see WHAT was tried for
    # debug-without-ssh.
    opener = _opener_raising(urlerror.URLError("refused"))
    # Act
    raised: HostListenTransportError | None = None
    try:
        host_listen_call(
            "GET", "/agents/x", base_url="http://my-host", bearer="", opener=opener
        )
    except HostListenTransportError as exc:
        raised = exc
    # Assert
    assert raised is not None and raised.url == "http://my-host/agents/x"


def test_timeout_raises_transport_error() -> None:
    # Arrange
    opener = _opener_raising(TimeoutError("timed out"))
    # Act
    # Assert
    with pytest.raises(HostListenTransportError):
        host_listen_call(
            "GET", "/v1/health", base_url="http://x", bearer="", opener=opener
        )


def test_oserror_raises_transport_error() -> None:
    # Arrange — broader socket-level failure (network unreachable).
    opener = _opener_raising(OSError("network is unreachable"))
    # Act
    # Assert
    with pytest.raises(HostListenTransportError):
        host_listen_call(
            "GET", "/v1/health", base_url="http://x", bearer="", opener=opener
        )


# ---------------------------------------------------------------------------
# Missing base URL → fail loud
# ---------------------------------------------------------------------------


def test_missing_base_url_raises_transport_error(env_save_restore) -> None:
    # Arrange — no explicit base_url + env unset.
    env_save_restore.delete("SAC_LISTEN_BASE_URL")
    env_save_restore.delete("SCITEX_AGENT_CONTAINER_LISTEN_BASE_URL")
    # Act
    # Assert
    with pytest.raises(HostListenTransportError):
        host_listen_call("GET", "/v1/health", base_url=None, bearer="")


def test_missing_base_url_message_names_env_var(env_save_restore) -> None:
    # Arrange
    env_save_restore.delete("SAC_LISTEN_BASE_URL")
    env_save_restore.delete("SCITEX_AGENT_CONTAINER_LISTEN_BASE_URL")
    # Act
    raised: HostListenTransportError | None = None
    try:
        host_listen_call("GET", "/v1/health", base_url=None, bearer="")
    except HostListenTransportError as exc:
        raised = exc
    # Assert — operator must see WHICH env to fix.
    assert raised is not None and "SAC_LISTEN_BASE_URL" in str(raised)


# ---------------------------------------------------------------------------
# Bearer injection
# ---------------------------------------------------------------------------


def test_bearer_is_sent_in_authorization_header() -> None:
    # Arrange — opener captures the request to inspect the header.
    captured: dict[str, Any] = {}

    def _opener(req, timeout=None):  # noqa: ARG001
        captured["headers"] = dict(req.headers)
        return _FakeResponse(200, b'{"ok": true}')

    # Act
    host_listen_call(
        "GET",
        "/v1/health",
        base_url="http://x",
        bearer="my-token",
        opener=_opener,
    )
    # Assert — urllib lower-cases header keys when stored.
    assert captured["headers"].get("Authorization") == "Bearer my-token"


def test_no_bearer_header_when_bearer_empty() -> None:
    # Arrange
    captured: dict[str, Any] = {}

    def _opener(req, timeout=None):  # noqa: ARG001
        captured["headers"] = dict(req.headers)
        return _FakeResponse(200, b"{}")

    # Act
    host_listen_call(
        "GET", "/v1/health", base_url="http://x", bearer="", opener=_opener
    )
    # Assert
    assert "Authorization" not in captured["headers"]


# ---------------------------------------------------------------------------
# Body serialisation
# ---------------------------------------------------------------------------


def test_request_body_is_serialised_as_json() -> None:
    # Arrange — capture the request body to verify the wire shape.
    captured: dict[str, Any] = {}

    def _opener(req, timeout=None):  # noqa: ARG001
        captured["data"] = req.data
        return _FakeResponse(200, b"{}")

    # Act
    host_listen_call(
        "POST",
        "/agents",
        body={"name": "child"},
        base_url="http://x",
        bearer="",
        opener=_opener,
    )
    # Assert
    import json as _j

    assert _j.loads(captured["data"]) == {"name": "child"}


def test_content_type_set_when_body_present() -> None:
    # Arrange
    captured: dict[str, Any] = {}

    def _opener(req, timeout=None):  # noqa: ARG001
        captured["headers"] = dict(req.headers)
        return _FakeResponse(200, b"{}")

    # Act
    host_listen_call(
        "POST",
        "/agents",
        body={"x": 1},
        base_url="http://x",
        bearer="",
        opener=_opener,
    )
    # Assert
    assert captured["headers"].get("Content-type") == "application/json"
