"""Live Claude API quota fetcher.

Fetches usage quota from ``GET https://api.anthropic.com/api/oauth/usage``
using the OAuth access token stored in ``~/.claude/.credentials.json``.

The endpoint requires the ``anthropic-beta: oauth-2025-04-20`` header and
returns a percentage-utilization model rather than raw token counts::

    {
      "five_hour":  {"utilization": <pct 0-100>, "resets_at": "<iso>"},
      "seven_day":  {"utilization": <pct 0-100>, "resets_at": "<iso>"}
    }

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

The legacy ``used_tokens_*`` / ``limit_tokens_*`` keys are preserved in the
returned dict for back-compat with downstream consumers, but are always
``None`` under the new percentage-utilization API shape.
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

# ---------------------------------------------------------------------------
# Public return-shape sentinel keys
# ---------------------------------------------------------------------------
# ``used_tokens_*`` and ``limit_tokens_*`` are preserved (always ``None`` now)
# so downstream consumers reading these keys do not KeyError. The active
# percentage signal lives in ``used_pct_5h`` / ``used_pct_7d``.
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
# Required by the OAuth usage endpoint; without it the API returns 4xx and
# the response shape cannot be parsed. Tracked by Anthropic via the
# anthropic-beta preview gating header.
_USAGE_BETA_HEADER = "oauth-2025-04-20"
# Match the user-agent claude-hud sends so the OAuth gateway treats sac
# requests as a first-party CLI client.
_USAGE_USER_AGENT = "claude-code/2.1"

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
    # stx-allow: fallback (reason: credentials or cache file may not exist or may be corrupt; None signals caller to skip caching)
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


def _read_tokens_at(
    credentials_path: Path,
) -> tuple[str | None, str | None, str | None, int | None]:
    """Read OAuth tokens from a specific credentials.json file.

    Returns ``(access_token, refresh_token, client_id, expires_at_ms)``.
    All four are ``None`` if the file is missing/corrupt or lacks the
    ``claudeAiOauth`` object. Values are consumed inside this module only;
    tokens never leave the module.
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


def _read_tokens(home: Path) -> tuple[str | None, str | None, str | None, int | None]:
    """Return (access_token, refresh_token, client_id, expires_at_ms).

    Thin wrapper over ``_read_tokens_at`` resolved to
    ``~/.claude/.credentials.json``. All values are consumed inside this
    module only.
    """
    return _read_tokens_at(_credentials_path(home))


def _is_token_expired(expires_at_ms: int | None) -> bool:
    if expires_at_ms is None:
        return False  # unknown — try anyway
    now_ms = int(time.time() * 1000)
    return now_ms >= expires_at_ms - 30_000  # 30-second buffer


# ---------------------------------------------------------------------------
# Token refresh (atomic write with flock)
# ---------------------------------------------------------------------------


def _refresh_access_token_at(
    credentials_path: Path,
    refresh_token: str,
    client_id: str,
    *,
    opener=None,
) -> str | None:
    """POST to token endpoint and atomically update the given credentials file.

    Used by both the legacy ``_refresh_access_token`` wrapper (which
    targets ``~/.claude/.credentials.json``) and ``fetch_usage_for_credentials``
    (which targets a per-account snapshot at
    ``~/.scitex/agent-container/accounts/<name>/.credentials.json``).
    Returns the new access token string, or ``None`` on failure. Tokens
    are never returned to the caller of ``fetch_usage`` — this is an
    internal helper only.
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
    _opener = opener if opener is not None else urllib.request.urlopen
    # stx-allow: fallback (reason: token refresh endpoint may be unreachable or return malformed JSON; None causes caller to proceed with the old token)
    try:
        with _opener(req, timeout=15) as resp:
            raw = resp.read()
        payload = json.loads(raw)
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        return None

    new_access = payload.get("access_token")
    expires_in = payload.get("expires_in")
    if not isinstance(new_access, str):
        return None

    # Atomically update the credentials file.
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
                if isinstance(expires_in, (int, float)):
                    oauth["expiresAt"] = int(time.time() * 1000) + int(
                        expires_in * 1000
                    )
                # Write to .tmp then rename for atomicity.
                tmp_path = Path(str(credentials_path) + ".tmp")
                with open(tmp_path, "w", encoding="utf-8") as tmp_fh:
                    json.dump(data, tmp_fh, indent=2)
                tmp_path.rename(credentials_path)
            finally:
                fcntl.flock(fh, fcntl.LOCK_UN)
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        pass  # best-effort write; we still have the new token in memory

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
    # stx-allow: fallback (reason: cache file may contain a malformed timestamp; returning None forces a fresh API fetch)
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
    # stx-allow: fallback (reason: ~/.scitex/cache/ may be on a read-only filesystem or disk-full; cache is a performance optimisation and callers are unaffected)
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


def _fetch_from_api(access_token: str, *, opener=None) -> dict[str, Any] | None:
    """Call the usage API and return the parsed JSON payload dict.

    The endpoint is gated by the ``anthropic-beta`` preview header and
    returns a single object containing ``five_hour`` / ``seven_day``
    sub-objects with ``utilization`` (0-100 percentage) and ``resets_at``.
    Returns the parsed dict, or ``None`` on any non-fatal failure.
    """
    req = urllib.request.Request(
        _USAGE_URL,
        headers={
            "Authorization": f"Bearer {access_token}",
            "anthropic-beta": _USAGE_BETA_HEADER,
            "User-Agent": _USAGE_USER_AGENT,
        },
        method="GET",
    )
    _opener = opener if opener is not None else urllib.request.urlopen
    # stx-allow: fallback (reason: network timeout or DNS failure hitting api.anthropic.com; None tells caller quota is unavailable, error returned to user)
    try:
        with _opener(req, timeout=15) as resp:
            raw = resp.read()
    except (
        urllib.error.HTTPError
    ) as exc:  # stx-allow: fallback (reason: expected failure — see inline comment)
        if exc.code == 401:
            raise  # caller handles 401 as token-expired
        return None
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        return None

    # stx-allow: fallback (reason: API may return non-JSON body (maintenance page, Cloudflare HTML); None causes caller to return an error result)
    try:
        payload = json.loads(raw)
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        return None

    if not isinstance(payload, dict):
        return None
    return payload


def _coerce_utilization_pct(value: Any) -> float | None:
    """Clamp a utilization value to [0, 100]; return None for non-numeric input."""
    if isinstance(value, bool):
        # bools are ints in Python — explicitly reject so True doesn't become 1.0%.
        return None
    if not isinstance(value, (int, float)):
        return None
    pct = float(value)
    if pct < 0:
        pct = 0.0
    elif pct > 100:
        pct = 100.0
    return round(pct, 2)


def _parse_windows(payload: dict[str, Any]) -> dict[str, Any]:
    """Extract 5h and 7d utilization + reset timestamps from the API payload.

    Expected payload shape (new in 2026-Q2)::

        {"five_hour": {"utilization": 23, "resets_at": "<iso>"},
         "seven_day": {"utilization": 7,  "resets_at": "<iso>"}}

    The endpoint reports a 0-100 percentage directly, so the legacy
    token-count math (``used / limit * 100``) is gone — we surface the
    server-provided percentage as ``used_pct_5h`` / ``used_pct_7d`` and
    leave the legacy ``used_tokens_*`` / ``limit_tokens_*`` keys as
    ``None`` for back-compat with consumers that destructure them.
    """
    out: dict[str, Any] = {}
    if not isinstance(payload, dict):
        return out

    mapping = (("five_hour", "5h"), ("seven_day", "7d"))
    for src_key, suffix in mapping:
        window = payload.get(src_key)
        if not isinstance(window, dict):
            continue
        out[f"used_pct_{suffix}"] = _coerce_utilization_pct(window.get("utilization"))
        resets_at = window.get("resets_at")
        out[f"reset_at_{suffix}"] = resets_at if isinstance(resets_at, str) else None
    return out


# ---------------------------------------------------------------------------
# Security guard
# ---------------------------------------------------------------------------


def _check_no_token_leak(result: dict[str, Any]) -> None:
    """Raise RuntimeError if any key/value looks like a token or secret."""
    for key, value in result.items():
        key_l = key.lower()
        for needle in _FORBIDDEN_KEY_SUBSTRINGS:
            if needle in key_l:
                raise RuntimeError(f"claude_usage: forbidden key detected: {key!r}")
        if value is None or isinstance(value, bool):
            continue
        val_l = str(value).lower()
        for needle in _FORBIDDEN_VALUE_SUBSTRINGS:
            if needle in val_l:
                raise RuntimeError(f"claude_usage: forbidden value under key {key!r}")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def fetch_usage(home: Path | None = None, *, opener=None) -> dict[str, Any]:
    """Return live Claude API quota metrics.

    Reads the OAuth access token from ``~/.claude/.credentials.json``,
    queries ``GET https://api.anthropic.com/api/oauth/usage`` with the
    required ``anthropic-beta: oauth-2025-04-20`` header, and returns
    quota metrics.  Tokens are **never** included in the returned dict.

    Results are cached in ``~/.scitex/cache/claude_usage.json`` for 5 min.

    Args:
        home: Override for the home directory (used in tests).

    Returns:
        Dict with keys::

            used_tokens_5h, limit_tokens_5h, used_pct_5h, reset_at_5h,
            used_tokens_7d, limit_tokens_7d, used_pct_7d, reset_at_7d,
            fetched_at, from_cache, error

        The ``used_tokens_*`` / ``limit_tokens_*`` keys are always
        ``None`` under the new percentage-utilization API shape and
        remain in the schema for downstream back-compat.

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
            new_token = _refresh_access_token(
                _home, refresh_token, client_id, opener=opener
            )
            if new_token:
                access_token = new_token
            # If refresh failed, try with the old token anyway

    # --- API call -----------------------------------------------------------
    payload: dict[str, Any] | None = None
    # stx-allow: fallback (reason: network errors or unexpected exceptions from the usage API are caught and surfaced as an error dict rather than an unhandled exception)
    try:
        payload = _fetch_from_api(access_token, opener=opener)
    except (
        urllib.error.HTTPError
    ) as exc:  # stx-allow: fallback (reason: expected failure — see inline comment)
        if exc.code == 401 and refresh_token and client_id:
            # Try refresh once on 401
            new_token = _refresh_access_token(
                _home, refresh_token, client_id, opener=opener
            )
            if new_token:
                access_token = new_token
                # stx-allow: fallback (reason: second API attempt after token refresh may still fail due to network issues; pass lets the 401 handler return an error dict)
                try:
                    payload = _fetch_from_api(access_token, opener=opener)
                except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
                    pass
        if payload is None:
            return _err(f"HTTP {exc.code} from usage API; refresh attempted")
    except Exception as exc:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        return _err(f"Network error: {exc}")

    if payload is None:
        return _err("Failed to fetch or parse usage API response")

    # --- build result -------------------------------------------------------
    result: dict[str, Any] = dict(_EMPTY_RESULT)
    result.update(_parse_windows(payload))
    result["fetched_at"] = _iso_now()
    result["from_cache"] = False
    result["error"] = None

    # Security guard — must run before cache write
    try:
        _check_no_token_leak(result)
    except (
        RuntimeError
    ) as exc:  # stx-allow: fallback (reason: runtime state error — handled gracefully)
        return _err(str(exc))

    _write_cache(_home, result)
    return result


# ---------------------------------------------------------------------------
# Per-account fetch — uses an explicit credentials file, NOT the active one
# ---------------------------------------------------------------------------


def _per_account_cache_path(credentials_path: Path) -> Path:
    """Cache the per-account usage snapshot next to its credentials file.

    ``<account_dir>/usage.json`` is also the file
    ``_state.account_store.read_account_usage_cache`` reads, so populating
    it makes writers and readers symmetric — the ``account list`` JSON
    path can transparently pick up the cached value across sac
    invocations without changing the reader.
    """
    return credentials_path.parent / "usage.json"


def _read_per_account_cache(path: Path) -> dict[str, Any] | None:
    """Per-account cache reader; same 5-min TTL as the shared cache."""
    data = _load_json(path)
    if data is None:
        return None
    fetched_at_str = data.get("fetched_at") or data.get("as_of")
    if not isinstance(fetched_at_str, str):
        return None
    # stx-allow: fallback (reason: cache file may contain a malformed timestamp; returning None forces a fresh API fetch)
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


def _write_per_account_cache(path: Path, result: dict[str, Any]) -> None:
    """Write per-account usage cache atomically; never raises."""
    # stx-allow: fallback (reason: per-account cache file may be on a read-only filesystem or disk-full; cache is a performance optimisation and callers are unaffected)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = Path(str(path) + ".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)
        tmp.rename(path)
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        pass


def fetch_usage_for_credentials(
    credentials_path: Path,
    *,
    opener=None,
) -> dict[str, Any]:
    """Fetch usage data using the OAuth credentials at ``credentials_path``.

    Per-account variant of :func:`fetch_usage`: reads access/refresh tokens
    from ``credentials_path`` (NOT ``~/.claude/.credentials.json``),
    refreshes on expiry by atomically writing back to that same file,
    fetches the OAuth usage API, and caches the result at
    ``credentials_path.parent / "usage.json"`` (the same file
    ``_state.account_store.read_account_usage_cache`` reads, so writers
    + readers are symmetric).

    The cache is intentionally keyed per-account by the file location:
    one stored account's cache never masks another's. The TTL matches
    :func:`fetch_usage` (5 min) so repeated ``sac account list`` calls
    don't hammer the API.

    Args:
        credentials_path: Path to the per-account ``.credentials.json``.
        opener: Optional injection seam for tests.

    Returns:
        A dict with the same keys as :func:`fetch_usage` plus an extra
        ``as_of`` (ISO timestamp). Never raises; ``error`` is populated
        on any failure path.
    """
    creds = Path(credentials_path)

    def _err(msg: str) -> dict[str, Any]:
        r = dict(_EMPTY_RESULT)
        now = _iso_now()
        r["fetched_at"] = now
        r["as_of"] = now
        r["error"] = msg
        return r

    # --- cache check (per-account) -----------------------------------------
    cache_path = _per_account_cache_path(creds)
    cached = _read_per_account_cache(cache_path)
    if cached is not None:
        return cached

    # --- read tokens (never returned) --------------------------------------
    access_token, refresh_token, client_id, expires_at_ms = _read_tokens_at(creds)
    if not access_token:
        return _err(f"No access token in {creds}")

    # --- refresh if expired -------------------------------------------------
    if _is_token_expired(expires_at_ms):
        if refresh_token and client_id:
            new_token = _refresh_access_token_at(
                creds, refresh_token, client_id, opener=opener
            )
            if new_token:
                access_token = new_token
            # If refresh failed, try with the old token anyway.

    # --- API call -----------------------------------------------------------
    payload: dict[str, Any] | None = None
    # stx-allow: fallback (reason: network errors or unexpected exceptions from the usage API are caught and surfaced as an error dict rather than an unhandled exception)
    try:
        payload = _fetch_from_api(access_token, opener=opener)
    except (
        urllib.error.HTTPError
    ) as exc:  # stx-allow: fallback (reason: expected failure — see inline comment)
        if exc.code == 401 and refresh_token and client_id:
            # Try refresh once on 401.
            new_token = _refresh_access_token_at(
                creds, refresh_token, client_id, opener=opener
            )
            if new_token:
                access_token = new_token
                # stx-allow: fallback (reason: second API attempt after token refresh may still fail due to network issues; pass lets the 401 handler return an error dict)
                try:
                    payload = _fetch_from_api(access_token, opener=opener)
                except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
                    pass
        if payload is None:
            return _err(f"HTTP {exc.code} from usage API; refresh attempted")
    except Exception as exc:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        return _err(f"Network error: {exc}")

    if payload is None:
        return _err("Failed to fetch or parse usage API response")

    # --- build result -------------------------------------------------------
    result: dict[str, Any] = dict(_EMPTY_RESULT)
    result.update(_parse_windows(payload))
    now = _iso_now()
    result["fetched_at"] = now
    result["as_of"] = now
    result["from_cache"] = False
    result["error"] = None

    # Security guard — must run before cache write.
    try:
        _check_no_token_leak(result)
    except (
        RuntimeError
    ) as exc:  # stx-allow: fallback (reason: runtime state error — handled gracefully)
        return _err(str(exc))

    _write_per_account_cache(cache_path, result)
    return result


# ---------------------------------------------------------------------------
# Headless OAuth token refresh — used by `sac accounts refresh`
# ---------------------------------------------------------------------------


def refresh_account_credentials(
    credentials_path: Path,
    *,
    opener=None,
) -> dict[str, Any]:
    """Refresh the OAuth access token for the credentials at ``credentials_path``.

    Calls ``_refresh_access_token_at`` (which does the POST to the token
    endpoint + atomic write-back to the SAME file) and returns a structured
    result the CLI can render without ever surfacing token values.

    Args:
        credentials_path: Path to the per-account ``.credentials.json``
            (typically ``~/.scitex/agent-container/accounts/<name>/.credentials.json``).
        opener: Optional injection seam for tests.

    Returns:
        Dict with keys::

            success      : bool — True iff a new access_token was minted.
            expires_at   : ISO-8601 string of the new token's expiry, or None.
            error        : str  — reason for failure, or None on success.
            credentials_path : str — echo of the input path (for `--all` rendering).

        Never raises. Token values are NEVER included.
    """
    creds = Path(credentials_path)
    out: dict[str, Any] = {
        "success": False,
        "expires_at": None,
        "error": None,
        "credentials_path": str(creds),
    }

    if not creds.is_file():
        out["error"] = f"credentials file not found: {creds}"
        return out

    _, refresh_token, client_id, _ = _read_tokens_at(creds)
    if not refresh_token:
        out["error"] = "no refresh_token in credentials — needs `claude /login`"
        return out
    if not client_id:
        out["error"] = "no clientId in credentials — needs `claude /login`"
        return out

    new_access = _refresh_access_token_at(
        creds, refresh_token, client_id, opener=opener
    )
    if not new_access:
        out["error"] = (
            "refresh endpoint rejected the refresh_token — needs `claude /login`"
        )
        return out

    # Read back the freshly-written expiry; the token value itself is
    # intentionally NOT touched (tokens never leave this module).
    # stx-allow: fallback (reason: post-write read is best-effort; if the just-written file is unreadable the refresh still succeeded in memory)
    try:
        data = _load_json(creds) or {}
        oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
        expires_at_ms = (
            oauth.get("expiresAt") if isinstance(oauth, dict) else None
        )
    except Exception:  # stx-allow: fallback (reason: catch-all safety net — see inline comment for context)
        expires_at_ms = None

    if isinstance(expires_at_ms, int):
        out["expires_at"] = datetime.fromtimestamp(
            expires_at_ms / 1000, tz=timezone.utc
        ).isoformat()

    out["success"] = True
    return out

