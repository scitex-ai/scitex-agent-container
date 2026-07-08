"""Best-effort OAuth "whoami" — the email a credential's token authenticates as.

The credential auto-sync identity-guard (``_account.creds_sync.sync_live``)
must derive the TARGET account store from the LIVE TOKEN's ACTUAL identity,
NOT from ``~/.claude.json``'s ``oauthAccount.emailAddress`` metadata — which
drifts out of sync with the token across logins/account switches. On
2026-07 that drift caused ``sync_live`` to snapshot one account's live
token into ANOTHER account's store (the metadata still named the previous
account), silently clobbering the second account's credential.

This module queries the OAuth profile endpoint with the token and returns
ONLY the resolved account email. Like :mod:`claude_usage`, the access token
is read from disk inside this module and is **never** returned to callers —
only the email leaves.

Best-effort + offline-safe: any failure (network down, non-2xx, malformed
body, absent token) returns ``None`` so the caller falls back to its
metadata-based path without crashing. Reuses the stdlib-urllib OAuth call
pattern + gating headers from :mod:`claude_usage`.
"""

from __future__ import annotations

import json
import urllib.request
from pathlib import Path

from .claude_usage import (
    _USAGE_BETA_HEADER,
    _USAGE_USER_AGENT,
    _read_tokens_at,
)

# Identity ("whoami") endpoint: returns the account the OAuth token
# authenticates as, under ``account.email``. Same anthropic-beta gating +
# user-agent as the usage endpoint on the same host.
_PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"


def _fetch_email_from_api(access_token: str, *, opener=None) -> str | None:
    """GET the OAuth profile and return ``account.email``, or None on failure.

    Best-effort: every failure mode (network error, non-2xx, non-JSON body,
    unexpected shape) collapses to ``None``. The token is used only to build
    the ``Authorization`` header here and never leaves the function.
    """
    req = urllib.request.Request(
        _PROFILE_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "anthropic-beta": _USAGE_BETA_HEADER,
            "User-Agent": _USAGE_USER_AGENT,
        },
        method="GET",
    )
    _opener = opener if opener is not None else urllib.request.urlopen
    # stx-allow: fallback (reason: whoami is best-effort; any network/parse
    # failure returns None so the identity-guard falls back to its offline
    # metadata path instead of crashing the credential sync.)
    try:
        with _opener(req, timeout=15) as resp:
            raw = resp.read()
        payload = json.loads(raw)
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        return None

    if not isinstance(payload, dict):
        return None
    account = payload.get("account")
    if not isinstance(account, dict):
        return None
    email = account.get("email")
    if isinstance(email, str) and email.strip():
        return email.strip()
    return None


def fetch_account_email(credentials_path: Path, *, opener=None) -> str | None:
    """Return the email the token in ``credentials_path`` authenticates as.

    Reads the OAuth access token from ``credentials_path`` internally (the
    token is NEVER returned), queries the OAuth profile endpoint, and returns
    ``response['account']['email']``.

    Best-effort: returns ``None`` when the token is absent, the network is
    unreachable, or the response is malformed. Never raises — the caller
    (the credential identity-guard) treats ``None`` as "identity could not
    be verified" and falls back to its offline path.

    Args:
        credentials_path: Path to a ``.credentials.json`` (the live cred, or
            a per-account snapshot).
        opener: Optional injection seam for tests (an ``urlopen``-alike).

    Returns:
        The account email string, or ``None`` on any failure/offline path.
    """
    access_token, _, _, _ = _read_tokens_at(Path(credentials_path))
    if not access_token:
        return None
    return _fetch_email_from_api(access_token, opener=opener)


__all__ = ["fetch_account_email"]
