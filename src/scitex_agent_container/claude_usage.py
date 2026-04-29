"""Live Claude API quota fetcher.

Fetches token-usage quota from ``GET https://api.anthropic.com/api/oauth/usage``
using the OAuth access token stored in ``~/.claude/.credentials.json``.

Design rules
------------
1. Tokens are read from disk only inside this module and are **never** returned
   to callers.  Only quota metrics leave this module.
2. If the access token is expired (``expiresAt`` in the past, or a 401 from the
   API), a refresh is attempted automatically via the ``refreshToken`` and
   ``clientId`` fields in the same file.
3. Results are cached in ``~/.scitex/cache/claude_usage.json`` for 5 minutes
   to avoid hammering the API.
4. The public function ``fetch_usage()`` **never raises**.  On any failure it
   returns a dict with ``error`` set and other quota fields ``None``.
5. Pure stdlib + ``urllib.request`` only.  No requests/httpx.
"""

from __future__ import annotations

import fcntl
import json
import os
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Public return-shape sentinel keys
# ---------------------------------------------------------------------------
_EMPTY_RESULT: dict[str, Any] = {
    "used_tokens_5h": None,
    "limit_tokens_5h": None,
    "used_pct_5h": None,
    "reset_at_5h": None,
    "used_tokens_7d": None,
    "limit_tokens_7d": None,
    "used_pct_7d": None,
    "reset_at_7d": None,
    "fetched_at": None,
    "from_cache": False,
    "error": None,
}

_CACHE_TTL_SECONDS = 300  # 5 minutes
_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
_TOKEN_URL = "https://claude.ai/api/auth/oauth/token"

# Substrings that must never appear in KEYS of the returned dict.
# These are chosen to catch accidental token field leaks (e.g. "accessToken")
# without false-positiving on legitimate quota metric keys like "used_tokens_5h".
_FORBIDDEN_KEY_SUBSTRINGS: tuple[str, ...] = (
    "sk-ant-",
    "bearer",
    "accesstoken",
    "refreshtoken",
    "clientid",
    "client_id",
    "password",
    "credential",
    "secret",
)

# Substrings that must never appear in VALUES of the returned dict.
_FORBIDDEN_VALUE_SUBSTRINGS: tuple[str, ...] = (
    "sk-ant-",
    "bearer ",
)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _iso_now() -> str:
    return _now_utc().isoformat()


def _load_json(path: Path) -> dict[str, Any] | None:
    """Load JSON file; return None on any error."""
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


def _cache_path(home: Path) -> Path:
    return home / ".scitex" / "cache" / "claude_usage.json"


# ---------------------------------------------------------------------------
# Token reading (internal only — tokens never returned)
# ---------------------------------------------------------------------------


def _read_tokens(home: Path) -> tuple[str | None, str | None, str | None, int | None]:
    """Return (access_token, refresh_token, client_id, expires_at_ms).

    All values are consumed inside this module only.
    """
    data = _load_json(_credentials_path(home))
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


def _is_token_expired(expires_at_ms: int | None) -> bool:
    if expires_at_ms is None:
        return False  # unknown — try anyway
    now_ms = int(time.time() * 1000)
    return now_ms >= expires_at_ms - 30_000  # 30-second buffer


# ---------------------------------------------------------------------------
# Token refresh (atomic write with flock)
# ---------------------------------------------------------------------------


def _refresh_access_token(
    home: Path,
    refresh_token: str,
    client_id: str,
) -> str | None:
    """POST to token endpoint and atomically update credentials file.

    Returns the new access token string, or None on failure.
    Tokens are never returned to the caller of ``fetch_usage`` — this is
    an internal helper only.
    """
    body = json.dumps(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        }
    ).encode()
    req = urllib.request.Request(
        _TOKEN_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
        payload = json.loads(raw)
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        return None

    new_access = payload.get("access_token")
    expires_in = payload.get("expires_in")
    if not isinstance(new_access, str):
        return None

    # Atomically update the credentials file.
    creds_path = _credentials_path(home)
    try:
        with open(creds_path, "r+", encoding="utf-8") as fh:
            fcntl.flock(fh, fcntl.LOCK_EX)
            try:
                data = json.load(fh)
                if not isinstance(data, dict):
                    data = {}
                oauth = data.setdefault("claudeAiOauth", {})
                oauth["accessToken"] = new_access
                if isinstance(expires_in, (int, float)):
                    oauth["expiresAt"] = int(time.time() * 1000) + int(
                        expires_in * 1000
                    )
                # Write to .tmp then rename for atomicity.
                tmp_path = Path(str(creds_path) + ".tmp")
                with open(tmp_path, "w", encoding="utf-8") as tmp_fh:
                    json.dump(data, tmp_fh, indent=2)
                tmp_path.rename(creds_path)
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        pass  # best-effort write; we still have the new token in memory

    return new_access


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------


def _read_cache(home: Path) -> dict[str, Any] | None:
    """Return cached result if it is younger than TTL, else None."""
    path = _cache_path(home)
    data = _load_json(path)
    if data is None:
        return None
    fetched_at_str = data.get("fetched_at")
    if not isinstance(fetched_at_str, str):
        return None
    try:
        fetched_at = datetime.fromisoformat(fetched_at_str)
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=timezone.utc)
    except ValueError:  # stx-allow: fallback (reason: type coercion or format mismatch)
        return None
    age = (_now_utc() - fetched_at).total_seconds()
    if age < _CACHE_TTL_SECONDS:
        data["from_cache"] = True
        return data
    return None


def _write_cache(home: Path, result: dict[str, Any]) -> None:
    """Write result to cache file (best-effort, never raises)."""
    path = _cache_path(home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(path) + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)
        tmp.rename(path)
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        pass


# ---------------------------------------------------------------------------
# API fetch
# ---------------------------------------------------------------------------


def _fetch_from_api(access_token: str) -> list[dict[str, Any]] | None:
    """Call the usage API and return the parsed JSON.

    Returns None on failure.  The response may be a list of window objects
    or a single dict.
    """
    req = urllib.request.Request(
        _USAGE_URL,
        headers={"Authorization": f"Bearer {access_token}"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:  # stx-allow: fallback (reason: expected failure — see inline comment)
        if exc.code == 401:
            raise  # caller handles 401 as token-expired
        return None
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        return None

    try:
        payload = json.loads(raw)
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        return None

    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        # Single-window or wrapped response
        windows = payload.get("windows") or payload.get("data")
        if isinstance(windows, list):
            return windows
        # Treat the dict itself as a single window if it has "window" key
        if "window" in payload:
            return [payload]
    return None


def _parse_windows(windows: list[dict[str, Any]]) -> dict[str, Any]:
    """Extract 5h and 7d quota values from the API window list."""
    result: dict[str, Any] = {}
    for w in windows:
        if not isinstance(w, dict):
            continue
        win = w.get("window")
        used = w.get("used")
        limit = w.get("limit")
        reset_at = w.get("resetAt")
        if win not in ("5h", "7d"):
            continue
        suffix = win.replace("h", "h").replace("d", "d")  # already correct
        result[f"used_tokens_{suffix}"] = used if isinstance(used, int) else None
        result[f"limit_tokens_{suffix}"] = limit if isinstance(limit, int) else None
        if (
            isinstance(used, int)
            and isinstance(limit, int)
            and limit > 0
        ):
            result[f"used_pct_{suffix}"] = round(used / limit * 100, 2)
        else:
            result[f"used_pct_{suffix}"] = None
        result[f"reset_at_{suffix}"] = reset_at if isinstance(reset_at, str) else None
    return result


# ---------------------------------------------------------------------------
# Security guard
# ---------------------------------------------------------------------------


def _check_no_token_leak(result: dict[str, Any]) -> None:
    """Raise RuntimeError if any key/value looks like a token or secret."""
    for key, value in result.items():
        key_l = key.lower()
        for needle in _FORBIDDEN_KEY_SUBSTRINGS:
            if needle in key_l:
                raise RuntimeError(
                    f"claude_usage: forbidden key detected: {key!r}"
                )
        if value is None or isinstance(value, bool):
            continue
        val_l = str(value).lower()
        for needle in _FORBIDDEN_VALUE_SUBSTRINGS:
            if needle in val_l:
                raise RuntimeError(
                    f"claude_usage: forbidden value under key {key!r}"
                )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_usage(home: Path | None = None) -> dict[str, Any]:
    """Return live Claude API quota metrics.

    Reads the OAuth access token from ``~/.claude/.credentials.json``,
    queries ``GET https://api.anthropic.com/api/oauth/usage``, and returns
    quota metrics.  Tokens are **never** included in the returned dict.

    Results are cached in ``~/.scitex/cache/claude_usage.json`` for 5 min.

    Args:
        home: Override for the home directory (used in tests).

    Returns:
        Dict with keys::

            used_tokens_5h, limit_tokens_5h, used_pct_5h, reset_at_5h,
            used_tokens_7d, limit_tokens_7d, used_pct_7d, reset_at_7d,
            fetched_at, from_cache, error

        Never raises.
    """
    _home = Path(home) if home is not None else Path.home()

    def _err(msg: str) -> dict[str, Any]:
        r = dict(_EMPTY_RESULT)
        r["fetched_at"] = _iso_now()
        r["error"] = msg
        return r

    # --- cache check --------------------------------------------------------
    cached = _read_cache(_home)
    if cached is not None:
        return cached

    # --- read tokens (never returned) ---------------------------------------
    access_token, refresh_token, client_id, expires_at_ms = _read_tokens(_home)
    if not access_token:
        return _err("No access token found in ~/.claude/.credentials.json")

    # --- refresh if expired -------------------------------------------------
    if _is_token_expired(expires_at_ms):
        if refresh_token and client_id:
            new_token = _refresh_access_token(_home, refresh_token, client_id)
            if new_token:
                access_token = new_token
            # If refresh failed, try with the old token anyway

    # --- API call -----------------------------------------------------------
    windows = None
    try:
        windows = _fetch_from_api(access_token)
    except urllib.error.HTTPError as exc:  # stx-allow: fallback (reason: expected failure — see inline comment)
        if exc.code == 401 and refresh_token and client_id:
            # Try refresh once on 401
            new_token = _refresh_access_token(_home, refresh_token, client_id)
            if new_token:
                access_token = new_token
                try:
                    windows = _fetch_from_api(access_token)
                except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
                    pass
        if windows is None:
            return _err(f"HTTP {exc.code} from usage API; refresh attempted")
    except Exception as exc:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        return _err(f"Network error: {exc}")

    if windows is None:
        return _err("Failed to fetch or parse usage API response")

    # --- build result -------------------------------------------------------
    result: dict[str, Any] = dict(_EMPTY_RESULT)
    result.update(_parse_windows(windows))
    result["fetched_at"] = _iso_now()
    result["from_cache"] = False
    result["error"] = None

    # Security guard — must run before cache write
    try:
        _check_no_token_leak(result)
    except RuntimeError as exc:  # stx-allow: fallback (reason: runtime state error — handled gracefully)
        return _err(str(exc))

    _write_cache(_home, result)
    return result
