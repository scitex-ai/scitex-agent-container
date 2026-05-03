"""Tests for claude_usage.fetch_usage().

Covers:
1. test_cache_hit        — fresh cache prevents HTTP calls
2. test_no_token_leak    — returned dict contains no token/secret material
3. test_error_returns_dict — network failure returns dict with error, no raise
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from scitex_agent_container._account.claude_usage import (
    _CACHE_TTL_SECONDS,
    _EMPTY_RESULT,
    fetch_usage,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_home(tmp_path: Path) -> Path:
    """Create a minimal fake home with a credentials file."""
    home = tmp_path / "home"
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True)

    creds = {
        "claudeAiOauth": {
            "accessToken": "fake-access-token",
            "refreshToken": "fake-refresh-token",
            "clientId": "fake-client-id",
            "expiresAt": int(time.time() * 1000) + 3_600_000,  # 1 hour from now
            "subscriptionType": "pro",
            "rateLimitTier": "standard",
        }
    }
    (claude_dir / ".credentials.json").write_text(json.dumps(creds))
    return home


def _make_fresh_cache(home: Path, data: dict[str, Any]) -> None:
    """Write a cache file that is under the TTL."""
    from datetime import datetime, timezone

    cache_dir = home / ".scitex" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    data = dict(data)
    data["fetched_at"] = datetime.now(timezone.utc).isoformat()
    data["from_cache"] = True
    (cache_dir / "claude_usage.json").write_text(json.dumps(data))


# ---------------------------------------------------------------------------
# Test 1: cache hit — no HTTP call
# ---------------------------------------------------------------------------


def test_cache_hit(tmp_path: Path) -> None:
    """If cache is fresh (<5 min), fetch_usage must NOT make any HTTP call."""
    home = _make_home(tmp_path)

    cached_data: dict[str, Any] = {
        "used_tokens_5h": 1000,
        "limit_tokens_5h": 10000,
        "used_pct_5h": 10.0,
        "reset_at_5h": "2026-01-01T00:00:00Z",
        "used_tokens_7d": 5000,
        "limit_tokens_7d": 100000,
        "used_pct_7d": 5.0,
        "reset_at_7d": "2026-01-08T00:00:00Z",
        "fetched_at": "",  # filled by _make_fresh_cache
        "from_cache": False,
        "error": None,
    }
    _make_fresh_cache(home, cached_data)

    call_count = {"n": 0}

    def fake_urlopen(*args, **kwargs):
        call_count["n"] += 1
        raise AssertionError("urlopen was called but cache should have been used")

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = fetch_usage(home=home)

    assert call_count["n"] == 0, "HTTP call was made despite fresh cache"
    assert result["from_cache"] is True
    assert result["used_tokens_5h"] == 1000
    assert result["error"] is None


# ---------------------------------------------------------------------------
# Test 2: no token leak
# ---------------------------------------------------------------------------


def test_no_token_leak(tmp_path: Path) -> None:
    """Returned dict must not contain token/secret material in keys or values."""
    home = _make_home(tmp_path)

    # Simulate a successful API response.
    api_payload = json.dumps(
        [
            {"window": "5h", "used": 2000, "limit": 20000, "resetAt": "2026-01-01T05:00:00Z"},
            {"window": "7d", "used": 8000, "limit": 200000, "resetAt": "2026-01-08T00:00:00Z"},
        ]
    ).encode()

    mock_resp = MagicMock()
    mock_resp.read.return_value = api_payload
    mock_resp.__enter__ = lambda s: s
    mock_resp.__exit__ = MagicMock(return_value=False)

    with patch("urllib.request.urlopen", return_value=mock_resp):
        result = fetch_usage(home=home)

    assert result["error"] is None, f"Unexpected error: {result['error']}"

    # Scan every key and value for token-like substrings.
    # NOTE: "token" is intentionally omitted from key checks because legitimate
    # metric keys like "used_tokens_5h" contain "token" as part of their name.
    # The implementation's _FORBIDDEN_KEY_SUBSTRINGS list matches this logic.
    forbidden_in_keys = ("accesstoken", "refreshtoken", "sk-ant-", "bearer", "password", "secret", "credential")
    forbidden_in_values = ("sk-ant-", "bearer ")

    for key, value in result.items():
        key_l = key.lower()
        for needle in forbidden_in_keys:
            assert needle not in key_l, (
                f"Forbidden substring {needle!r} found in key {key!r}"
            )
        if value is None or isinstance(value, bool):
            continue
        val_l = str(value).lower()
        for needle in forbidden_in_values:
            assert needle not in val_l, (
                f"Forbidden substring {needle!r} found in value under key {key!r}"
            )


# ---------------------------------------------------------------------------
# Test 3: error on network failure — returns dict, does not raise
# ---------------------------------------------------------------------------


def test_error_returns_dict(tmp_path: Path) -> None:
    """A network error must return a dict with ``error`` set; must not raise."""
    home = _make_home(tmp_path)

    import urllib.error

    def fake_urlopen(*args, **kwargs):
        raise OSError("simulated network failure")

    with patch("urllib.request.urlopen", side_effect=fake_urlopen):
        result = fetch_usage(home=home)

    # Must return a dict — not raise.
    assert isinstance(result, dict)

    # Must have all expected keys.
    for key in _EMPTY_RESULT:
        assert key in result, f"Missing key {key!r} in error result"

    # Error field must be set.
    assert result["error"] is not None
    assert isinstance(result["error"], str)
    assert len(result["error"]) > 0

    # Quota fields must be None.
    for field in (
        "used_tokens_5h",
        "limit_tokens_5h",
        "used_pct_5h",
        "reset_at_5h",
        "used_tokens_7d",
        "limit_tokens_7d",
        "used_pct_7d",
        "reset_at_7d",
    ):
        assert result[field] is None, f"Expected {field} to be None on error"


# ---------------------------------------------------------------------------
# Test 4: missing credentials file returns dict with error
# ---------------------------------------------------------------------------


def test_missing_credentials(tmp_path: Path) -> None:
    """Missing credentials file returns error dict without raising."""
    home = tmp_path / "empty_home"
    home.mkdir()
    (home / ".claude").mkdir()
    # No .credentials.json written

    result = fetch_usage(home=home)

    assert isinstance(result, dict)
    assert result["error"] is not None
    assert result["used_tokens_5h"] is None
