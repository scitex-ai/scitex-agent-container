"""Tests for `sac a2a list`'s fetch+parse — fail-loud, never a raw traceback.

Regression: on a fresh / loaded runner host (scitex-dev handoff 2026-06-17,
Spartan bm159) `sac a2a list --json` exited 1 with a TRACEBACK — the inline
request only caught ``urllib.error.URLError``, so a socket timeout
(``TimeoutError``) or a non-JSON body (``json.JSONDecodeError``) crashed
unhandled, breaking callers that shell out to it (todo's `/fleet/mesh`).
Every failure mode must map to a clean ``A2aListError`` (→ one-line SystemExit).

Conventions: one assert per test (STX-TQ007 — a ``pytest.raises`` block IS
the one assertion); AAA markers (STX-TQ002); no mocks (STX-NM) — the opener
is dependency-injected (a plain callable seam).
"""

from __future__ import annotations

import json
import urllib.error

import pytest
from scitex_agent_container.cli_pkg._a2a_list_fetch import (
    A2aListError,
    fetch_agents,
    parse_agents_response,
)


class _FakeResp:
    """Minimal context-manager stand-in for an http response."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *_a: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


def _opener_returning(body: bytes):
    def _open(req, timeout=None):  # noqa: ANN001
        return _FakeResp(body)

    return _open


def _opener_raising(exc: BaseException):
    def _open(req, timeout=None):  # noqa: ANN001
        raise exc

    return _open


# --- parse_agents_response ---------------------------------------------------


def test_parse_valid_body_returns_agents_list():
    # Arrange
    body = json.dumps({"agents": [{"name": "a"}]}).encode()
    # Act
    agents = parse_agents_response(body)
    # Assert
    assert agents == [{"name": "a"}]


def test_parse_missing_agents_key_returns_empty_list():
    # Arrange
    body = json.dumps({}).encode()
    # Act
    agents = parse_agents_response(body)
    # Assert
    assert agents == []


def test_parse_non_json_body_raises_a2a_list_error():
    # Arrange
    body = b"<html>502 Bad Gateway</html>"
    # Act
    # Assert
    with pytest.raises(A2aListError):
        parse_agents_response(body)


def test_parse_non_object_json_raises_a2a_list_error():
    # Arrange — a bare array is not the expected {"agents": [...]} object.
    body = json.dumps([1, 2, 3]).encode()
    # Act
    # Assert
    with pytest.raises(A2aListError):
        parse_agents_response(body)


# --- fetch_agents (injected opener) ------------------------------------------


def test_fetch_returns_agents_on_valid_response():
    # Arrange
    body = json.dumps({"agents": [{"name": "lead"}]}).encode()
    # Act
    agents = fetch_agents("http://x:7878", "tok", opener=_opener_returning(body))
    # Assert
    assert agents == [{"name": "lead"}]


def test_fetch_maps_socket_timeout_to_a2a_list_error():
    # Arrange — the bug: TimeoutError was NOT caught by `except URLError`.
    opener = _opener_raising(TimeoutError("timed out"))
    # Act
    # Assert
    with pytest.raises(A2aListError):
        fetch_agents("http://x:7878", "tok", opener=opener)


def test_fetch_maps_urlerror_to_a2a_list_error():
    # Arrange — connection refused / DNS failure.
    opener = _opener_raising(urllib.error.URLError("conn refused"))
    # Act
    # Assert
    with pytest.raises(A2aListError):
        fetch_agents("http://x:7878", "tok", opener=opener)


def test_fetch_maps_oserror_to_a2a_list_error():
    # Arrange — generic socket / OS error mid-read.
    opener = _opener_raising(OSError("reset by peer"))
    # Act
    # Assert
    with pytest.raises(A2aListError):
        fetch_agents("http://x:7878", "tok", opener=opener)


def test_fetch_maps_bad_json_response_to_a2a_list_error():
    # Arrange — listen reachable but returns garbage.
    opener = _opener_returning(b"not json{")
    # Act
    # Assert
    with pytest.raises(A2aListError):
        fetch_agents("http://x:7878", "tok", opener=opener)
