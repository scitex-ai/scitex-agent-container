"""Tests for ``_state._meta.secrets`` — pure regex redaction helpers.

PS-202 src-tests mirror for the post-split submodule. The redaction
helpers are pure string transforms — no IO, no env — so every test
here uses literal input strings and asserts on literal output.
"""

from __future__ import annotations

import pytest

from scitex_agent_container._state._meta.secrets import (
    _SECRET_PATTERNS,
    _redact_secrets,
)


def test_redact_secrets_returns_empty_for_empty_input():
    # Arrange
    text = ""
    # Act
    out = _redact_secrets(text)
    # Assert
    assert out == ""


def test_redact_secrets_masks_anthropic_api_key():
    # Arrange
    text = "saw sk-ant-abcDEF_-1234 in logs"
    # Act
    out = _redact_secrets(text)
    # Assert
    assert "sk-ant-abcDEF_-1234" not in out


def test_redact_secrets_masks_workspace_token():
    # Arrange
    text = "wks_abc123XYZ leaked"
    # Act
    out = _redact_secrets(text)
    # Assert
    assert "wks_abc123XYZ" not in out


@pytest.mark.parametrize(
    "secret_input",
    [
        "token=hunter2",
        "secret: opensesame",
        "api_key=supersecret",
        "api-key=supersecret",
        "password=letmein",
        "bearer=eyJabc",
    ],
)
def test_redact_secrets_masks_keyword_assignments(secret_input):
    # Arrange
    text = secret_input
    # Act
    out = _redact_secrets(text)
    # Assert
    assert "REDACTED" in out


def test_redact_secrets_preserves_non_secret_text():
    # Arrange
    text = "just plain English with no credentials"
    # Act
    out = _redact_secrets(text)
    # Assert
    assert out == text


def test_secret_patterns_list_is_non_empty():
    # Arrange
    patterns = _SECRET_PATTERNS
    # Act
    count = len(patterns)
    # Assert
    assert count >= 3
