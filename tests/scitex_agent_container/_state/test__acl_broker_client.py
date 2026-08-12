"""Tests for the in-container ACL broker HTTP client (task #27 PR B).

Mirrors :mod:`tests/.../_lifecycle/test__spawn_client` shape — no
mocks, real ``urllib.request.Request`` shape inspection via an
injectable ``opener`` callable, real env save/restore (no
monkeypatch on production internals).

The broker translates an ACL decision into a single POST against
the host listen's ``/v1/acl/{decision}`` route, carrying the
``SAC_LISTEN_BEARER`` token when present.
"""

from __future__ import annotations

import io
import json
import os
from typing import Iterator
from urllib import error as urlerror

import pytest

from scitex_agent_container._state._acl_broker_client import (
    AclBrokerError,
    _resolve_bearer,
    broker_acl_decision,
)

# ---------------------------------------------------------------------------
# Fake response + opener factories (real objects, urllib protocol)
# ---------------------------------------------------------------------------


class _FakeResp:
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
    captured: dict = {}

    def opener(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["body"] = req.data
        captured["headers"] = {k.lower(): v for k, v in dict(req.headers).items()}
        captured["timeout"] = timeout
        return _FakeResp(body, status)

    return opener, captured


# ---------------------------------------------------------------------------
# Env fixtures — toggle SAC_LISTEN_BASE_URL + SAC_LISTEN_BEARER
# ---------------------------------------------------------------------------


_LISTEN_KEYS = (
    "SAC_LISTEN_BASE_URL",
    "SCITEX_AGENT_CONTAINER_LISTEN_BASE_URL",
    "SAC_LISTEN_BEARER",
    "SCITEX_AGENT_CONTAINER_LISTEN_BEARER",
)


@pytest.fixture
def listen_env() -> Iterator[callable]:
    saved = {k: os.environ.get(k) for k in _LISTEN_KEYS}
    for k in _LISTEN_KEYS:
        os.environ.pop(k, None)

    def _set(suffix: str, value: str | None) -> None:
        short = f"SAC_{suffix}"
        long_ = f"SCITEX_AGENT_CONTAINER_{suffix}"
        os.environ.pop(long_, None)
        if value is None:
            os.environ.pop(short, None)
        else:
            os.environ[short] = value

    try:
        yield _set
    finally:
        for k in _LISTEN_KEYS:
            os.environ.pop(k, None)
        for k, prev in saved.items():
            if prev is not None:
                os.environ[k] = prev


# ---------------------------------------------------------------------------
# Missing base URL — fail loud
# ---------------------------------------------------------------------------


def test_missing_base_url_raises_acl_broker_error(listen_env) -> None:
    # Arrange — env is clean (fixture pre-clears).
    msg = ""
    # Act
    try:
        broker_acl_decision(
            "unblock",
            sender="a",
            target="b",
            opener=lambda req, timeout=None: _FakeResp(b"", 200),
        )
    except AclBrokerError as exc:
        msg = str(exc)
    # Assert — the env var name must appear in the message so the
    # operator knows which container var the apptainer runtime
    # missed.
    assert "SAC_LISTEN_BASE_URL" in msg


# ---------------------------------------------------------------------------
# Happy path — POST shape
# ---------------------------------------------------------------------------


def test_post_targets_unblock_route(listen_env) -> None:
    # Arrange
    listen_env("LISTEN_BASE_URL", "http://host:9100/")  # trailing slash
    opener, captured = _opener_returning(b'{"granted":true}')
    # Act
    broker_acl_decision("unblock", sender="a", target="b", opener=opener)
    # Assert
    assert captured["url"] == "http://host:9100/v1/acl/unblock"


def test_post_targets_block_route(listen_env) -> None:
    # Arrange
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    opener, captured = _opener_returning(b'{"blocked":true}')
    # Act
    broker_acl_decision("block", sender="a", target="b", opener=opener)
    # Assert
    assert captured["url"] == "http://host:9100/v1/acl/block"


def test_post_targets_grant_route(listen_env) -> None:
    # Arrange — legacy alias of unblock.
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    opener, captured = _opener_returning(b'{"granted":true}')
    # Act
    broker_acl_decision("grant", sender="a", target="b", opener=opener)
    # Assert
    assert captured["url"] == "http://host:9100/v1/acl/grant"


def test_post_uses_http_post_method(listen_env) -> None:
    # Arrange
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    opener, captured = _opener_returning(b"{}")
    # Act
    broker_acl_decision("unblock", sender="a", target="b", opener=opener)
    # Assert
    assert captured["method"] == "POST"


def test_post_body_includes_sender_and_target(listen_env) -> None:
    # Arrange
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    opener, captured = _opener_returning(b"{}")
    # Act
    broker_acl_decision("block", sender="alice", target="lead", opener=opener)
    # Assert
    body = json.loads(captured["body"])
    assert body["sender"] == "alice" and body["target"] == "lead"


def test_post_body_omits_note_when_absent(listen_env) -> None:
    # Arrange — note is optional; absence is normal.
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    opener, captured = _opener_returning(b"{}")
    # Act
    broker_acl_decision("unblock", sender="a", target="b", opener=opener)
    # Assert
    assert "note" not in json.loads(captured["body"])


def test_post_body_includes_note_when_set(listen_env) -> None:
    # Arrange
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    opener, captured = _opener_returning(b"{}")
    # Act
    broker_acl_decision(
        "unblock",
        sender="a",
        target="b",
        note="prompt msg_id abc123",
        opener=opener,
    )
    # Assert
    assert json.loads(captured["body"])["note"] == "prompt msg_id abc123"


def test_bearer_attached_as_authorization_header(listen_env) -> None:
    # Arrange
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    listen_env("LISTEN_BEARER", "tok-abc")
    opener, captured = _opener_returning(b"{}")
    # Act
    broker_acl_decision("unblock", sender="a", target="b", opener=opener)
    # Assert
    assert captured["headers"].get("authorization") == "Bearer tok-abc"


def test_no_authorization_header_when_bearer_unset(listen_env, home_redirect) -> None:
    # Arrange — no bearer in the env AND (via home_redirect) none on disk.
    # `home_redirect` is load-bearing here, not cosmetic: this test asserted
    # "no bearer available" while only clearing the ENV, which held solely
    # because the old resolver could not read the token file at all. Once the
    # client gained the fallback, the unredirected HOME let it find the
    # OPERATOR'S REAL host token — the assertion failed and printed that token
    # into the test output. The fixture now makes "no token available" true
    # instead of accidental.
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    opener, captured = _opener_returning(b"{}")
    # Act
    broker_acl_decision("unblock", sender="a", target="b", opener=opener)
    # Assert
    assert "authorization" not in captured["headers"]


def test_returns_parsed_server_body_on_success(listen_env) -> None:
    # Arrange
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    body = {
        "sender": "alice",
        "target": "lead",
        "granted": True,
        "unblocked": False,
        "cleared_pending": True,
    }
    opener, _ = _opener_returning(json.dumps(body).encode())
    # Act
    result = broker_acl_decision(
        "unblock", sender="alice", target="lead", opener=opener
    )
    # Assert
    assert result["cleared_pending"] is True


# ---------------------------------------------------------------------------
# Failure modes — fail loud, surface status + body verbatim
# ---------------------------------------------------------------------------


def test_http_4xx_raises_with_status_in_error(listen_env) -> None:
    # Arrange
    listen_env("LISTEN_BASE_URL", "http://host:9100")

    def opener(req, timeout=None):
        raise urlerror.HTTPError(
            req.full_url, 400, "Bad Request", {}, io.BytesIO(b'{"error":"x"}')
        )

    status = None
    # Act
    try:
        broker_acl_decision("unblock", sender="a", target="b", opener=opener)
    except AclBrokerError as exc:
        status = exc.status
    # Assert
    assert status == 400


def test_transport_error_raises_with_clear_reason(listen_env) -> None:
    # Arrange — host listen unreachable.
    listen_env("LISTEN_BASE_URL", "http://host:9100")

    def opener(req, timeout=None):
        raise urlerror.URLError("Connection refused")

    msg = ""
    # Act
    try:
        broker_acl_decision("unblock", sender="a", target="b", opener=opener)
    except AclBrokerError as exc:
        msg = str(exc)
    # Assert
    assert "cannot reach listen" in msg


def test_unknown_decision_raises_loudly() -> None:
    # Arrange
    # Act
    # Assert
    with pytest.raises(AclBrokerError, match="unknown ACL decision"):
        broker_acl_decision("nope", sender="a", target="b")


def test_empty_sender_raises_loudly(listen_env) -> None:
    # Arrange
    listen_env("LISTEN_BASE_URL", "http://host:9100")
    # Act
    # Assert
    with pytest.raises(AclBrokerError, match="non-empty"):
        broker_acl_decision("unblock", sender="", target="b")


# ---------------------------------------------------------------------------
# The bearer must be findable ON DISK, not just in the env
#
# This module's `_resolve_bearer` stopped at SAC_LISTEN_BEARER while the spawn /
# restart / card-event clients also read the host token file at
# ~/.scitex/agent-container/tokens/listen-<host>.token. The runtime injects that
# env var ONLY for agents whose spec registers the `server:sac` channel, so for
# every other agent this route brokered ACL decisions UNAUTHENTICATED and the
# listen answered 401 — same container, same readable token, different copy of
# the resolver.
# ---------------------------------------------------------------------------


@pytest.fixture
def home_redirect(tmp_path):
    """Point HOME at a clean tmp dir so the token-file fallback is isolated.

    Without this the resolver reads the operator's REAL
    ``~/.scitex/agent-container/tokens/...`` and the test result depends on the
    machine it runs on.
    """
    saved = os.environ.get("HOME")
    os.environ["HOME"] = str(tmp_path)
    try:
        yield tmp_path
    finally:
        if saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved


def _write_host_token_file(home, token: str) -> None:
    from scitex_agent_container._listen.tokens import default_token_path

    path = default_token_path(home=home)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(token, encoding="utf-8")


def test_bearer_falls_back_to_the_host_token_file(listen_env, home_redirect) -> None:
    """The regression: an on-disk token was invisible to this client."""
    # Arrange — env cleared by listen_env; a real token file on disk.
    _write_host_token_file(home_redirect, "file-tok-acl")
    # Act
    resolved = _resolve_bearer(None)
    # Assert
    assert resolved == "file-tok-acl"


def test_env_bearer_still_wins_over_the_token_file(listen_env, home_redirect) -> None:
    # Arrange — both sources present; the env must win.
    _write_host_token_file(home_redirect, "file-tok")
    listen_env("LISTEN_BEARER", "env-tok")
    # Act
    resolved = _resolve_bearer(None)
    # Assert
    assert resolved == "env-tok"


def test_an_empty_explicit_bearer_stays_unauthenticated(
    listen_env, home_redirect
) -> None:
    """``""`` is the deliberate opt-out — it must NOT reach for the file."""
    # Arrange
    _write_host_token_file(home_redirect, "file-tok")
    # Act
    resolved = _resolve_bearer("")
    # Assert
    assert resolved is None
