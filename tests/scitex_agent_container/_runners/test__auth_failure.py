"""Tests for the auth/credential-death classifier.

The classifier turns a generic SDK exception into a LOUD, specific
operator signal: when the failure carries an Anthropic auth-rejection
signature it returns a message that names the cause and carries the
``claude login`` refresh hint; otherwise it returns ``None`` so the
caller keeps its generic ``sdk-crash`` handling.

Style: AAA markers, one assert per test, no mocks (pure-function input).
"""

from __future__ import annotations

import pytest

from scitex_agent_container._runners._auth_failure import (
    AUTH_FAILURE_CAUSE,
    REFRESH_HINT,
    classify_auth_failure,
)


class TestRecognisesAuthFailures:
    """Anthropic auth-rejection signatures must classify as auth-expired."""

    @pytest.mark.parametrize(
        "message",
        [
            "API error 401 Unauthorized",
            "ProcessError: authentication_error: invalid api key",
            "OAuth token has expired",
            "Your credit balance is too low",
            "Command failed: 401",
        ],
    )
    def test_auth_signature_returns_hint_bearing_message(self, message: str) -> None:
        # Arrange
        exc = RuntimeError(message)
        # Act
        result = classify_auth_failure(exc)
        # Assert
        assert result is not None and REFRESH_HINT in result

    def test_classified_message_preserves_original_text(self) -> None:
        # Arrange
        exc = RuntimeError("HTTP 401 from api.anthropic.com")
        # Act
        result = classify_auth_failure(exc)
        # Assert
        assert "HTTP 401 from api.anthropic.com" in result


class TestPreflightPhrasings:
    """Messages emitted by the provision/preflight layer also classify."""

    @pytest.mark.parametrize(
        "message",
        [
            "OAuth token in /home/x/.claude/.credentials.json expired 5 seconds ago. "
            "Run `claude login` to refresh.",
            "OAuth credentials at /tmp/.credentials.json are not usable. "
            "Run `claude login`.",
            "no Anthropic auth available — run `claude /login`.",
        ],
    )
    def test_preflight_message_classifies_as_auth(self, message: str) -> None:
        # Arrange
        exc = RuntimeError(message)
        # Act
        result = classify_auth_failure(exc)
        # Assert
        assert result is not None


class TestNonAuthFailures:
    """Generic SDK/network faults must NOT be misclassified as auth."""

    @pytest.mark.parametrize(
        "message",
        [
            "Connection reset by peer",
            "asyncio.TimeoutError after 25s",
            "ModuleNotFoundError: claude_agent_sdk",
            "ValueError: bad tool result",
            "",
        ],
    )
    def test_non_auth_failure_returns_none(self, message: str) -> None:
        # Arrange
        exc = RuntimeError(message)
        # Act
        result = classify_auth_failure(exc)
        # Assert
        assert result is None


class TestCause:
    """The cause identifier is the distinct, groupable ``auth-expired``."""

    def test_cause_is_auth_expired(self) -> None:
        # Arrange
        cause = AUTH_FAILURE_CAUSE
        # Act
        actual = str(cause)
        # Assert
        assert actual == "auth-expired"
