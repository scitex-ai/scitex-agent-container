"""Tests for claude_usage — cache, parsing, token refresh, HTTP path.

No-mocks pattern (PA-306): all HTTP is via an injected ``opener``
parameter on ``fetch_usage`` / ``_fetch_from_api`` / ``_refresh_access_token``.
Tests pass a hand-rolled callable that returns real ``urllib.response``-
shaped objects. Credential / cache files are real bytes on tmp_path.
"""

from __future__ import annotations

import json
import time
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import pytest

from scitex_agent_container._account import claude_usage as cu
from scitex_agent_container._account.claude_usage import (
    _EMPTY_RESULT,
    fetch_usage,
)

# ---------------------------------------------------------------------------
# Real fake response — has the protocol urllib callers expect.
# ---------------------------------------------------------------------------


class _FakeResp:
    """Plain callable response object. NOT a mock."""

    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._body


def _opener_returning(payload: Any):
    """Build an opener that returns ``payload`` (dict|list|bytes|str)."""
    if isinstance(payload, (dict, list)):
        raw = json.dumps(payload).encode()
    elif isinstance(payload, bytes):
        raw = payload
    else:
        raw = str(payload).encode()

    def opener(req, timeout=None):
        return _FakeResp(raw)

    return opener


def _opener_raising(exc: Exception):
    def opener(req, timeout=None):
        raise exc

    return opener


def _opener_sequence(*resps):
    """Each call returns / raises the next item in sequence."""
    state = {"i": 0}

    def opener(req, timeout=None):
        item = resps[state["i"]]
        state["i"] += 1
        if isinstance(item, Exception):
            raise item
        return _FakeResp(item if isinstance(item, bytes) else json.dumps(item).encode())

    return opener


# ---------------------------------------------------------------------------
# Filesystem helpers — real bytes on tmp_path.
# ---------------------------------------------------------------------------


def _make_home_with_creds(tmp_path: Path, expires_at_ms: int | None = None) -> Path:
    home = tmp_path / "home"
    (home / ".claude").mkdir(parents=True)
    creds: dict[str, Any] = {
        "claudeAiOauth": {
            "accessToken": "old-access",
            "refreshToken": "fake-refresh",
            "clientId": "fake-client",
        }
    }
    if expires_at_ms is not None:
        creds["claudeAiOauth"]["expiresAt"] = expires_at_ms
    (home / ".claude" / ".credentials.json").write_text(json.dumps(creds))
    return home


def _make_home_full_creds(tmp_path: Path) -> Path:
    home = tmp_path / "home"
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True)
    creds = {
        "claudeAiOauth": {
            "accessToken": "fake-access-token",
            "refreshToken": "fake-refresh-token",
            "clientId": "fake-client-id",
            "expiresAt": int(time.time() * 1000) + 3_600_000,
            "subscriptionType": "pro",
            "rateLimitTier": "standard",
        }
    }
    (claude_dir / ".credentials.json").write_text(json.dumps(creds))
    return home


def _make_fresh_cache(home: Path, data: dict[str, Any]) -> None:
    cache_dir = home / ".scitex" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    data = dict(data)
    data["fetched_at"] = datetime.now(timezone.utc).isoformat()
    data["from_cache"] = True
    (cache_dir / "claude_usage.json").write_text(json.dumps(data))


# ---------------------------------------------------------------------------
# Cache hit — opener must never be called.
# ---------------------------------------------------------------------------


def test_cache_hit_returns_cached_data_without_http_call(tmp_path: Path) -> None:
    # Arrange
    home = _make_home_full_creds(tmp_path)
    _make_fresh_cache(
        home,
        {
            "used_tokens_5h": 1_000,
            "limit_tokens_5h": 10_000,
            "used_pct_5h": 10.0,
            "reset_at_5h": "2026-01-01T00:00:00Z",
            "used_tokens_7d": 5_000,
            "limit_tokens_7d": 100_000,
            "used_pct_7d": 5.0,
            "reset_at_7d": "2026-01-08T00:00:00Z",
            "fetched_at": "",
            "from_cache": False,
            "error": None,
        },
    )

    def must_not_call(req, timeout=None):
        raise AssertionError("opener called despite fresh cache")

    # Act
    result = fetch_usage(home=home, opener=must_not_call)
    # Assert
    assert result["from_cache"] is True


def _opener_must_not_be_called(req, timeout=None):
    raise AssertionError("opener was called when cache was fresh")


def test_cache_hit_preserves_used_tokens_5h(tmp_path: Path) -> None:
    # Arrange
    home = _make_home_full_creds(tmp_path)
    _make_fresh_cache(home, dict(_EMPTY_RESULT, used_tokens_5h=1_000))
    # Act
    result = fetch_usage(home=home, opener=_opener_must_not_be_called)
    # Assert
    assert result["used_tokens_5h"] == 1_000


# ---------------------------------------------------------------------------
# Token-leak guard — returned dict never contains secret material.
# ---------------------------------------------------------------------------


_API_PAYLOAD_OK = {
    "five_hour": {"utilization": 10, "resets_at": "2026-01-01T05:00:00Z"},
    "seven_day": {"utilization": 4, "resets_at": "2026-01-08T00:00:00Z"},
}


def test_returned_dict_does_not_leak_token_in_keys(tmp_path: Path) -> None:
    # Arrange
    home = _make_home_full_creds(tmp_path)
    opener = _opener_returning(_API_PAYLOAD_OK)
    forbidden = (
        "accesstoken",
        "refreshtoken",
        "sk-ant-",
        "bearer",
        "password",
        "secret",
        "credential",
    )
    # Act
    result = fetch_usage(home=home, opener=opener)
    # Assert
    for key in result:
        kl = key.lower()
        for needle in forbidden:
            assert needle not in kl


def test_returned_dict_does_not_leak_token_in_values(tmp_path: Path) -> None:
    # Arrange
    home = _make_home_full_creds(tmp_path)
    opener = _opener_returning(_API_PAYLOAD_OK)
    forbidden_in_values = ("sk-ant-", "bearer ")
    # Act
    result = fetch_usage(home=home, opener=opener)
    # Assert
    str_values = [
        str(v).lower()
        for v in result.values()
        if v is not None and not isinstance(v, bool)
    ]
    assert not any(needle in v for v in str_values for needle in forbidden_in_values)


# ---------------------------------------------------------------------------
# Error path — network failure returns dict, never raises.
# ---------------------------------------------------------------------------


def test_network_error_returns_dict_instead_of_raising(tmp_path: Path) -> None:
    # Arrange
    home = _make_home_full_creds(tmp_path)
    opener = _opener_raising(OSError("simulated network failure"))
    # Act
    result = fetch_usage(home=home, opener=opener)
    # Assert
    assert isinstance(result, dict)


def test_network_error_populates_error_field(tmp_path: Path) -> None:
    # Arrange
    home = _make_home_full_creds(tmp_path)
    opener = _opener_raising(OSError("boom"))
    # Act
    result = fetch_usage(home=home, opener=opener)
    # Assert
    assert isinstance(result["error"], str) and result["error"]


def test_network_error_leaves_quota_fields_none(tmp_path: Path) -> None:
    # Arrange
    home = _make_home_full_creds(tmp_path)
    opener = _opener_raising(OSError("boom"))
    # Act
    result = fetch_usage(home=home, opener=opener)
    # Assert
    assert result["used_tokens_5h"] is None


def test_missing_credentials_returns_error_dict(tmp_path: Path) -> None:
    # Arrange
    home = tmp_path / "empty"
    (home / ".claude").mkdir(parents=True)
    # Act
    result = fetch_usage(home=home)
    # Assert
    assert result["error"] is not None


# ---------------------------------------------------------------------------
# _load_json edge cases — pure file I/O.
# ---------------------------------------------------------------------------


def test_load_json_returns_none_for_list_payload(tmp_path: Path) -> None:
    # Arrange
    p = tmp_path / "x.json"
    p.write_text("[1, 2, 3]")
    # Act
    out = cu._load_json(p)
    # Assert
    assert out is None


def test_load_json_returns_none_for_missing_file(tmp_path: Path) -> None:
    # Arrange
    p = tmp_path / "nope.json"
    # Act
    out = cu._load_json(p)
    # Assert
    assert out is None


def test_load_json_returns_none_for_malformed_json(tmp_path: Path) -> None:
    # Arrange
    p = tmp_path / "x.json"
    p.write_text("{nope")
    # Act
    out = cu._load_json(p)
    # Assert
    assert out is None


# ---------------------------------------------------------------------------
# _read_tokens corner cases.
# ---------------------------------------------------------------------------


def test_read_tokens_returns_all_none_when_oauth_key_missing(tmp_path: Path) -> None:
    # Arrange
    home = tmp_path / "h"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / ".credentials.json").write_text("{}")
    # Act
    out = cu._read_tokens(home)
    # Assert
    assert out == (None, None, None, None)


def test_read_tokens_returns_all_none_when_oauth_value_not_dict(
    tmp_path: Path,
) -> None:
    # Arrange
    home = tmp_path / "h"
    (home / ".claude").mkdir(parents=True)
    (home / ".claude" / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": "string-value"})
    )
    # Act
    out = cu._read_tokens(home)
    # Assert
    assert out == (None, None, None, None)


def test_read_tokens_returns_all_none_when_creds_file_absent(tmp_path: Path) -> None:
    # Arrange
    home = tmp_path / "h"
    home.mkdir()
    # Act
    out = cu._read_tokens(home)
    # Assert
    assert out == (None, None, None, None)


# ---------------------------------------------------------------------------
# _is_token_expired — pure logic.
# ---------------------------------------------------------------------------


def test_is_token_expired_returns_false_for_none_input() -> None:
    # Arrange
    expiry = None
    # Act
    out = cu._is_token_expired(expiry)
    # Assert
    assert out is False


def test_is_token_expired_returns_true_for_past_timestamp() -> None:
    # Arrange
    expiry = 0
    # Act
    out = cu._is_token_expired(expiry)
    # Assert
    assert out is True


def test_is_token_expired_returns_false_for_future_timestamp() -> None:
    # Arrange
    expiry = int(time.time() * 1000) + 60 * 60 * 1000
    # Act
    out = cu._is_token_expired(expiry)
    # Assert
    assert out is False


# ---------------------------------------------------------------------------
# _refresh_access_token via real opener injection.
# ---------------------------------------------------------------------------


def test_refresh_access_token_returns_new_token_on_success(tmp_path: Path) -> None:
    # Arrange
    home = _make_home_with_creds(tmp_path, expires_at_ms=0)
    opener = _opener_returning({"access_token": "NEW-TOKEN", "expires_in": 3600})
    # Act
    out = cu._refresh_access_token(home, "fake-refresh", "fake-client", opener=opener)
    # Assert
    assert out == "NEW-TOKEN"


def test_refresh_access_token_writes_new_token_to_creds_file(tmp_path: Path) -> None:
    # Arrange
    home = _make_home_with_creds(tmp_path, expires_at_ms=0)
    opener = _opener_returning({"access_token": "NEW-TOKEN", "expires_in": 3600})
    # Act
    cu._refresh_access_token(home, "fake-refresh", "fake-client", opener=opener)
    # Assert
    creds = json.loads((home / ".claude" / ".credentials.json").read_text())
    assert creds["claudeAiOauth"]["accessToken"] == "NEW-TOKEN"


def test_refresh_access_token_updates_expires_at(tmp_path: Path) -> None:
    # Arrange
    home = _make_home_with_creds(tmp_path, expires_at_ms=0)
    opener = _opener_returning({"access_token": "NEW", "expires_in": 3600})
    # Act
    cu._refresh_access_token(home, "fake-refresh", "fake-client", opener=opener)
    # Assert
    creds = json.loads((home / ".claude" / ".credentials.json").read_text())
    assert creds["claudeAiOauth"]["expiresAt"] > int(time.time() * 1000)


def test_refresh_access_token_returns_none_on_network_error(tmp_path: Path) -> None:
    # Arrange
    home = _make_home_with_creds(tmp_path, expires_at_ms=0)
    opener = _opener_raising(OSError("boom"))
    # Act
    out = cu._refresh_access_token(home, "fake-refresh", "fake-client", opener=opener)
    # Assert
    assert out is None


def test_refresh_access_token_returns_none_when_response_missing_access_token(
    tmp_path: Path,
) -> None:
    # Arrange
    home = _make_home_with_creds(tmp_path, expires_at_ms=0)
    opener = _opener_returning({"not_access_token": "x"})
    # Act
    out = cu._refresh_access_token(home, "fake-refresh", "fake-client", opener=opener)
    # Assert
    assert out is None


def test_refresh_access_token_returns_token_when_creds_file_unwritable(
    tmp_path: Path,
) -> None:
    # Arrange — real read-only credentials file via chmod
    home = _make_home_with_creds(tmp_path, expires_at_ms=0)
    creds_file = home / ".claude" / ".credentials.json"
    creds_file.chmod(0o444)  # read-only
    opener = _opener_returning({"access_token": "NEW", "expires_in": 1000})
    try:
        # Act
        out = cu._refresh_access_token(
            home, "fake-refresh", "fake-client", opener=opener
        )
        # Assert
        assert out == "NEW"
    finally:
        creds_file.chmod(0o644)


# ---------------------------------------------------------------------------
# Cache helpers — pure file I/O on tmp_path.
# ---------------------------------------------------------------------------


def test_read_cache_returns_none_when_fetched_at_missing(tmp_path: Path) -> None:
    # Arrange
    home = tmp_path / "h"
    cache_dir = home / ".scitex" / "cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "claude_usage.json").write_text(json.dumps({"x": 1}))
    # Act
    out = cu._read_cache(home)
    # Assert
    assert out is None


def test_read_cache_returns_none_when_fetched_at_unparseable(tmp_path: Path) -> None:
    # Arrange
    home = tmp_path / "h"
    cache_dir = home / ".scitex" / "cache"
    cache_dir.mkdir(parents=True)
    (cache_dir / "claude_usage.json").write_text(
        json.dumps({"fetched_at": "not-a-timestamp"})
    )
    # Act
    out = cu._read_cache(home)
    # Assert
    assert out is None


def test_read_cache_returns_none_when_entry_is_older_than_ttl(tmp_path: Path) -> None:
    # Arrange
    home = tmp_path / "h"
    cache_dir = home / ".scitex" / "cache"
    cache_dir.mkdir(parents=True)
    stale = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    (cache_dir / "claude_usage.json").write_text(json.dumps({"fetched_at": stale}))
    # Act
    out = cu._read_cache(home)
    # Assert
    assert out is None


def test_read_cache_accepts_naive_timestamp_as_utc(tmp_path: Path) -> None:
    # Arrange
    home = tmp_path / "h"
    cache_dir = home / ".scitex" / "cache"
    cache_dir.mkdir(parents=True)
    # Naive timestamp — production treats it as UTC for back-compat.
    naive = datetime.now(timezone.utc).replace(tzinfo=None).isoformat()
    (cache_dir / "claude_usage.json").write_text(
        json.dumps({"fetched_at": naive, "used_tokens_5h": 5})
    )
    # Act
    out = cu._read_cache(home)
    # Assert
    assert out is not None and out["from_cache"] is True


def test_write_cache_returns_silently_when_parent_path_is_a_file(
    tmp_path: Path,
) -> None:
    # Arrange — block .scitex with a regular file so mkdir(parents=True) fails
    home = tmp_path / "h"
    home.mkdir()
    blocker = home / ".scitex"
    blocker.write_text("not a dir")
    # Act
    result = cu._write_cache(home, {"ok": True})
    # Assert
    assert result is None  # production returns None on best-effort cache writes


# ---------------------------------------------------------------------------
# _fetch_from_api response-shape branches.
# ---------------------------------------------------------------------------


def test_fetch_from_api_returns_payload_dict_unchanged() -> None:
    # Arrange
    payload = {
        "five_hour": {"utilization": 12, "resets_at": "2026-01-01T05:00:00Z"},
        "seven_day": {"utilization": 3, "resets_at": "2026-01-08T00:00:00Z"},
    }
    opener = _opener_returning(payload)
    # Act
    out = cu._fetch_from_api("tok", opener=opener)
    # Assert
    assert out == payload


def test_fetch_from_api_returns_none_for_non_dict_payload() -> None:
    # Arrange — list payloads no longer match the documented shape.
    opener = _opener_returning(["not", "a", "dict"])
    # Act
    out = cu._fetch_from_api("tok", opener=opener)
    # Assert
    assert out is None


def test_fetch_from_api_sends_anthropic_beta_header() -> None:
    # Arrange
    captured: dict[str, str] = {}

    def capture_opener(req, timeout=None):
        for header_name, header_value in req.header_items():
            captured[header_name.lower()] = header_value
        return _FakeResp(b"{}")

    # Act
    cu._fetch_from_api("tok", opener=capture_opener)
    # Assert — the OAuth usage endpoint requires this preview-gating header.
    assert captured.get("anthropic-beta") == "oauth-2025-04-20"


def test_fetch_from_api_returns_none_on_malformed_json() -> None:
    # Arrange
    opener = _opener_returning(b"not json{")
    # Act
    out = cu._fetch_from_api("tok", opener=opener)
    # Assert
    assert out is None


def test_fetch_from_api_reraises_http_401() -> None:
    # Arrange
    err = urllib.error.HTTPError("http://x", 401, "unauth", {}, None)  # type: ignore[arg-type]
    opener = _opener_raising(err)
    # Act
    raised = pytest.raises(urllib.error.HTTPError)
    # Assert
    with raised:
        cu._fetch_from_api("tok", opener=opener)


def test_fetch_from_api_returns_none_on_http_500() -> None:
    # Arrange
    err = urllib.error.HTTPError("http://x", 500, "oops", {}, None)  # type: ignore[arg-type]
    opener = _opener_raising(err)
    # Act
    out = cu._fetch_from_api("tok", opener=opener)
    # Assert
    assert out is None


# ---------------------------------------------------------------------------
# _parse_windows — pure logic.
# ---------------------------------------------------------------------------


def test_parse_windows_extracts_five_hour_pct_from_new_shape() -> None:
    # Arrange — real OAuth usage response shape (2026-Q2 percentage model).
    payload = {
        "five_hour": {"utilization": 23, "resets_at": "2026-05-29T05:00:00Z"},
        "seven_day": {"utilization": 7, "resets_at": "2026-06-01T00:00:00Z"},
    }
    # Act
    out = cu._parse_windows(payload)
    # Assert
    assert out["used_pct_5h"] == 23.0


def test_parse_windows_extracts_seven_day_pct_from_new_shape() -> None:
    # Arrange
    payload = {
        "five_hour": {"utilization": 23, "resets_at": "2026-05-29T05:00:00Z"},
        "seven_day": {"utilization": 7, "resets_at": "2026-06-01T00:00:00Z"},
    }
    # Act
    out = cu._parse_windows(payload)
    # Assert
    assert out["used_pct_7d"] == 7.0


def test_parse_windows_extracts_five_hour_reset_timestamp() -> None:
    # Arrange
    payload = {
        "five_hour": {"utilization": 23, "resets_at": "2026-05-29T05:00:00Z"},
    }
    # Act
    out = cu._parse_windows(payload)
    # Assert
    assert out["reset_at_5h"] == "2026-05-29T05:00:00Z"


def test_parse_windows_clamps_utilization_above_one_hundred() -> None:
    # Arrange — server should never overshoot, but defend against it anyway.
    payload = {"five_hour": {"utilization": 150}}
    # Act
    out = cu._parse_windows(payload)
    # Assert
    assert out["used_pct_5h"] == 100.0


def test_parse_windows_treats_non_numeric_utilization_as_none() -> None:
    # Arrange
    payload = {"five_hour": {"utilization": "not-a-number"}}
    # Act
    out = cu._parse_windows(payload)
    # Assert
    assert out["used_pct_5h"] is None


def test_parse_windows_ignores_non_dict_window_value() -> None:
    # Arrange
    payload = {"five_hour": "not-a-dict", "seven_day": {"utilization": 5}}
    # Act
    out = cu._parse_windows(payload)
    # Assert
    assert out.get("used_pct_5h") is None and out["used_pct_7d"] == 5.0


def test_parse_windows_returns_empty_dict_for_non_dict_payload() -> None:
    # Arrange
    payload = ["not", "a", "dict"]
    # Act
    out = cu._parse_windows(payload)  # type: ignore[arg-type]
    # Assert
    assert out == {}


# ---------------------------------------------------------------------------
# _check_no_token_leak — security guard.
# ---------------------------------------------------------------------------


def test_check_no_token_leak_raises_on_forbidden_key() -> None:
    # Arrange
    payload = {"accessToken": "x"}
    # Act
    raised = pytest.raises(RuntimeError, match="forbidden key")
    # Assert
    with raised:
        cu._check_no_token_leak(payload)


def test_check_no_token_leak_raises_on_forbidden_value() -> None:
    # Arrange
    payload = {"note": "bearer foo"}
    # Act
    raised = pytest.raises(RuntimeError, match="forbidden value")
    # Assert
    with raised:
        cu._check_no_token_leak(payload)


def test_check_no_token_leak_ignores_none_and_bool_values(tmp_path: Path) -> None:
    # Arrange
    payload: dict[str, Any] = {"x": None, "y": True}
    # Act
    cu._check_no_token_leak(payload)  # must not raise
    # Assert
    assert True  # behavior is the absence of an exception


# ---------------------------------------------------------------------------
# fetch_usage — refresh-then-succeed integrations (real opener sequencing).
# ---------------------------------------------------------------------------


def test_fetch_usage_refreshes_expired_token_then_returns_data(tmp_path: Path) -> None:
    # Arrange — expired token + opener that returns refresh THEN API payload.
    home = _make_home_with_creds(tmp_path, expires_at_ms=0)
    opener = _opener_sequence(
        {"access_token": "NEW", "expires_in": 3600},  # refresh
        {"five_hour": {"utilization": 17}, "seven_day": {"utilization": 2}},
    )
    # Act
    result = fetch_usage(home=home, opener=opener)
    # Assert
    assert result["used_pct_5h"] == 17.0


def test_fetch_usage_handles_401_then_refresh_then_retry(tmp_path: Path) -> None:
    # Arrange — fresh token; first API call 401s, refresh succeeds, retry succeeds.
    home = _make_home_with_creds(
        tmp_path, expires_at_ms=int(time.time() * 1000) + 60_000
    )
    http_401 = urllib.error.HTTPError("http://x", 401, "", {}, None)  # type: ignore[arg-type]
    opener = _opener_sequence(
        http_401,  # API attempt 1
        {"access_token": "REFRESHED", "expires_in": 3600},  # refresh
        {"five_hour": {"utilization": 4}, "seven_day": {"utilization": 9}},
    )
    # Act
    result = fetch_usage(home=home, opener=opener)
    # Assert
    assert result["used_pct_7d"] == 9.0


def test_fetch_usage_returns_error_when_refresh_after_401_fails(
    tmp_path: Path,
) -> None:
    # Arrange — first API 401, refresh returns no access_token.
    home = _make_home_with_creds(
        tmp_path, expires_at_ms=int(time.time() * 1000) + 60_000
    )
    http_401 = urllib.error.HTTPError("http://x", 401, "", {}, None)  # type: ignore[arg-type]
    opener = _opener_sequence(
        http_401,  # API attempt 1
        {"not_access_token": "x"},  # refresh returns junk
    )
    # Act
    result = fetch_usage(home=home, opener=opener)
    # Assert
    assert "HTTP 401" in (result["error"] or "")


def test_fetch_usage_returns_error_for_unparsable_response(tmp_path: Path) -> None:
    # Arrange
    home = _make_home_with_creds(
        tmp_path, expires_at_ms=int(time.time() * 1000) + 60_000
    )
    opener = _opener_returning(b"not-json{")
    # Act
    result = fetch_usage(home=home, opener=opener)
    # Assert
    assert "Failed to fetch or parse" in (result["error"] or "")


def test_fetch_usage_writes_cache_file_on_success(tmp_path: Path) -> None:
    # Arrange
    home = _make_home_with_creds(
        tmp_path, expires_at_ms=int(time.time() * 1000) + 60_000
    )
    opener = _opener_returning(
        {"five_hour": {"utilization": 12, "resets_at": "now"}}
    )
    # Act
    fetch_usage(home=home, opener=opener)
    # Assert
    assert (home / ".scitex" / "cache" / "claude_usage.json").is_file()
