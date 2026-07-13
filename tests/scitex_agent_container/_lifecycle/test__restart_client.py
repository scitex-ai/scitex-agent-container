"""Tests for the in-container restart-proxy HTTP client.

The restart sibling of :mod:`test__spawn_client`. Same no-mocks
pattern: the production module exposes an injectable ``opener`` callable
that defaults to ``urllib.request.urlopen``; tests pass a hand-rolled
opener returning a ``urllib.response``-shaped object. No
``monkeypatch.setattr`` on production internals.

Each test: AAA markers (TQ002), one assertion (TQ007), 3+-word name.
"""

from __future__ import annotations

import io
import json
import os
from typing import Iterator
from urllib import error as urlerror

import pytest

from scitex_agent_container._lifecycle._restart_client import (
    RestartRequestError,
    request_restart,
)


class _FakeResp:
    """A real callable response object matching the urllib contract."""

    def __init__(self, body: bytes, status: int = 200) -> None:
        self._body = body
        self.status = status

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *args: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


def _opener_returning(body: bytes, status: int = 200):
    """Build (opener, captured) — the opener records each call."""
    captured: dict = {}

    def opener(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["body"] = req.data
        captured["headers"] = {k.lower(): v for k, v in dict(req.headers).items()}
        captured["timeout"] = timeout
        return _FakeResp(body, status)

    return opener, captured


def _opener_raising(exc: Exception):
    def opener(req, timeout=None):
        raise exc

    return opener


_LISTEN_KEYS = (
    "SAC_LISTEN_BASE_URL",
    "SCITEX_AGENT_CONTAINER_LISTEN_BASE_URL",
    "SAC_LISTEN_BEARER",
    "SCITEX_AGENT_CONTAINER_LISTEN_BEARER",
    "SAC_NAME",
    "SCITEX_AGENT_CONTAINER_NAME",
)


@pytest.fixture
def listen_env(tmp_path) -> Iterator[callable]:
    """Yield a setter; clears + restores both prefixes for every key.

    HOME is redirected to a clean ``tmp_path`` so the bearer file
    fallback reads from an isolated, empty tokens dir.
    """
    saved = {k: os.environ.get(k) for k in _LISTEN_KEYS}
    saved_home = os.environ.get("HOME")
    for k in _LISTEN_KEYS:
        os.environ.pop(k, None)
    os.environ["HOME"] = str(tmp_path)

    def _set(suffix: str, value: str | None) -> None:
        long_form = f"SCITEX_AGENT_CONTAINER_{suffix}"
        short_form = f"SAC_{suffix}"
        os.environ.pop(long_form, None)
        if value is None:
            os.environ.pop(short_form, None)
        else:
            os.environ[short_form] = value

    try:
        yield _set
    finally:
        for k in _LISTEN_KEYS:
            os.environ.pop(k, None)
        for k, prev in saved.items():
            if prev is not None:
                os.environ[k] = prev
        if saved_home is not None:
            os.environ["HOME"] = saved_home
        else:
            os.environ.pop("HOME", None)


# ---------------------------------------------------------------------------
# Missing base URL — fail loud
# ---------------------------------------------------------------------------


def test_missing_base_url_raises_restart_request_error(listen_env) -> None:
    # Arrange — listen_env yields with both base-url envs cleared.
    captured_message = ""
    # Act
    try:
        request_restart("peer", opener=lambda req, timeout=None: None)
    except RestartRequestError as exc:
        captured_message = str(exc)
    # Assert
    assert "SAC_LISTEN_BASE_URL" in captured_message


def test_empty_name_raises_restart_request_error(listen_env) -> None:
    # Arrange
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    raised = False
    # Act
    try:
        request_restart("", opener=lambda req, timeout=None: None)
    except RestartRequestError:
        raised = True
    # Assert
    assert raised is True


# ---------------------------------------------------------------------------
# Happy path — POST shape
# ---------------------------------------------------------------------------


def test_post_targets_restart_route_on_base_url(listen_env) -> None:
    # Arrange — trailing slash on the base URL must be stripped.
    listen_env("LISTEN_BASE_URL", "http://host:9100/")
    opener, captured = _opener_returning(b'{"name":"peer","returncode":0}')
    # Act
    request_restart("peer", opener=opener)
    # Assert
    assert captured["url"] == "http://host:9100/agents/peer/restart"


def test_post_uses_http_post_method(listen_env) -> None:
    # Arrange
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    opener, captured = _opener_returning(b'{"name":"peer","returncode":0}')
    # Act
    request_restart("peer", opener=opener)
    # Assert
    assert captured["method"] == "POST"


def test_post_body_defaults_caller_from_sac_name_env(listen_env) -> None:
    # Arrange — SAC_NAME present → resolved as caller automatically.
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    listen_env("NAME", "neurovista")
    opener, captured = _opener_returning(b'{"name":"peer","returncode":0}')
    # Act
    request_restart("peer", opener=opener)
    # Assert
    assert json.loads(captured["body"])["caller"] == "neurovista"


def test_post_body_omits_caller_for_admin_path(listen_env) -> None:
    # Arrange — no SAC_NAME → admin path; the field is omitted entirely.
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    opener, captured = _opener_returning(b'{"name":"peer","returncode":0}')
    # Act
    request_restart("peer", opener=opener)
    # Assert
    assert "caller" not in json.loads(captured["body"])


def test_post_body_includes_fresh_when_requested(listen_env) -> None:
    # Arrange
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    opener, captured = _opener_returning(b'{"name":"peer","returncode":0}')
    # Act
    request_restart("peer", fresh=True, opener=opener)
    # Assert — the host learns to start a fresh session.
    assert json.loads(captured["body"])["fresh"] is True


def test_post_body_omits_fresh_by_default(listen_env) -> None:
    # Arrange
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    opener, captured = _opener_returning(b'{"name":"peer","returncode":0}')
    # Act
    request_restart("peer", opener=opener)
    # Assert — default body stays byte-identical to a plain restart.
    assert "fresh" not in json.loads(captured["body"])


def test_explicit_caller_arg_overrides_sac_name_env(listen_env) -> None:
    # Arrange
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    listen_env("NAME", "env-caller")
    opener, captured = _opener_returning(b'{"name":"peer","returncode":0}')
    # Act
    request_restart("peer", caller="arg-caller", opener=opener)
    # Assert
    assert json.loads(captured["body"])["caller"] == "arg-caller"


def test_bearer_token_attached_as_authorization_header(listen_env) -> None:
    # Arrange
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    listen_env("LISTEN_BEARER", "tok-abc")
    opener, captured = _opener_returning(b'{"name":"peer","returncode":0}')
    # Act
    request_restart("peer", opener=opener)
    # Assert
    assert captured["headers"].get("authorization") == "Bearer tok-abc"


def test_no_authorization_header_when_bearer_unset(listen_env) -> None:
    # Arrange — no bearer in env, none passed, no on-disk token file.
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    opener, captured = _opener_returning(b'{"name":"peer","returncode":0}')
    # Act
    request_restart("peer", opener=opener)
    # Assert
    assert "authorization" not in captured["headers"]


def test_success_returns_parsed_response_dict(listen_env) -> None:
    # Arrange
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    body = json.dumps(
        {"name": "peer", "returncode": 0, "stdout": "ok", "stderr": ""}
    ).encode()
    opener, _ = _opener_returning(body)
    # Act
    out = request_restart("peer", opener=opener)
    # Assert
    assert out["returncode"] == 0


# ---------------------------------------------------------------------------
# Failure modes — fail loud, never swallowed
# ---------------------------------------------------------------------------


def test_acl_403_deny_raises_with_status_403(listen_env) -> None:
    # Arrange — server returns the ACL deny payload from deny_response().
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    deny_body = json.dumps(
        {"error": "ACL deny", "reason": "lineage ACL deny: caller has no edge"}
    ).encode()
    opener = _opener_raising(
        urlerror.HTTPError(
            "http://host:9100/agents/peer/restart",
            403,
            "Forbidden",
            {},
            io.BytesIO(deny_body),
        )
    )
    captured_status = None
    # Act
    try:
        request_restart("peer", opener=opener)
    except RestartRequestError as exc:
        captured_status = exc.status
    # Assert
    assert captured_status == 403


def test_acl_403_deny_preserves_server_reason_in_body(listen_env) -> None:
    # Arrange
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    deny_body = json.dumps(
        {"error": "ACL deny", "reason": "lineage ACL deny"}
    ).encode()
    opener = _opener_raising(
        urlerror.HTTPError(
            "http://host:9100/agents/peer/restart",
            403,
            "Forbidden",
            {},
            io.BytesIO(deny_body),
        )
    )
    captured_body = None
    # Act
    try:
        request_restart("peer", opener=opener)
    except RestartRequestError as exc:
        captured_body = exc.body
    # Assert
    assert captured_body == {"error": "ACL deny", "reason": "lineage ACL deny"}


def test_server_500_raises_restart_request_error(listen_env) -> None:
    # Arrange
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    opener = _opener_raising(
        urlerror.HTTPError(
            "http://host:9100/agents/peer/restart", 500, "boom", {}, io.BytesIO(b"")
        )
    )
    captured_status = None
    # Act
    try:
        request_restart("peer", opener=opener)
    except RestartRequestError as exc:
        captured_status = exc.status
    # Assert
    assert captured_status == 500


def test_transport_error_keeps_status_none(listen_env) -> None:
    # Arrange — listen unreachable (connection refused etc.).
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    opener = _opener_raising(urlerror.URLError("connection refused"))
    captured_status: object = "UNSET"
    # Act
    try:
        request_restart("peer", opener=opener)
    except RestartRequestError as exc:
        captured_status = exc.status
    # Assert — no HTTP exchange happened, so status stays None.
    assert captured_status is None


def test_transport_error_message_carries_broker_hint(listen_env) -> None:
    # Arrange — listen unreachable (connection refused etc.).
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    opener = _opener_raising(urlerror.URLError("connection refused"))
    message = ""
    # Act
    try:
        request_restart("peer", opener=opener)
    except RestartRequestError as exc:
        message = str(exc)
    # Assert — the enriched message keeps the original 'cannot reach listen'
    # detail AND appends the cause+fix hint naming `sac listen restart`, so a
    # broker-down restart is a dead-end no longer.
    assert "cannot reach listen" in message and "sac listen restart" in message


def test_non_dict_2xx_body_raises_restart_request_error(listen_env) -> None:
    # Arrange — server returns a JSON array instead of an object.
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    opener, _ = _opener_returning(b"[1,2,3]", status=200)
    raised = False
    # Act
    try:
        request_restart("peer", opener=opener)
    except RestartRequestError:
        raised = True
    # Assert
    assert raised is True
