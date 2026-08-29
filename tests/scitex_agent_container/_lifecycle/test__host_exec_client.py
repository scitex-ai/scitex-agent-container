"""Tests for the in-container ``request_host_exec`` client.

No mocks (STX-NM002): the client accepts an ``opener`` keyword — a
``urllib.request.urlopen``-shaped callable seam — so tests pass a hand-rolled
callable that returns a plain fake response object. Real Request bytes are
inspected via the ``req.data`` / ``req.full_url`` / ``req.headers`` attributes;
no monkeypatching of the transport.
"""

from __future__ import annotations

import json
import os
from contextlib import contextmanager
from typing import Any
from urllib import error as urlerror

import pytest


@contextmanager
def _env(name: str, value: str):
    """Set a REAL env var for the block, restoring the prior value on exit.

    No ``monkeypatch`` (STX-NM002): the production resolver reads
    ``os.environ`` directly, so the test sets the real variable and pops it.
    """
    prev = os.environ.get(name)
    os.environ[name] = value
    try:
        yield
    finally:
        if prev is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = prev


from scitex_agent_container._lifecycle._host_exec_client import (
    HostExecRequestError,
    request_host_exec,
)


class _FakeResponse:
    """Minimal urllib.response-shaped object: read() + context manager."""

    def __init__(self, body_bytes: bytes) -> None:
        self._body = body_bytes

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_a) -> None:  # noqa: ANN001
        return None


def _opener_returning(body: dict[str, Any]):
    """Return a urlopen-shaped callable that yields ``body`` as JSON. Also
    records the last received Request onto ``.last_request`` for assertion."""
    payload = json.dumps(body).encode("utf-8")

    def _opener(req, timeout=None):  # noqa: ANN001
        _opener.last_request = req
        return _FakeResponse(payload)

    _opener.last_request = None  # type: ignore[attr-defined]
    return _opener


def _opener_raising_http(code: int, body: dict[str, Any]):
    """Return an opener that raises ``urllib.error.HTTPError`` — the server-
    reached-but-refused shape ``request_host_exec`` must surface as
    ``HostExecRequestError`` with ``.status`` populated."""

    def _opener(req, timeout=None):  # noqa: ANN001
        raw = json.dumps(body).encode("utf-8")

        class _ErrBody:
            def read(self_inner) -> bytes:  # noqa: N805
                return raw

            def close(self_inner) -> None:  # noqa: N805
                return None

        exc = urlerror.HTTPError(req.full_url, code, "reason", hdrs=None, fp=_ErrBody())
        raise exc

    return _opener


def _opener_raising_url(msg: str = "connection refused"):
    """Return an opener that raises ``urllib.error.URLError`` — the transport
    failure shape."""

    def _opener(req, timeout=None):  # noqa: ANN001
        raise urlerror.URLError(msg)

    return _opener


# --------------------------------------------------------------------------
# Happy path — request shape + response parsing
# --------------------------------------------------------------------------


def test_request_host_exec_posts_to_v1_host_exec():
    # Arrange
    opener = _opener_returning({"exit_code": 0})
    # Act
    request_host_exec(
        ["true"],
        base_url="http://127.0.0.1:7878",
        bearer="tok",
        opener=opener,
    )
    # Assert
    assert opener.last_request.full_url == "http://127.0.0.1:7878/v1/host_exec"


def test_request_host_exec_sends_json_body_with_argv():
    # Arrange
    opener = _opener_returning({"exit_code": 0})
    # Act
    request_host_exec(
        ["echo", "hi"],
        base_url="http://127.0.0.1:7878",
        bearer="tok",
        opener=opener,
    )
    body = json.loads(opener.last_request.data.decode("utf-8"))
    # Assert
    assert body["argv"] == ["echo", "hi"]


def test_request_host_exec_forwards_optional_cwd():
    # Arrange
    opener = _opener_returning({"exit_code": 0})
    # Act
    request_host_exec(
        ["true"],
        cwd="/tmp/work",
        base_url="http://127.0.0.1:7878",
        bearer="tok",
        opener=opener,
    )
    body = json.loads(opener.last_request.data.decode("utf-8"))
    # Assert
    assert body["cwd"] == "/tmp/work"


def test_request_host_exec_forwards_optional_timeout():
    # Arrange
    opener = _opener_returning({"exit_code": 0})
    # Act
    request_host_exec(
        ["true"],
        timeout_s=42.0,
        base_url="http://127.0.0.1:7878",
        bearer="tok",
        opener=opener,
    )
    body = json.loads(opener.last_request.data.decode("utf-8"))
    # Assert
    assert body["timeout_s"] == 42.0


def test_request_host_exec_forwards_optional_env():
    # Arrange
    opener = _opener_returning({"exit_code": 0})
    # Act
    request_host_exec(
        ["true"],
        env={"FOO": "bar"},
        base_url="http://127.0.0.1:7878",
        bearer="tok",
        opener=opener,
    )
    body = json.loads(opener.last_request.data.decode("utf-8"))
    # Assert
    assert body["env"] == {"FOO": "bar"}


def test_request_host_exec_forwards_optional_caller():
    # Arrange
    opener = _opener_returning({"exit_code": 0})
    # Act
    request_host_exec(
        ["true"],
        caller="scitex-agent-container",
        base_url="http://127.0.0.1:7878",
        bearer="tok",
        opener=opener,
    )
    body = json.loads(opener.last_request.data.decode("utf-8"))
    # Assert
    assert body["caller"] == "scitex-agent-container"


def test_request_host_exec_sets_bearer_header_when_token_given():
    # Arrange
    opener = _opener_returning({"exit_code": 0})
    # Act
    request_host_exec(
        ["true"],
        base_url="http://127.0.0.1:7878",
        bearer="my-token",
        opener=opener,
    )
    # Assert — urllib normalises header names via .capitalize().
    assert opener.last_request.headers.get("Authorization") == "Bearer my-token"


def test_request_host_exec_returns_parsed_server_body():
    # Arrange — the endpoint's real success shape.
    expected = {
        "exit_code": 0,
        "stdout": "ok",
        "stderr": "",
        "duration_s": 0.01,
        "timed_out": False,
    }
    opener = _opener_returning(expected)
    # Act
    result = request_host_exec(
        ["true"],
        base_url="http://127.0.0.1:7878",
        bearer="tok",
        opener=opener,
    )
    # Assert
    assert result == expected


# --------------------------------------------------------------------------
# Error surfaces — server-reached-but-refused (HTTPError) + transport (URLError)
# --------------------------------------------------------------------------


def test_request_host_exec_raises_on_403_from_server():
    # Arrange — group gate refused (real deny shape from the endpoint).
    opener = _opener_raising_http(403, {"error": "group not eligible"})

    # Act
    def _call() -> None:
        request_host_exec(
            ["true"], base_url="http://127.0.0.1:7878", bearer="tok", opener=opener
        )

    # Assert
    with pytest.raises(HostExecRequestError):
        _call()


def test_request_host_exec_exception_populates_status_on_http_error():
    # Arrange
    opener = _opener_raising_http(401, {"error": "bad bearer"})
    # Act
    exc = None
    try:
        request_host_exec(
            ["true"], base_url="http://127.0.0.1:7878", bearer="", opener=opener
        )
    except HostExecRequestError as e:
        exc = e
    # Assert
    assert exc is not None and exc.status == 401


def test_request_host_exec_raises_on_transport_error():
    # Arrange — listen daemon down / connection refused.
    opener = _opener_raising_url("connection refused")

    # Act
    def _call() -> None:
        request_host_exec(
            ["true"], base_url="http://127.0.0.1:7878", bearer="tok", opener=opener
        )

    # Assert
    with pytest.raises(HostExecRequestError):
        _call()


# --------------------------------------------------------------------------
# Load-resilience: the client-side HTTP wait is BOUNDED to the server contract
# (incident 2026-07-09 — a fixed ~62 min wait let a jammed listen block the MCP
# handler long enough for the stdio client to drop the whole server).
# --------------------------------------------------------------------------


def _opener_recording_timeout(body: dict[str, Any] | None = None):
    """Opener that records the ``timeout`` it was called with onto
    ``.last_timeout`` (and yields ``body`` as JSON)."""
    payload = json.dumps(body or {"exit_code": 0}).encode("utf-8")

    def _opener(req, timeout=None):  # noqa: ANN001
        _opener.last_timeout = timeout
        return _FakeResponse(payload)

    _opener.last_timeout = None  # type: ignore[attr-defined]
    return _opener


def test_default_http_timeout_tracks_server_default_not_62_minutes():
    # Arrange — no timeout_s → server default 300s → client waits 330s (NOT 3700).
    opener = _opener_recording_timeout()
    # Act
    request_host_exec(
        ["true"], base_url="http://127.0.0.1:7878", bearer="tok", opener=opener
    )
    # Assert
    assert opener.last_timeout == pytest.approx(330.0)


def test_http_timeout_tracks_requested_timeout_s():
    # Arrange — an explicit long build timeout is honoured (bounded to it + margin).
    opener = _opener_recording_timeout()
    # Act
    request_host_exec(
        ["true"],
        timeout_s=1800.0,
        base_url="http://127.0.0.1:7878",
        bearer="tok",
        opener=opener,
    )
    # Assert
    assert opener.last_timeout == pytest.approx(1830.0)


def test_http_timeout_capped_at_server_max():
    # Arrange — an over-max timeout_s is clamped to the server ceiling (3600) + margin.
    opener = _opener_recording_timeout()
    # Act
    request_host_exec(
        ["true"],
        timeout_s=99_999.0,
        base_url="http://127.0.0.1:7878",
        bearer="tok",
        opener=opener,
    )
    # Assert
    assert opener.last_timeout == pytest.approx(3630.0)


def test_explicit_http_timeout_wins_verbatim():
    # Arrange — an explicit per-call override is used as-is.
    opener = _opener_recording_timeout()
    # Act
    request_host_exec(
        ["true"],
        base_url="http://127.0.0.1:7878",
        bearer="tok",
        http_timeout_s=7.5,
        opener=opener,
    )
    # Assert
    assert opener.last_timeout == pytest.approx(7.5)


def test_env_override_sets_http_timeout():
    # Arrange — deployment-wide hard override via a REAL env var.
    opener = _opener_recording_timeout()
    # Act
    with _env("SAC_MCP_HOST_EXEC_HTTP_TIMEOUT_S", "12"):
        request_host_exec(
            ["true"], base_url="http://127.0.0.1:7878", bearer="tok", opener=opener
        )
    # Assert
    assert opener.last_timeout == pytest.approx(12.0)


def test_invalid_env_override_falls_back_to_derived():
    # Arrange — a garbage env value is ignored; the derived default stands.
    opener = _opener_recording_timeout()
    # Act
    with _env("SAC_MCP_HOST_EXEC_HTTP_TIMEOUT_S", "not-a-number"):
        request_host_exec(
            ["true"], base_url="http://127.0.0.1:7878", bearer="tok", opener=opener
        )
    # Assert
    assert opener.last_timeout == pytest.approx(330.0)


# ---------------------------------------------------------------------------
# Caller identity is AUTO-RESOLVED from SAC_NAME
#
# Omitting `caller` used to send a body with no identity at all. On the
# host-wide bearer path the server has no per-node identity either, so it
# refused with 403 "host_exec requires a resolvable caller" — even though
# SAC_NAME was set in that container. Reported by scitex-storage 2026-07-28,
# hit again 2026-08-09. The tool's docstring had promised this defaulting all
# along while no layer implemented it.
# ---------------------------------------------------------------------------


def test_caller_is_auto_resolved_from_sac_name_when_omitted():
    """The regression: no `caller` argument must still send an identity."""
    # Arrange
    opener = _opener_returning({"exit_code": 0})
    # Act
    with _env("SAC_NAME", "agent-under-test"):
        request_host_exec(
            ["true"], base_url="http://127.0.0.1:7878", bearer="tok", opener=opener
        )
    body = json.loads(opener.last_request.data.decode("utf-8"))
    # Assert
    assert body["caller"] == "agent-under-test"


def test_an_explicit_caller_is_not_overridden_by_sac_name():
    """Auto-resolution fills a gap; it must never override a caller's choice."""
    # Arrange
    opener = _opener_returning({"exit_code": 0})
    # Act
    with _env("SAC_NAME", "agent-under-test"):
        request_host_exec(
            ["true"],
            caller="explicit-caller",
            base_url="http://127.0.0.1:7878",
            bearer="tok",
            opener=opener,
        )
    body = json.loads(opener.last_request.data.decode("utf-8"))
    # Assert
    assert body["caller"] == "explicit-caller"


def test_an_empty_caller_opts_out_and_sends_no_identity():
    """`caller=""` is the deliberate admin/host-wide path — keep it working."""
    # Arrange
    opener = _opener_returning({"exit_code": 0})
    # Act
    with _env("SAC_NAME", "agent-under-test"):
        request_host_exec(
            ["true"],
            caller="",
            base_url="http://127.0.0.1:7878",
            bearer="tok",
            opener=opener,
        )
    body = json.loads(opener.last_request.data.decode("utf-8"))
    # Assert
    assert "caller" not in body
