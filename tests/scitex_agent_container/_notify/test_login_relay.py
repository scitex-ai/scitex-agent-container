"""Tests for the OAuth login-relay helpers.

URL scraping is pure-string; the email path is exercised through the real
``send_email`` with a hand-rolled transport injected via ``send_fn`` (no
monkeypatch) and recipient/password supplied on the real ``os.environ`` by
the ``email_env`` fixture (see conftest).
"""

from __future__ import annotations

import os

import pytest

from scitex_agent_container._notify.login_relay import (
    extract_oauth_url,
    send_login_url_email,
)

_URL = "https://claude.ai/oauth/authorize?code=true&client_id=abc"


class TestExtractOauthUrl:
    def test_returns_url_when_present(self):
        # Arrange
        pane = f"Paste this URL to sign in:\n{_URL}\nPaste code here:"
        # Act
        url = extract_oauth_url(pane)
        # Assert
        assert url == _URL

    def test_returns_none_when_absent(self):
        # Arrange
        pane = "just normal agent output, no authorize link here"
        # Act
        url = extract_oauth_url(pane)
        # Assert
        assert url is None

    def test_returns_none_for_empty_pane(self):
        # Arrange
        pane = ""
        # Act
        url = extract_oauth_url(pane)
        # Assert
        assert url is None


@pytest.fixture
def relayed(email_env):
    """Run ``send_login_url_email`` through a capturing fake transport.

    Returns ``(ok, captured)`` so the return value and each formatted
    field can be asserted in isolation.
    """
    os.environ["SCITEX_AGENT_CONTAINER_EMAIL_TO"] = "op@example.com"
    os.environ["SCITEX_AGENT_CONTAINER_EMAIL_PASSWORD"] = "pw"
    captured: dict = {}
    ok = send_login_url_email("cap-002", _URL, send_fn=lambda **kw: captured.update(kw))
    return ok, captured


class TestSendLoginUrlEmail:
    def test_returns_true_on_send(self, relayed):
        # Arrange
        ok, _captured = relayed
        # Act
        delivered = ok
        # Assert
        assert delivered is True

    def test_subject_names_the_agent(self, relayed):
        # Arrange
        _ok, captured = relayed
        # Act
        subject = captured["subject"]
        # Assert
        assert "cap-002" in subject

    def test_body_contains_the_oauth_url(self, relayed):
        # Arrange
        _ok, captured = relayed
        # Act
        body = captured["body"]
        # Assert
        assert _URL in body

    def test_body_contains_the_relay_command(self, relayed):
        # Arrange
        _ok, captured = relayed
        # Act
        body = captured["body"]
        # Assert
        assert "sac send cap-002" in body
