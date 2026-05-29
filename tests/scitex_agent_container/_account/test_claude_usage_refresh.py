"""Tests for ``refresh_account_credentials`` — the headless OAuth refresh path.

No-mocks (PA-306): real bytes on tmp_path, opener callable injected for HTTP.
AAA marker comments; one assertion per test.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from scitex_agent_container._account import claude_usage as cu


# ---------------------------------------------------------------------------
# Fakes — same pattern as test_claude_usage.py (kept local for clarity).
# ---------------------------------------------------------------------------


class _FakeResp:
    """Plain callable response object — not a mock."""

    def __init__(self, body: bytes):
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def read(self):
        return self._body


def _opener_returning(payload: Any):
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


def _seed_creds(
    path: Path,
    *,
    access: str = "old-access",
    refresh: str | None = "the-refresh",
    client_id: str | None = "cid",
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    oauth: dict[str, Any] = {"accessToken": access}
    if refresh is not None:
        oauth["refreshToken"] = refresh
    if client_id is not None:
        oauth["clientId"] = client_id
    path.write_text(json.dumps({"claudeAiOauth": oauth}))


# ---------------------------------------------------------------------------
# Happy path — refresh writes new access_token to the SAME file.
# ---------------------------------------------------------------------------


def test_refresh_account_credentials_writes_new_token_to_same_file(
    tmp_path: Path,
) -> None:
    # Arrange
    creds = tmp_path / "acct" / ".credentials.json"
    _seed_creds(creds)
    opener = _opener_returning({"access_token": "NEW", "expires_in": 3600})
    # Act
    cu.refresh_account_credentials(creds, opener=opener)
    # Assert
    written = json.loads(creds.read_text())
    assert written["claudeAiOauth"]["accessToken"] == "NEW"


def test_refresh_account_credentials_reports_success_true_on_happy_path(
    tmp_path: Path,
) -> None:
    # Arrange
    creds = tmp_path / "acct" / ".credentials.json"
    _seed_creds(creds)
    opener = _opener_returning({"access_token": "NEW", "expires_in": 3600})
    # Act
    result = cu.refresh_account_credentials(creds, opener=opener)
    # Assert
    assert result["success"] is True


def test_refresh_account_credentials_reports_iso_expiry(tmp_path: Path) -> None:
    # Arrange
    creds = tmp_path / "acct" / ".credentials.json"
    _seed_creds(creds)
    opener = _opener_returning({"access_token": "NEW", "expires_in": 3600})
    # Act
    result = cu.refresh_account_credentials(creds, opener=opener)
    # Assert — ISO-8601 expiry surfaced for the CLI
    assert isinstance(result["expires_at"], str) and "T" in result["expires_at"]


# ---------------------------------------------------------------------------
# Degraded paths — never raises, surfaces error string.
# ---------------------------------------------------------------------------


def test_refresh_account_credentials_returns_error_when_no_refresh_token(
    tmp_path: Path,
) -> None:
    # Arrange — credentials file with accessToken but no refreshToken.
    creds = tmp_path / "acct" / ".credentials.json"
    _seed_creds(creds, refresh=None)
    # Act
    result = cu.refresh_account_credentials(creds)
    # Assert
    assert "no refresh_token" in (result["error"] or "")


def test_refresh_account_credentials_returns_error_when_no_client_id(
    tmp_path: Path,
) -> None:
    # Arrange
    creds = tmp_path / "acct" / ".credentials.json"
    _seed_creds(creds, client_id=None)
    # Act
    result = cu.refresh_account_credentials(creds)
    # Assert
    assert "no clientId" in (result["error"] or "")


def test_refresh_account_credentials_returns_error_when_endpoint_rejects(
    tmp_path: Path,
) -> None:
    # Arrange — refresh endpoint returns junk (no access_token field).
    creds = tmp_path / "acct" / ".credentials.json"
    _seed_creds(creds)
    opener = _opener_returning({"not_access_token": "x"})
    # Act
    result = cu.refresh_account_credentials(creds, opener=opener)
    # Assert
    assert "refresh endpoint rejected" in (result["error"] or "")


def test_refresh_account_credentials_returns_error_when_creds_file_missing(
    tmp_path: Path,
) -> None:
    # Arrange — path that does not exist.
    creds = tmp_path / "does-not-exist" / ".credentials.json"
    # Act
    result = cu.refresh_account_credentials(creds)
    # Assert
    assert "not found" in (result["error"] or "")


def test_refresh_account_credentials_returns_error_when_endpoint_network_error(
    tmp_path: Path,
) -> None:
    # Arrange — opener raises OSError to simulate a network failure.
    creds = tmp_path / "acct" / ".credentials.json"
    _seed_creds(creds)
    opener = _opener_raising(OSError("boom"))
    # Act
    result = cu.refresh_account_credentials(creds, opener=opener)
    # Assert — the inner refresh helper returns None on network error and the
    # CLI helper reports endpoint rejection (no new token minted).
    assert "refresh endpoint rejected" in (result["error"] or "")


# ---------------------------------------------------------------------------
# No-leak guards — return dict must never carry token bytes or secret markers.
# ---------------------------------------------------------------------------


def test_refresh_account_credentials_does_not_leak_token_in_result_keys(
    tmp_path: Path,
) -> None:
    # Arrange
    creds = tmp_path / "acct" / ".credentials.json"
    _seed_creds(creds)
    opener = _opener_returning({"access_token": "NEW", "expires_in": 3600})
    forbidden = ("accesstoken", "refreshtoken", "sk-ant-", "bearer", "clientid")
    # Act
    result = cu.refresh_account_credentials(creds, opener=opener)
    # Assert
    for key in result:
        kl = key.lower()
        for needle in forbidden:
            assert needle not in kl


def test_refresh_account_credentials_does_not_leak_token_in_result_values(
    tmp_path: Path,
) -> None:
    # Arrange
    creds = tmp_path / "acct" / ".credentials.json"
    _seed_creds(creds, access="ORIGINAL-TOKEN", refresh="ORIGINAL-REFRESH")
    opener = _opener_returning(
        {"access_token": "BRAND-NEW-TOKEN", "expires_in": 3600}
    )
    leaky_substrings = ("original-token", "original-refresh", "brand-new-token")
    # Act
    result = cu.refresh_account_credentials(creds, opener=opener)
    # Assert — no token (old or new) is ever surfaced to the caller
    joined = " ".join(str(v) for v in result.values() if v is not None).lower()
    assert not any(needle in joined for needle in leaky_substrings)
