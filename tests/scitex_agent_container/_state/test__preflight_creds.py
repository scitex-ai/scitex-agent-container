"""Tests for the OAuth-credentials pre-dispatch expiry check.

Each test writes its own ``tmp_path / ".credentials.json"`` and passes
the path explicitly — none of these tests are allowed to read the
operator's real ``~/.claude/.credentials.json``.

Style rules in force here:
* One assert per test (STX-TQ007).
* AAA markers each on their own line.
* No monkeypatch / mocker fixture params (STX-NM002) — env mutation
  uses ``os.environ`` save/restore inside the test body.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from scitex_agent_container._state._preflight_creds import (
    EXPIRY_SKEW_SECONDS,
    check_oauth_token_expiry,
)

# Pinned wall-clock instant used by every test. Single named constant
# keeps STX-NL001 (PEP 515 thousands-separator) noise off inline literals
# and gives one place to change if a future test needs a different
# reference instant. All ms offsets are expressed via _MS constants so
# the arithmetic at the test sites stays simple-integer addition only.
_FROZEN_NOW = 1_700_000_000.0
_FROZEN_NOW_MS = 1_700_000_000_000
_ONE_HOUR_MS = 3_600_000
_SIX_HOURS_MS = 21_600_000
_ONE_MIN_MS = 60_000

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_creds(path: Path, expires_at_ms: int | float | str) -> None:
    """Materialise a credentials.json with the given expiresAt value.

    The file shape mirrors the real ``~/.claude/.credentials.json`` —
    only ``expiresAt`` varies per test. ``expiresAt`` is stored in
    milliseconds, matching what claude-code actually writes (see the
    bottom of _preflight_creds for the unit-detection logic).
    """
    payload = {
        "claudeAiOauth": {
            "accessToken": "sk-ant-oat-fake",
            "refreshToken": "sk-ant-ort-fake",
            "expiresAt": expires_at_ms,
            "scopes": ["user:inference"],
            "subscriptionType": "max",
        }
    }
    path.write_text(json.dumps(payload), encoding="utf-8")


@pytest.fixture
def clean_env():
    """Strip the API-key env vars for the duration of the test.

    Several tests need to assert that the OAuth branch is taken; the
    operator's shell may export ANTHROPIC_API_KEY which would short-
    circuit the check. Save+restore around the test body avoids the
    forbidden monkeypatch fixture.
    """
    # Arrange
    snapshot = {
        "ANTHROPIC_API_KEY": os.environ.pop("ANTHROPIC_API_KEY", None),
        "SAC_ANTHROPIC_API_KEY": os.environ.pop("SAC_ANTHROPIC_API_KEY", None),
    }
    try:
        yield
    finally:
        for key, value in snapshot.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


# ---------------------------------------------------------------------------
# Happy paths
# ---------------------------------------------------------------------------


class TestNotExpired:
    """Tokens with sufficient remaining lifetime must pass silently."""

    def test_oauth_token_not_expired_returns_none(
        self, tmp_path: Path, clean_env
    ) -> None:
        # Arrange
        creds = tmp_path / ".credentials.json"
        _write_creds(creds, expires_at_ms=_FROZEN_NOW_MS + _ONE_HOUR_MS)
        # Act
        result = check_oauth_token_expiry(creds, now=_FROZEN_NOW)
        # Assert
        assert result is None

    def test_oauth_token_far_future_returns_none(
        self, tmp_path: Path, clean_env
    ) -> None:
        # Arrange
        creds = tmp_path / ".credentials.json"
        _write_creds(creds, expires_at_ms=_FROZEN_NOW_MS + _SIX_HOURS_MS)
        # Act
        result = check_oauth_token_expiry(creds, now=_FROZEN_NOW)
        # Assert
        assert result is None


# ---------------------------------------------------------------------------
# Expiry / near-expiry
# ---------------------------------------------------------------------------


class TestExpired:
    """Expired or imminently-expiring tokens raise RuntimeError."""

    def test_oauth_token_expired_raises_runtime_error(
        self, tmp_path: Path, clean_env
    ) -> None:
        # Arrange
        creds = tmp_path / ".credentials.json"
        _write_creds(creds, expires_at_ms=_FROZEN_NOW_MS - _ONE_HOUR_MS)
        # Act
        action = lambda: check_oauth_token_expiry(creds, now=_FROZEN_NOW)
        # Assert
        with pytest.raises(RuntimeError, match=r"expired \d+ seconds ago"):
            action()

    def test_oauth_token_near_expiry_raises_runtime_error(
        self, tmp_path: Path, clean_env
    ) -> None:
        # Arrange
        creds = tmp_path / ".credentials.json"
        _write_creds(creds, expires_at_ms=_FROZEN_NOW_MS + _ONE_MIN_MS)
        # Act
        action = lambda: check_oauth_token_expiry(creds, now=_FROZEN_NOW)
        # Assert
        with pytest.raises(RuntimeError, match=r"expires in \d+ seconds"):
            action()


# ---------------------------------------------------------------------------
# Malformed / missing files
# ---------------------------------------------------------------------------


class TestFileShape:
    """Missing or malformed credentials files must fail loudly."""

    def test_missing_creds_file_raises_filenotfound(
        self, tmp_path: Path, clean_env
    ) -> None:
        # Arrange
        creds = tmp_path / ".credentials.json"
        # Act
        action = lambda: check_oauth_token_expiry(creds, now=_FROZEN_NOW)
        # Assert
        with pytest.raises(FileNotFoundError, match=str(creds)):
            action()

    def test_unparseable_creds_file_raises_value_error(
        self, tmp_path: Path, clean_env
    ) -> None:
        # Arrange
        creds = tmp_path / ".credentials.json"
        creds.write_text("this is not json {{{", encoding="utf-8")
        # Act
        action = lambda: check_oauth_token_expiry(creds, now=_FROZEN_NOW)
        # Assert
        with pytest.raises(ValueError, match="not valid JSON"):
            action()

    def test_credentials_field_missing_raises_value_error(
        self, tmp_path: Path, clean_env
    ) -> None:
        # Arrange
        creds = tmp_path / ".credentials.json"
        creds.write_text(json.dumps({"somethingElse": {}}), encoding="utf-8")
        # Act
        action = lambda: check_oauth_token_expiry(creds, now=_FROZEN_NOW)
        # Assert
        with pytest.raises(ValueError, match="claudeAiOauth"):
            action()

    def test_expires_at_missing_raises_value_error(
        self, tmp_path: Path, clean_env
    ) -> None:
        # Arrange
        creds = tmp_path / ".credentials.json"
        creds.write_text(
            json.dumps({"claudeAiOauth": {"accessToken": "x"}}),
            encoding="utf-8",
        )
        # Act
        action = lambda: check_oauth_token_expiry(creds, now=_FROZEN_NOW)
        # Assert
        with pytest.raises(ValueError, match="expiresAt"):
            action()


# ---------------------------------------------------------------------------
# API-key bypass
# ---------------------------------------------------------------------------


class TestApiKeyBypass:
    """Setting an API-key env var must skip the OAuth check entirely.

    Both tests build an *expired* credentials file and assert the
    check returns ``None`` anyway — proving the env var short-circuits
    before the file is even consulted.
    """

    def test_api_key_env_skips_check(self, tmp_path: Path, clean_env) -> None:
        # Arrange
        creds = tmp_path / ".credentials.json"
        _write_creds(creds, expires_at_ms=_FROZEN_NOW_MS - _ONE_HOUR_MS)
        os.environ["ANTHROPIC_API_KEY"] = "sk-ant-fake"
        # Act
        result = check_oauth_token_expiry(creds, now=_FROZEN_NOW)
        # Assert
        assert result is None

    def test_sac_api_key_env_skips_check(self, tmp_path: Path, clean_env) -> None:
        # Arrange
        creds = tmp_path / ".credentials.json"
        _write_creds(creds, expires_at_ms=_FROZEN_NOW_MS - _ONE_HOUR_MS)
        os.environ["SAC_ANTHROPIC_API_KEY"] = "sk-ant-fake"
        # Act
        result = check_oauth_token_expiry(creds, now=_FROZEN_NOW)
        # Assert
        assert result is None


# ---------------------------------------------------------------------------
# Skew threshold sanity (boundary at EXPIRY_SKEW_SECONDS)
# ---------------------------------------------------------------------------


class TestSkewThreshold:
    """EXPIRY_SKEW_SECONDS is the 5-min near-expiry cutoff."""

    def test_skew_threshold_is_five_minutes(self) -> None:
        # Arrange
        threshold_constant = EXPIRY_SKEW_SECONDS
        # Act
        actual = int(threshold_constant)
        # Assert
        assert actual == 300
