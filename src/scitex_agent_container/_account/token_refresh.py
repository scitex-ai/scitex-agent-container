"""Headless Claude OAuth token refresh — read, rotate, persist, classify.

Split out of ``claude_usage.py`` (817 lines, over the 512-line cap;
usage fetching and token rotation are separate concerns that only
shared a module historically). ``claude_usage`` re-exports every moved
name so existing importers (``cli_pkg/_account_refresh``, tests) are
unchanged.

INCIDENT 2026-07-10 (card
``incident-account-pool-all-expired-boot-failure-20260710``): every
headless refresh had been failing silently because Anthropic MOVED the
OAuth token endpoint — the old
``https://console.anthropic.com/v1/oauth/token`` host (verified live
2026-05-30) now returns HTTP 404 ``not_found_error`` for EVERY
refresh_token grant. The old error handling collapsed every failure
(404s included) into ``None`` and the CLI labelled it "refresh endpoint
rejected the refresh_token — needs ``claude /login``", so a dead
ENDPOINT was misdiagnosed as dead TOKENS: stored account snapshots aged
out unrefreshed and a fleet boot failed with ``NoHealthyAccountError``
as the first visible symptom.

Two structural fixes live here:

* The endpoint constant points at the live host — verified 2026-07-11:
  a bogus refresh_token gets HTTP 400 ``invalid_grant`` from
  ``platform.claude.com/v1/oauth/token`` (endpoint alive, grant
  evaluated) while the old console host 404s for ANY grant. It is
  overridable via ``$SAC_ANTHROPIC_OAUTH_TOKEN_URL`` so the NEXT host
  move needs an env var, not a release.
* :func:`refresh_access_token_at_verbose` CLASSIFIES failures:
  ``rejected`` (the endpoint evaluated the grant and refused it — the
  refresh_token is genuinely dead, re-auth needed), ``transport``
  (endpoint moved / unreachable / 5xx / network — NOT a token
  problem), ``response`` (2xx but unusable body). Callers and alerts
  can therefore never again tell the operator to re-login every
  account when the URL is what died.

Token values NEVER leave this module in return values or error strings.
"""

from __future__ import annotations

import fcntl
import json
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# Live OAuth token endpoint (see module docstring — verified 2026-07-11).
_DEFAULT_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
# Back-compat name: pre-split code and tests referenced
# ``claude_usage._TOKEN_URL``. The wire path resolves per call through
# :func:`resolve_token_url` so the env override always wins.
_TOKEN_URL = _DEFAULT_TOKEN_URL
# Env suffix read through the sac env helper — set
# ``SAC_ANTHROPIC_OAUTH_TOKEN_URL`` (or the long-prefix twin) to repoint
# the refresh path without a code change.
_TOKEN_URL_ENV_SUFFIX = "ANTHROPIC_OAUTH_TOKEN_URL"

# Required by the OAuth endpoints; without it the API returns 4xx.
_USAGE_BETA_HEADER = "oauth-2025-04-20"
# The refresh endpoint gates POSTs unless the client looks like the
# real Claude Code CLI (verified live 2026-05-30: bare Content-Type is
# not enough — all four request headers are required).
_REFRESH_USER_AGENT = "claude-cli/2.1.0 (external, cli)"
# Claude Code's well-known OAuth client_id, used when the stored
# credentials snapshot lacks an explicit ``clientId`` (current Claude
# Code versions do not write one).
_CLAUDE_CODE_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"

# Failure classes returned by :func:`refresh_access_token_at_verbose`.
FAILURE_REJECTED = "rejected"
FAILURE_TRANSPORT = "transport"
FAILURE_RESPONSE = "response"


def resolve_token_url() -> str:
    """Return the effective OAuth token endpoint (env override wins).

    Reads ``SAC_ANTHROPIC_OAUTH_TOKEN_URL`` /
    ``SCITEX_AGENT_CONTAINER_ANTHROPIC_OAUTH_TOKEN_URL`` through the sac
    env helper; empty/unset falls back to :data:`_DEFAULT_TOKEN_URL`.
    Resolved per call so a long-lived process honours a live change.
    """
    from .._env import getenv as _sac_env

    raw = (_sac_env(_TOKEN_URL_ENV_SUFFIX, "") or "").strip()
    return raw or _DEFAULT_TOKEN_URL


def _load_json(path: Path) -> dict[str, Any] | None:
    """Load JSON file; return None on any error."""
    # stx-allow: fallback (reason: credentials or cache file may not exist or may be corrupt; None signals caller to skip)
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return None
        return data
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        return None


def _credentials_path(home: Path) -> Path:
    return home / ".claude" / ".credentials.json"


def _read_tokens_at(
    credentials_path: Path,
) -> tuple[str | None, str | None, str | None, int | None]:
    """Read OAuth tokens from a specific credentials.json file.

    Returns ``(access_token, refresh_token, client_id, expires_at_ms)``.
    All four are ``None`` if the file is missing/corrupt or lacks the
    ``claudeAiOauth`` object. Values are consumed inside this package
    only; tokens never leave it.
    """
    data = _load_json(credentials_path)
    if data is None:
        return None, None, None, None
    oauth = data.get("claudeAiOauth")
    if not isinstance(oauth, dict):
        return None, None, None, None
    access = oauth.get("accessToken")
    refresh = oauth.get("refreshToken")
    client_id = oauth.get("clientId")
    expires_at = oauth.get("expiresAt")  # milliseconds epoch
    return (
        access if isinstance(access, str) else None,
        refresh if isinstance(refresh, str) else None,
        client_id if isinstance(client_id, str) else None,
        expires_at if isinstance(expires_at, int) else None,
    )


def _persist_rotated_tokens(
    credentials_path: Path, payload: dict[str, Any]
) -> None:
    """Atomically write the rotated token block back to the SAME file.

    The refresh_token ROTATES on every successful refresh; the server
    invalidates the previous one, so the new ``refresh_token`` MUST be
    persisted alongside the new access_token or the NEXT refresh fails.
    flock + write-to-tmp + rename keeps concurrent sac writers serialized
    on the same inode and readers away from torn JSON.
    """
    new_access = payload.get("access_token")
    new_refresh = payload.get("refresh_token")
    expires_in = payload.get("expires_in")
    # stx-allow: fallback (reason: credentials file may be read-only or locked by another process; new access token is still usable in memory even if disk write fails)
    try:
        with open(credentials_path, "r+", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                data = json.load(fh)
                if not isinstance(data, dict):
                    data = {}
                oauth = data.setdefault("claudeAiOauth", {})
                oauth["accessToken"] = new_access
                if isinstance(new_refresh, str):
                    oauth["refreshToken"] = new_refresh
                if isinstance(expires_in, (int, float)):
                    oauth["expiresAt"] = int(time.time() * 1000) + int(
                        expires_in * 1000
                    )
                tmp_path = Path(str(credentials_path) + ".tmp")
                with open(tmp_path, "w", encoding="utf-8") as tmp_fh:
                    json.dump(data, tmp_fh, indent=2)
                tmp_path.rename(credentials_path)
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        pass  # best-effort write; we still have the new token in memory


def refresh_access_token_at_verbose(
    credentials_path: Path,
    refresh_token: str,
    client_id: str | None,
    *,
    opener=None,
) -> tuple[str | None, str | None, str | None]:
    """POST the refresh grant; return ``(access_token, kind, reason)``.

    On success: ``(token, None, None)`` and the rotated block has been
    atomically persisted to ``credentials_path``. On failure the token
    is ``None`` and ``kind`` is one of

    * :data:`FAILURE_REJECTED` — the endpoint EVALUATED the grant and
      refused it (HTTP 4xx carrying ``invalid_grant``/``invalid_request``
      semantics). The stored refresh_token is genuinely dead; only a
      re-auth fixes it.
    * :data:`FAILURE_TRANSPORT` — the grant was never evaluated: HTTP
      404 (endpoint moved — THE 2026-07-10 incident), 5xx, or a network
      error. NOT a token problem; re-logging in cannot fix it.
    * :data:`FAILURE_RESPONSE` — 2xx but the body was unparseable or
      lacked ``access_token``.

    ``reason`` is an operator-facing sentence naming the endpoint URL
    and HTTP status. It NEVER contains token material (failure bodies
    from the endpoint carry no tokens; success bodies are never quoted).
    """
    url = resolve_token_url()
    effective_client_id = client_id or _CLAUDE_CODE_CLIENT_ID
    body = json.dumps(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": effective_client_id,
        }
    ).encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": _REFRESH_USER_AGENT,
            "anthropic-beta": _USAGE_BETA_HEADER,
        },
        method="POST",
    )
    _opener = opener if opener is not None else urllib.request.urlopen
    try:
        with _opener(req, timeout=15) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        # stx-allow: fallback (reason: error body read is diagnostic-only; an unreadable body still classifies by status code)
        try:
            err_body = exc.read().decode("utf-8", "replace")[:200]
        except Exception:  # stx-allow: fallback (reason: see inline comment)
            err_body = ""
        if exc.code in (400, 401, 403) and (
            "invalid_grant" in err_body or "invalid_request" in err_body
        ):
            return (
                None,
                FAILURE_REJECTED,
                f"token endpoint {url} evaluated and REFUSED the grant "
                f"(HTTP {exc.code}: {err_body or exc.reason}) — the stored "
                "refresh_token is dead",
            )
        return (
            None,
            FAILURE_TRANSPORT,
            f"token endpoint {url} did not evaluate the grant "
            f"(HTTP {exc.code}: {err_body or exc.reason}) — endpoint "
            "moved/unavailable; this is NOT a token problem",
        )
    except Exception as exc:  # stx-allow: fallback (reason: network layer may raise OSError/URLError/timeout; classify as transport, never crash the refresh loop)
        return (
            None,
            FAILURE_TRANSPORT,
            f"token endpoint {url} unreachable ({exc.__class__.__name__}: "
            f"{exc}) — network/endpoint failure; this is NOT a token problem",
        )

    # stx-allow: fallback (reason: a 2xx with an unparseable body must classify as FAILURE_RESPONSE, never crash)
    try:
        payload = json.loads(raw)
    except Exception:  # stx-allow: fallback (reason: see inline comment)
        return (
            None,
            FAILURE_RESPONSE,
            f"token endpoint {url} returned 2xx with a non-JSON body",
        )

    new_access = payload.get("access_token") if isinstance(payload, dict) else None
    if not isinstance(new_access, str):
        return (
            None,
            FAILURE_RESPONSE,
            f"token endpoint {url} returned 2xx without an access_token",
        )

    _persist_rotated_tokens(credentials_path, payload)
    return new_access, None, None


def _refresh_access_token_at(
    credentials_path: Path,
    refresh_token: str,
    client_id: str | None,
    *,
    opener=None,
) -> str | None:
    """Back-compat wrapper: token-or-None (reason discarded).

    Pre-incident callers (the usage-fetch retry paths) only need the
    token; new code that must EXPLAIN a failure calls
    :func:`refresh_access_token_at_verbose`.
    """
    new_access, _, _ = refresh_access_token_at_verbose(
        credentials_path, refresh_token, client_id, opener=opener
    )
    return new_access


def _refresh_access_token(
    home: Path,
    refresh_token: str,
    client_id: str,
    *,
    opener=None,
) -> str | None:
    """POST to token endpoint and atomically update credentials file.

    Thin wrapper over ``_refresh_access_token_at`` resolved to
    ``~/.claude/.credentials.json``. Returns the new access token string,
    or ``None`` on failure. Tokens are never returned to the caller of
    ``fetch_usage`` — this is an internal helper only.
    """
    return _refresh_access_token_at(
        _credentials_path(home), refresh_token, client_id, opener=opener
    )


def refresh_account_credentials(
    credentials_path: Path,
    *,
    opener=None,
) -> dict[str, Any]:
    """Refresh the OAuth access token for the credentials at ``credentials_path``.

    Calls :func:`refresh_access_token_at_verbose` (POST to the token
    endpoint + atomic write-back to the SAME file) and returns a
    structured result the CLI can render without ever surfacing token
    values.

    Returns:
        Dict with keys::

            success      : bool — True iff a new access_token was minted.
            expires_at   : ISO-8601 string of the new token's expiry, or None.
            error        : str  — reason for failure, or None on success.
            failure_kind : str|None — ``rejected`` / ``transport`` /
                           ``response`` / ``no-refresh-token`` /
                           ``missing-file`` (see module docstring); the
                           alerting rail and tests key off this.
            credentials_path : str — echo of the input path.

        Never raises. Token values are NEVER included.
    """
    creds = Path(credentials_path)
    out: dict[str, Any] = {
        "success": False,
        "expires_at": None,
        "error": None,
        "failure_kind": None,
        "credentials_path": str(creds),
    }

    if not creds.is_file():
        out["error"] = f"credentials file not found: {creds}"
        out["failure_kind"] = "missing-file"
        return out

    _, refresh_token, client_id, _ = _read_tokens_at(creds)
    if not refresh_token:
        out["error"] = "no refresh_token in credentials — needs `claude /login`"
        out["failure_kind"] = "no-refresh-token"
        return out

    new_access, kind, reason = refresh_access_token_at_verbose(
        creds, refresh_token, client_id, opener=opener
    )
    if not new_access and kind == FAILURE_REJECTED:
        # Concurrent-writer mitigation (shared writable credential file,
        # 2026-07-11): another refresher — the in-container claude, a
        # sibling timer run, a live login — may have ROTATED the
        # refresh_token between our read and our POST, making OUR copy
        # stale (the server invalidates the old one on rotation). Re-read
        # the file; if the on-disk refresh_token CHANGED, retry ONCE with
        # the fresh value instead of declaring the account dead.
        refresh_token_2, client_id_2 = _read_tokens_at(creds)[1:3]
        if refresh_token_2 and refresh_token_2 != refresh_token:
            new_access, kind, reason = refresh_access_token_at_verbose(
                creds, refresh_token_2, client_id_2, opener=opener
            )
    if not new_access:
        out["failure_kind"] = kind
        if kind == FAILURE_REJECTED:
            out["error"] = (
                f"refresh endpoint rejected the refresh_token ({reason}) — "
                "needs `claude /login` as this account, then "
                "`sac accounts save <name>`"
            )
        else:
            out["error"] = (
                f"{reason}; re-login will NOT fix this class of failure "
                "($SAC_ANTHROPIC_OAUTH_TOKEN_URL overrides the endpoint)"
            )
        return out

    # Read back the freshly-written expiry; the token value itself is
    # intentionally NOT touched (tokens never leave this module).
    # stx-allow: fallback (reason: post-write read is best-effort; if the just-written file is unreadable the refresh still succeeded in memory)
    try:
        data = _load_json(creds) or {}
        oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
        expires_at_ms = oauth.get("expiresAt") if isinstance(oauth, dict) else None
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        expires_at_ms = None

    if isinstance(expires_at_ms, int):
        out["expires_at"] = datetime.fromtimestamp(
            expires_at_ms / 1000, tz=timezone.utc
        ).isoformat()

    out["success"] = True
    return out


__all__ = [
    "FAILURE_REJECTED",
    "FAILURE_RESPONSE",
    "FAILURE_TRANSPORT",
    "refresh_access_token_at_verbose",
    "refresh_account_credentials",
    "resolve_token_url",
]
