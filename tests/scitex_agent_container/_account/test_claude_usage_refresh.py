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


def test_refresh_account_credentials_uses_default_client_id_when_missing(
    tmp_path: Path,
) -> None:
    # Arrange — creds file omits clientId (Claude Code does not write one).
    creds = tmp_path / "acct" / ".credentials.json"
    _seed_creds(creds, client_id=None)
    opener = _opener_returning(
        {"access_token": "NEW", "refresh_token": "NEW-REFRESH", "expires_in": 3600}
    )
    # Act
    result = cu.refresh_account_credentials(creds, opener=opener)
    # Assert — refresh succeeds via the constant fallback, no hard error.
    assert result["success"] is True


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


# ---------------------------------------------------------------------------
# Live-verified recipe coverage — endpoint URL, Cloudflare headers,
# rotated refresh_token persistence, well-known client_id fallback.
# ---------------------------------------------------------------------------


def _capturing_opener() -> tuple[dict, "callable"]:
    """Build an opener that captures the urllib Request and replies with the
    Anthropic token-endpoint success shape. Returns (state, opener).
    """
    state: dict[str, Any] = {}

    def opener(req, timeout=None):
        state["url"] = req.full_url
        state["method"] = req.get_method()
        state["headers"] = {k.lower(): v for k, v in req.header_items()}
        state["body"] = json.loads(req.data.decode("utf-8")) if req.data else None

        class _Resp:
            def __enter__(self_inner):
                return self_inner

            def __exit__(self_inner, *args):
                return False

            def read(self_inner):
                return json.dumps(
                    {
                        "access_token": "FRESH-ACCESS",
                        "refresh_token": "FRESH-REFRESH",
                        "expires_in": 3600,
                        "scope": "user:inference",
                        "token_type": "Bearer",
                    }
                ).encode()

        return _Resp()

    return state, opener


def test_refresh_request_uses_console_anthropic_endpoint(tmp_path: Path) -> None:
    # Arrange
    creds = tmp_path / "acct" / ".credentials.json"
    _seed_creds(creds)
    state, opener = _capturing_opener()
    # Act
    cu.refresh_account_credentials(creds, opener=opener)
    # Assert — claude.ai endpoint is Cloudflare-gated; the real endpoint
    # lives on the Anthropic console.
    assert state["url"] == "https://console.anthropic.com/v1/oauth/token"


def test_refresh_request_sends_required_cloudflare_user_agent(
    tmp_path: Path,
) -> None:
    # Arrange
    creds = tmp_path / "acct" / ".credentials.json"
    _seed_creds(creds)
    state, opener = _capturing_opener()
    # Act
    cu.refresh_account_credentials(creds, opener=opener)
    # Assert — must look like the real Claude Code CLI to pass Cloudflare.
    assert state["headers"].get("user-agent") == "claude-cli/2.1.0 (external, cli)"


def test_refresh_request_sends_required_anthropic_beta_header(
    tmp_path: Path,
) -> None:
    # Arrange
    creds = tmp_path / "acct" / ".credentials.json"
    _seed_creds(creds)
    state, opener = _capturing_opener()
    # Act
    cu.refresh_account_credentials(creds, opener=opener)
    # Assert
    assert state["headers"].get("anthropic-beta") == "oauth-2025-04-20"


def test_refresh_request_sends_required_accept_header(tmp_path: Path) -> None:
    # Arrange
    creds = tmp_path / "acct" / ".credentials.json"
    _seed_creds(creds)
    state, opener = _capturing_opener()
    # Act
    cu.refresh_account_credentials(creds, opener=opener)
    # Assert
    assert state["headers"].get("accept") == "application/json"


def test_refresh_body_uses_default_client_id_when_creds_lack_one(
    tmp_path: Path,
) -> None:
    # Arrange — credentials file with no clientId.
    creds = tmp_path / "acct" / ".credentials.json"
    _seed_creds(creds, client_id=None)
    state, opener = _capturing_opener()
    # Act
    cu.refresh_account_credentials(creds, opener=opener)
    # Assert — request body falls back to the well-known Claude Code client_id.
    assert state["body"]["client_id"] == "9d1c250a-e61b-44d9-88ed-5944d1962f5e"


def test_refresh_body_uses_stored_client_id_when_creds_have_one(
    tmp_path: Path,
) -> None:
    # Arrange — credentials file carries an explicit clientId.
    creds = tmp_path / "acct" / ".credentials.json"
    _seed_creds(creds, client_id="explicit-from-disk")
    state, opener = _capturing_opener()
    # Act
    cu.refresh_account_credentials(creds, opener=opener)
    # Assert — stored value wins over the constant.
    assert state["body"]["client_id"] == "explicit-from-disk"


def test_refresh_persists_rotated_refresh_token_to_same_file(
    tmp_path: Path,
) -> None:
    # Arrange — refresh response carries a new refresh_token. The server
    # rotates the refresh_token on every successful refresh, so we MUST
    # persist it or the next refresh fails (the old refresh_token is now
    # invalidated server-side).
    creds = tmp_path / "acct" / ".credentials.json"
    _seed_creds(creds, refresh="ORIGINAL-REFRESH")
    opener = _opener_returning(
        {
            "access_token": "NEW-ACCESS",
            "refresh_token": "ROTATED-REFRESH",
            "expires_in": 3600,
        }
    )
    # Act
    cu.refresh_account_credentials(creds, opener=opener)
    # Assert — atomic write-back persists the rotated refresh_token.
    written = json.loads(creds.read_text())
    assert written["claudeAiOauth"]["refreshToken"] == "ROTATED-REFRESH"


def test_refresh_preserves_stored_refresh_token_when_response_omits_it(
    tmp_path: Path,
) -> None:
    # Arrange — older response shapes may omit refresh_token rotation.
    creds = tmp_path / "acct" / ".credentials.json"
    _seed_creds(creds, refresh="ORIGINAL-REFRESH")
    opener = _opener_returning({"access_token": "NEW-ACCESS", "expires_in": 3600})
    # Act
    cu.refresh_account_credentials(creds, opener=opener)
    # Assert — when the response omits refresh_token, the stored one stays.
    written = json.loads(creds.read_text())
    assert written["claudeAiOauth"]["refreshToken"] == "ORIGINAL-REFRESH"

