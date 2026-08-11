"""Tests for :mod:`scitex_agent_container._lifecycle._in_sif_http_client`.

PR-3 Checkpoint 3 transport layer. Exercises the env / fail-loud /
non-2xx / transport-error branches without real network round-trips
by injecting a fake ``opener`` (no monkeypatching — PA-306). AAA +
one assert per test (PA-307).
"""

from __future__ import annotations

import io
import os
from typing import Any
from urllib import error as urlerror

import pytest

from scitex_agent_container._lifecycle._in_sif_http_client import (
    HostListenTransportError,
    _resolve_bearer,
    host_listen_call,
)
from scitex_agent_container._listen._handler_deadline import (
    AGENT_START_DEADLINE_S,
    client_timeout_for,
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
# Socket timeout — must OUTLIVE the server's declared deadline (2026-08-11)
#
# This client is generic, but ONE of its callers points it at ``POST /agents``
# (``sac agents spawn-from-here``), the one route in this system that DECLARES
# an answer-by deadline. The default here was a flat ``30.0`` — EXACTLY
# ``AGENT_START_DEADLINE_S`` — so on that route the client gave up at the precise
# moment the handler was still entitled to be working, and destroyed the ``202``
# "accepted, still in flight" that exists to stop a slow spawn being reported as
# a dead host. Measured that day on the sibling broker path: a spawn reported as
# "no response" had been accepted and ran for 5m12s.
#
# A shared default has to satisfy the STRICTEST route it is used on. Asserted as
# an ORDERING against the real derivation, never as a literal number.
# ---------------------------------------------------------------------------


def test_socket_timeout_outlives_the_server_deadline() -> None:
    # Arrange
    captured: dict[str, Any] = {}

    def _opener(_req, timeout=None):  # noqa: ARG001
        captured["timeout"] = timeout
        return _FakeResponse(200, b"{}")

    # Act
    host_listen_call(
        "POST",
        "/agents",
        body={"name": "c"},
        base_url="http://x",
        bearer="",
        opener=_opener,
    )
    # Assert — STRICTLY greater; the old 30.0 sat exactly on the deadline.
    assert captured["timeout"] > AGENT_START_DEADLINE_S


def test_socket_timeout_equals_the_one_derivation() -> None:
    # Arrange
    captured: dict[str, Any] = {}

    def _opener(_req, timeout=None):  # noqa: ARG001
        captured["timeout"] = timeout
        return _FakeResponse(200, b"{}")

    # Act
    host_listen_call(
        "GET", "/v1/health", base_url="http://x", bearer="", opener=_opener
    )
    # Assert
    assert captured["timeout"] == pytest.approx(client_timeout_for())


def test_explicit_timeout_argument_is_honoured() -> None:
    # Arrange — the derivation is the DEFAULT, not a lock.
    captured: dict[str, Any] = {}

    def _opener(_req, timeout=None):  # noqa: ARG001
        captured["timeout"] = timeout
        return _FakeResponse(200, b"{}")

    # Act
    host_listen_call(
        "GET",
        "/v1/health",
        base_url="http://x",
        bearer="",
        timeout_s=2.5,
        opener=_opener,
    )
    # Assert
    assert captured["timeout"] == pytest.approx(2.5)


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


# ---------------------------------------------------------------------------
# The bearer must be findable ON DISK, not just in the env
#
# `_resolve_bearer` here stopped at SAC_LISTEN_BEARER while the spawn / restart
# / card-event clients also read the host token file at
# ~/.scitex/agent-container/tokens/listen-<host>.token. The runtime injects that
# env var ONLY for agents whose spec registers the `server:sac` channel, so for
# every other agent this route sent an UNAUTHENTICATED request and the listen
# answered 401 — same container, same readable token, different copy of the
# resolver.
# ---------------------------------------------------------------------------

_BEARER_KEYS = (
    "SAC_LISTEN_BEARER",
    "SCITEX_AGENT_CONTAINER_LISTEN_BEARER",
)


@pytest.fixture
def isolated_bearer_env(tmp_path):
    """Clear BOTH env spellings and redirect HOME to a clean tmp dir.

    Clearing both matters: a stray value in the operator's shell would make the
    must-fall-back-to-file test pass for the wrong reason. Redirecting HOME
    keeps the token-file read isolated from the real host token.
    """
    saved = {k: os.environ.get(k) for k in _BEARER_KEYS}
    saved_home = os.environ.get("HOME")
    for k in _BEARER_KEYS:
        os.environ.pop(k, None)
    os.environ["HOME"] = str(tmp_path)
    try:
        yield tmp_path
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if saved_home is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved_home


def _write_host_token_file(home, token: str) -> None:
    from scitex_agent_container._listen.tokens import default_token_path

    path = default_token_path(home=home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")


def test_bearer_falls_back_to_the_host_token_file(isolated_bearer_env) -> None:
    """The regression: an on-disk token was invisible to this client."""
    # Arrange — no bearer env; a real token file on disk.
    _write_host_token_file(isolated_bearer_env, "file-tok-in-sif")
    # Act
    resolved = _resolve_bearer(None)
    # Assert
    assert resolved == "file-tok-in-sif"


def test_env_bearer_still_wins_over_the_token_file(isolated_bearer_env) -> None:
    # Arrange — both sources present; the env must win.
    _write_host_token_file(isolated_bearer_env, "file-tok")
    os.environ["SAC_LISTEN_BEARER"] = "env-tok"
    # Act
    resolved = _resolve_bearer(None)
    # Assert
    assert resolved == "env-tok"


def test_an_empty_explicit_bearer_stays_unauthenticated(isolated_bearer_env) -> None:
    """``""`` is the deliberate opt-out — it must NOT reach for the file."""
    # Arrange
    _write_host_token_file(isolated_bearer_env, "file-tok")
    # Act
    resolved = _resolve_bearer("")
    # Assert
    assert resolved is None


def test_no_env_and_no_file_resolves_to_none(isolated_bearer_env) -> None:
    """An absent bearer stays non-fatal — a dev listen may run without auth."""
    # Arrange — cleared env, no token file written.
    # Act
    resolved = _resolve_bearer(None)
    # Assert
    assert resolved is None
