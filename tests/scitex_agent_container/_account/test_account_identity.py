"""Tests for the best-effort OAuth whoami (``_account.account_identity``).

No mocks of our own code (PA-306): the ONLY seam is the injected ``opener``
(an ``urlopen``-alike), which stands in for the network boundary — real
tests must never hit ``api.anthropic.com``. AAA markers (TQ002), descriptive
names (TQ003), one assertion each (TQ007).

Security invariant: ``fetch_account_email`` reads the token from disk but
returns ONLY the email — these tests assert the email leaks out and nothing
token-shaped does.
"""

from __future__ import annotations

import json
from pathlib import Path

from scitex_agent_container._account.account_identity import fetch_account_email


class _FakeResponse:
    """Minimal context-manager response exposing ``.read()`` bytes."""

    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_exc: object) -> bool:
        return False

    def read(self) -> bytes:
        return self._body


def _opener_returning(payload: dict) -> object:
    """Build an ``urlopen``-alike that returns ``payload`` as JSON."""

    def _opener(_req: object, timeout: int = 0) -> _FakeResponse:  # noqa: ARG001
        return _FakeResponse(json.dumps(payload).encode())

    return _opener


def _write_creds(path: Path, access_token: str | None) -> None:
    """Write a ``.credentials.json`` with (or without) an access token."""
    path.parent.mkdir(parents=True, exist_ok=True)
    oauth: dict = {}
    if access_token is not None:
        oauth["accessToken"] = access_token
    path.write_text(json.dumps({"claudeAiOauth": oauth}))


def test_returns_email_from_profile_payload(tmp_path: Path) -> None:
    # Arrange
    creds = tmp_path / ".credentials.json"
    _write_creds(creds, "sk-ant-live-token")
    opener = _opener_returning({"account": {"email": "ywatanabe@scitex.ai"}})
    # Act
    email = fetch_account_email(creds, opener=opener)
    # Assert
    assert email == "ywatanabe@scitex.ai"


def test_returns_none_when_no_access_token(tmp_path: Path) -> None:
    # Arrange — credentials file exists but carries no accessToken.
    creds = tmp_path / ".credentials.json"
    _write_creds(creds, None)
    # Act — opener is never reached; a bare urlopen would fail the test.
    email = fetch_account_email(creds, opener=_opener_returning({"account": {}}))
    # Assert
    assert email is None


def test_returns_none_when_credentials_file_missing(tmp_path: Path) -> None:
    # Arrange
    creds = tmp_path / "absent" / ".credentials.json"
    # Act
    email = fetch_account_email(creds, opener=_opener_returning({}))
    # Assert
    assert email is None


def test_returns_none_on_network_error(tmp_path: Path) -> None:
    # Arrange
    creds = tmp_path / ".credentials.json"
    _write_creds(creds, "sk-ant-live-token")

    def _boom(_req: object, timeout: int = 0):  # noqa: ARG001
        raise OSError("network unreachable")

    # Act
    email = fetch_account_email(creds, opener=_boom)
    # Assert
    assert email is None


def test_returns_none_on_malformed_payload(tmp_path: Path) -> None:
    # Arrange — well-formed JSON but missing the account.email shape.
    creds = tmp_path / ".credentials.json"
    _write_creds(creds, "sk-ant-live-token")
    opener = _opener_returning({"unexpected": "shape"})
    # Act
    email = fetch_account_email(creds, opener=opener)
    # Assert
    assert email is None


def test_does_not_return_the_access_token(tmp_path: Path) -> None:
    # Arrange — the token must never leak out as the "email".
    creds = tmp_path / ".credentials.json"
    _write_creds(creds, "sk-ant-secret-token")
    opener = _opener_returning({"account": {"email": "a@b.com"}})
    # Act
    email = fetch_account_email(creds, opener=opener)
    # Assert
    assert email is not None and "sk-ant-" not in email
