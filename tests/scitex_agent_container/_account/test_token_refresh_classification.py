"""Failure-class tests for ``_account.token_refresh`` (INCIDENT 2026-07-10).

The incident: Anthropic moved the OAuth token endpoint; the old
``console.anthropic.com`` host 404s every refresh_token grant, and the
old error handling collapsed EVERY failure into "refresh endpoint
rejected the refresh_token — needs `claude /login`". Operators were told
to re-login healthy accounts while the URL was what had died. These
tests lock the honest classification: transport/endpoint failures must
NEVER read as token death, and only a genuine ``invalid_grant`` may
prescribe a re-login.

No-mocks (PA-306): real bytes on tmp_path; HTTP is injected as a plain
opener callable raising REAL ``urllib.error.HTTPError`` objects (the
production seam, same shape as the sibling refresh tests). AAA marker
comments; one assertion per test.
"""

from __future__ import annotations

import io
import json
import urllib.error
from pathlib import Path
from typing import Any

from scitex_agent_container._account import token_refresh as tr

_INCIDENT_404_BODY = (
    b'{"type":"error","error":{"type":"not_found_error","message":"Not found"}}'
)
_INVALID_GRANT_BODY = (
    b'{"error": "invalid_grant", '
    b'"error_description": "Refresh token not found or invalid"}'
)


def _seed_creds(path: Path, *, refresh: str = "the-refresh") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "old-access",
                    "refreshToken": refresh,
                    "clientId": "cid",
                }
            }
        )
    )


def _opener_http_error(code: int, body: bytes):
    """Opener raising a REAL urllib HTTPError with the given body."""

    def opener(req, timeout=None):
        raise urllib.error.HTTPError(
            req.full_url, code, "err", hdrs=None, fp=io.BytesIO(body)
        )

    return opener


# ---------------------------------------------------------------------------
# HTTP 404 (the incident shape) — transport class, never token death.
# ---------------------------------------------------------------------------


def test_http_404_classifies_as_transport_failure(tmp_path: Path) -> None:
    # Arrange — the exact 404 body the dead console endpoint returns.
    creds = tmp_path / "acct" / ".credentials.json"
    _seed_creds(creds)
    opener = _opener_http_error(404, _INCIDENT_404_BODY)
    # Act
    result = tr.refresh_account_credentials(creds, opener=opener)
    # Assert
    assert result["failure_kind"] == tr.FAILURE_TRANSPORT


def test_http_404_error_never_prescribes_claude_login(tmp_path: Path) -> None:
    # Arrange — INCIDENT 2026-07-10 regression lock: a 404 (endpoint
    # moved/gone) must never tell the operator the token needs /login.
    creds = tmp_path / "acct" / ".credentials.json"
    _seed_creds(creds)
    opener = _opener_http_error(404, _INCIDENT_404_BODY)
    # Act
    result = tr.refresh_account_credentials(creds, opener=opener)
    # Assert
    assert "claude /login" not in (result["error"] or "")


def test_http_404_error_says_not_a_token_problem(tmp_path: Path) -> None:
    # Arrange
    creds = tmp_path / "acct" / ".credentials.json"
    _seed_creds(creds)
    opener = _opener_http_error(404, _INCIDENT_404_BODY)
    # Act
    result = tr.refresh_account_credentials(creds, opener=opener)
    # Assert
    assert "NOT a token problem" in (result["error"] or "")


def test_http_404_error_names_the_endpoint_url(tmp_path: Path) -> None:
    # Arrange — self-diagnosing errors: the message must carry the URL
    # that failed so the next endpoint move is identified from one line.
    creds = tmp_path / "acct" / ".credentials.json"
    _seed_creds(creds)
    opener = _opener_http_error(404, _INCIDENT_404_BODY)
    # Act
    result = tr.refresh_account_credentials(creds, opener=opener)
    # Assert
    assert "platform.claude.com" in (result["error"] or "")


# ---------------------------------------------------------------------------
# HTTP 400 invalid_grant — the ONE genuinely-dead-token class.
# ---------------------------------------------------------------------------


def test_http_400_invalid_grant_classifies_as_rejected(tmp_path: Path) -> None:
    # Arrange — the live platform endpoint's answer for a dead token.
    creds = tmp_path / "acct" / ".credentials.json"
    _seed_creds(creds)
    opener = _opener_http_error(400, _INVALID_GRANT_BODY)
    # Act
    result = tr.refresh_account_credentials(creds, opener=opener)
    # Assert
    assert result["failure_kind"] == tr.FAILURE_REJECTED


def test_http_400_invalid_grant_prescribes_claude_login(tmp_path: Path) -> None:
    # Arrange — a genuinely-dead refresh_token IS fixed by re-auth.
    creds = tmp_path / "acct" / ".credentials.json"
    _seed_creds(creds)
    opener = _opener_http_error(400, _INVALID_GRANT_BODY)
    # Act
    result = tr.refresh_account_credentials(creds, opener=opener)
    # Assert
    assert "claude /login" in (result["error"] or "")


# ---------------------------------------------------------------------------
# Endpoint URL resolution — env override so the next move needs no release.
# ---------------------------------------------------------------------------


def test_default_token_url_targets_platform_claude_host() -> None:
    # Arrange — INCIDENT 2026-07-10: console.anthropic.com is dead for
    # refresh grants (404); the platform host is the verified-live one.
    # Act
    url = tr.resolve_token_url()
    # Assert
    assert url == "https://platform.claude.com/v1/oauth/token"


def test_env_override_repoints_token_endpoint(
    tmp_path: Path, env_save_restore
) -> None:
    # Arrange
    env_save_restore.set(
        "SAC_ANTHROPIC_OAUTH_TOKEN_URL", "https://example.test/oauth/token"
    )
    # Act
    url = tr.resolve_token_url()
    # Assert
    assert url == "https://example.test/oauth/token"


# ---------------------------------------------------------------------------
# Concurrent-rotation retry — shared writable credential file (2026-07-11).
# ---------------------------------------------------------------------------


def test_rejected_grant_retries_once_with_rotated_on_disk_token(
    tmp_path: Path,
) -> None:
    # Arrange — first POST gets invalid_grant; by then ANOTHER writer has
    # rotated the on-disk refresh_token (shared :rw credential file). The
    # refresher must re-read and succeed with the rotated token instead
    # of declaring the account dead.
    creds = tmp_path / "acct" / ".credentials.json"
    _seed_creds(creds, refresh="STALE-REFRESH")
    calls: list[dict[str, Any]] = []

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return json.dumps(
                {"access_token": "FRESH", "refresh_token": "R2", "expires_in": 3600}
            ).encode()

    def opener(req, timeout=None):
        body = json.loads(req.data.decode())
        calls.append(body)
        if body["refresh_token"] == "STALE-REFRESH":
            # Simulate the concurrent writer landing the rotation on disk
            # BEFORE the failure returns, then refuse the stale grant.
            _seed_creds(creds, refresh="ROTATED-BY-PEER")
            raise urllib.error.HTTPError(
                req.full_url, 400, "err", hdrs=None, fp=io.BytesIO(_INVALID_GRANT_BODY)
            )
        return _Resp()

    # Act
    result = tr.refresh_account_credentials(creds, opener=opener)
    # Assert
    assert result["success"] is True


def test_rejected_grant_with_unchanged_disk_token_stays_rejected(
    tmp_path: Path,
) -> None:
    # Arrange — invalid_grant with NO concurrent rotation on disk: there
    # is nothing fresher to retry with; the account is genuinely dead.
    creds = tmp_path / "acct" / ".credentials.json"
    _seed_creds(creds, refresh="ONLY-REFRESH")
    opener = _opener_http_error(400, _INVALID_GRANT_BODY)
    # Act
    result = tr.refresh_account_credentials(creds, opener=opener)
    # Assert
    assert result["failure_kind"] == tr.FAILURE_REJECTED
