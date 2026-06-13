"""Tests for the Anthropic auth-token prefix classifier.

Guard purpose (see module docstring): defend against the
"OAuth-token-as-api-key mismap" fleet failure mode. The classifier is
a pure prefix check on the Anthropic credential shape:

* ``sk-ant-oat-…`` → OAuth access token (from ``claude login``); the
  canonical wiring is the ``.credentials.json`` bind, NOT an env var.
* ``sk-ant-api-…`` → console API key; this is what
  ``ANTHROPIC_API_KEY`` / ``SAC_ANTHROPIC_API_KEY`` are for.

Style: STX-TQ002 AAA markers, STX-TQ007 one assert per test, no mocks
(the classifier is a pure function — there is nothing to mock).
"""

from __future__ import annotations

import pytest

from scitex_agent_container._runners._anthropic_auth_classify import (
    API_KEY_PREFIX,
    OAUTH_PREFIX,
    assert_api_key,
    classify,
    is_api_key,
    is_oauth_token,
)


class TestIsOauthToken:
    """``sk-ant-oat-…`` tokens classify as OAuth; nothing else does."""

    def test_oauth_prefix_is_recognised(self) -> None:
        # Arrange
        token = "sk-ant-oat-01-AAAAAAAA"
        # Act
        result = is_oauth_token(token)
        # Assert
        assert result is True

    def test_api_key_prefix_is_not_oauth(self) -> None:
        # Arrange
        token = "sk-ant-api-03-BBBBBBBB"
        # Act
        result = is_oauth_token(token)
        # Assert
        assert result is False

    def test_oauth_token_with_surrounding_whitespace_still_classifies(self) -> None:
        # Arrange — operators routinely paste tokens with trailing \n.
        token = "  sk-ant-oat-01-CCCCCCCC\n"
        # Act
        result = is_oauth_token(token)
        # Assert
        assert result is True


class TestIsApiKey:
    """``sk-ant-api-…`` tokens classify as API key; OAuth tokens do not."""

    def test_api_key_prefix_is_recognised(self) -> None:
        # Arrange
        token = "sk-ant-api-03-DDDDDDDD"
        # Act
        result = is_api_key(token)
        # Assert
        assert result is True

    def test_oauth_prefix_is_not_api_key(self) -> None:
        # Arrange
        token = "sk-ant-oat-01-EEEEEEEE"
        # Act
        result = is_api_key(token)
        # Assert
        assert result is False


class TestClassify:
    """Three-way return: oauth / api_key / unknown."""

    @pytest.mark.parametrize(
        ("token", "expected"),
        [
            ("sk-ant-oat-01-FFFFFFFF", "oauth"),
            ("sk-ant-api-03-GGGGGGGG", "api_key"),
            ("sk-something-else-HHHHHHHH", "unknown"),
            ("", "unknown"),
        ],
    )
    def test_prefix_maps_to_expected_bucket(self, token: str, expected: str) -> None:
        # Arrange — token + expected come from parametrize.
        bucket = expected
        # Act
        result = classify(token)
        # Assert
        assert result == bucket


class TestAssertApiKeyRefusesOauth:
    """``assert_api_key`` must raise — LOUDLY — on a known OAuth token."""

    def test_oauth_token_raises_value_error(self) -> None:
        # Arrange
        token = "sk-ant-oat-01-IIIIIIII"
        # Act / Assert
        with pytest.raises(ValueError):
            assert_api_key(token)

    def test_oauth_error_message_names_the_canonical_env_var(self) -> None:
        # Arrange — operator-facing remediation must mention the env var
        # they have to drop, so a grep on the error finds the fix.
        token = "sk-ant-oat-01-JJJJJJJJ"
        # Act
        with pytest.raises(ValueError) as excinfo:
            assert_api_key(token)
        # Assert
        assert "SAC_ANTHROPIC_API_KEY" in str(excinfo.value)

    def test_oauth_error_message_points_at_credentials_bind(self) -> None:
        # Arrange — the remediation is the credentials.json bind; the
        # error must name it so the operator knows where the OAuth
        # token actually belongs.
        token = "sk-ant-oat-01-KKKKKKKK"
        # Act
        with pytest.raises(ValueError) as excinfo:
            assert_api_key(token)
        # Assert
        assert "credentials.json" in str(excinfo.value)


class TestAssertApiKeyAcceptsNonOauth:
    """``assert_api_key`` must NOT raise on API keys or unknown shapes."""

    def test_api_key_is_accepted_silently(self) -> None:
        # Arrange
        token = "sk-ant-api-03-LLLLLLLL"
        # Act
        result = assert_api_key(token)
        # Assert — function returns None on accept.
        assert result is None

    def test_unknown_shape_is_accepted_silently(self) -> None:
        # Arrange — fail-open on unknown so a future Anthropic prefix
        # doesn't brick the fleet on the day Anthropic ships it.
        token = "sk-future-shape-MMMMMMMM"
        # Act
        result = assert_api_key(token)
        # Assert
        assert result is None


class TestPrefixConstants:
    """The exported prefix constants are the canonical Anthropic shapes."""

    def test_oauth_prefix_is_canonical(self) -> None:
        # Arrange
        prefix = OAUTH_PREFIX
        # Act
        actual = str(prefix)
        # Assert
        assert actual == "sk-ant-oat-"

    def test_api_key_prefix_is_canonical(self) -> None:
        # Arrange
        prefix = API_KEY_PREFIX
        # Act
        actual = str(prefix)
        # Assert
        assert actual == "sk-ant-api-"
