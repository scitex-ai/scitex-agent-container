"""Safe extraction of Claude Code credential/settings metadata.

See ``docs/credentials.md`` for the full specification of which files
are read and which fields are considered safe to surface.

Design rules:

1. **Whitelist only.** This module never blacklists. Only the fields
   explicitly listed below are copied out of the source files.
2. **Token material is forbidden.** The file ``~/.claude/.credentials.json``
   contains OAuth tokens. Only three non-secret fields are ever read
   from it: ``subscriptionType``, ``rateLimitTier``, and ``expiresAt``
   (a unix-ms integer deadline, used to detect token rotations).
   ``accessToken``, ``refreshToken``, and ``scopes`` are never parsed.
3. **Post-extraction guard.** After extraction, the result dict is
   scanned for any key or stringified value containing a secret-looking
   substring. A match raises ``RuntimeError``.
4. Pure stdlib. No new dependencies.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

# Fields read from ~/.claude.json -> oauthAccount
_OAUTH_ACCOUNT_FIELDS = {
    "accountUuid": "account_uuid",
    "emailAddress": "email_address",
    "organizationUuid": "organization_uuid",
    "organizationName": "organization_name",
    "billingType": "billing_type",
    "accountCreatedAt": "account_created_at",
    "subscriptionCreatedAt": "subscription_created_at",
    "hasExtraUsageEnabled": "has_extra_usage_enabled",
    "displayName": "display_name",
    "organizationRole": "organization_role",
}

# Fields read from ~/.claude.json top level
_TOP_LEVEL_FIELDS = {
    "hasAvailableSubscription": "has_available_subscription",
    "cachedExtraUsageDisabledReason": "cached_extra_usage_disabled_reason",
    "numStartups": "num_startups",
    "installMethod": "install_method",
    "claudeCodeFirstTokenDate": "claude_code_first_token_date",
    "firstStartTime": "first_start_time",
    "hasCompletedOnboarding": "has_completed_onboarding",
}

# Fields read from ~/.claude/.credentials.json.
# Whitelist only. accessToken/refreshToken/scopes are NEVER read.
# ``expiresAt`` is a unix-ms integer (token rotation deadline); it is
# not a secret and is required for the auth-rotation tracker
# (`oauth_expires_at` changes → token has rotated → emit one NDJSON
# line keyed on email so the dashboard can show "rotated N times").
_CREDENTIALS_SAFE_FIELDS = {
    "subscriptionType": "subscription_type",
    "rateLimitTier": "rate_limit_tier",
    "expiresAt": "oauth_expires_at",
}

# Human-friendly label derived from rateLimitTier (fallback to
# subscriptionType). Dashboard should prefer ``plan_label``; raw
# values stay exposed so future tiers surface as ``plan_label=None``
# rather than silently being bucketed wrong.
_PLAN_LABELS = {
    "default_claude_max_20x": "Max 20x",
    "default_claude_max_5x": "Max 5x",
    "default_claude_pro": "Pro",
    "default_claude_free": "Free",
}
_SUBSCRIPTION_FALLBACK_LABELS = {
    "max": "Max",
    "pro": "Pro",
    "free": "Free",
}

# Fields read from ~/.claude/settings.json
_SETTINGS_FIELDS = {
    "statusLine": "status_line_command",
    "enabledPlugins": "enabled_plugins",
}

# Derived (not read directly from any file). Always present in the
# output dict so callers have a stable schema.
_DERIVED_FIELDS = (
    "plan_label",
    "installed_plugins",
)

# Substrings that MUST NOT appear in any returned key or stringified value.
_FORBIDDEN_SUBSTRINGS = (
    "sk-ant-",
    "bearer ",
    "accesstoken",
    "refreshtoken",
    "claudeaioauth",
    "apikey",
    "secret",
)


def _all_safe_keys() -> list[str]:
    """Return every key this extractor is permitted to emit."""
    keys: list[str] = []
    keys.extend(_OAUTH_ACCOUNT_FIELDS.values())
    keys.extend(_TOP_LEVEL_FIELDS.values())
    keys.extend(_CREDENTIALS_SAFE_FIELDS.values())
    keys.extend(_SETTINGS_FIELDS.values())
    keys.extend(_DERIVED_FIELDS)
    return keys


def _derive_plan_label(
    rate_limit_tier: str | None,
    subscription_type: str | None,
) -> str | None:
    """Map raw tier/subscription strings to a human plan label.

    Returns ``None`` if neither input yields a known tier — the
    dashboard should surface the raw values as a fallback so unknown
    plans don't get silently bucketed under "Free".
    """
    if rate_limit_tier and rate_limit_tier in _PLAN_LABELS:
        return _PLAN_LABELS[rate_limit_tier]
    if subscription_type and subscription_type in _SUBSCRIPTION_FALLBACK_LABELS:
        return _SUBSCRIPTION_FALLBACK_LABELS[subscription_type]
    return None


def _read_installed_plugins(home: Path) -> list[dict[str, Any]]:
    """Return a flat list of installed Claude Code plugins.

    Parses ``~/.claude/plugins/installed_plugins.json`` which Claude
    Code writes when plugins are installed via ``/plugin install``.
    One entry per (plugin, scope) tuple — the same plugin can appear
    twice (user-scope + local-scope). Empty list if the file does not
    exist or is malformed.
    """
    path = home / ".claude" / "plugins" / "installed_plugins.json"
    data = _load_json(path)
    if data is None:
        return []
    plugins_raw = data.get("plugins")
    if not isinstance(plugins_raw, dict):
        return []
    out: list[dict[str, Any]] = []
    for pname, entries in plugins_raw.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            out.append(
                {
                    "name": pname,
                    "version": entry.get("version"),
                    "scope": entry.get("scope"),
                    "installed_at": entry.get("installedAt"),
                    "last_updated": entry.get("lastUpdated"),
                    "project_path": entry.get("projectPath"),
                }
            )
    return out


def _load_json(path: Path) -> dict[str, Any] | None:
    """Load a JSON file; return ``None`` if missing or unparseable."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            data = json.load(fh)
    except FileNotFoundError:  # stx-allow: fallback (reason: file may not exist on first use)
        return None
    except (OSError, json.JSONDecodeError):  # stx-allow: fallback (reason: malformed JSON tolerated)
        return None
    if not isinstance(data, dict):
        return None
    return data


def _check_no_secrets(result: dict[str, Any]) -> None:
    """Raise if any key or value contains a forbidden substring."""
    for key, value in result.items():
        key_l = key.lower()
        for needle in _FORBIDDEN_SUBSTRINGS:
            if needle in key_l:
                raise RuntimeError(
                    f"credentials extractor leaked forbidden key: {key!r}"
                )
        if value is None:
            continue
        val_l = str(value).lower()
        for needle in _FORBIDDEN_SUBSTRINGS:
            if needle in val_l:
                raise RuntimeError(
                    f"credentials extractor leaked forbidden value under key {key!r}"
                )


def read_credentials_metadata(home: Path | None = None) -> dict[str, Any]:
    """Return safe metadata from the Claude Code credential/settings files.

    Reads ``~/.claude.json``, ``~/.claude/settings.json``, and
    ``~/.claude/.credentials.json``. From ``.credentials.json`` only the
    non-secret ``subscriptionType`` and ``rateLimitTier`` fields are
    read; tokens and any other field in that file are NEVER emitted.

    Missing files are tolerated: corresponding fields are ``None`` and
    no exception is raised.

    The shape of the returned dict is fixed — every key listed in
    ``docs/credentials.md`` is always present, with ``None`` if unknown.

    Args:
        home: Optional override for the user's home directory. Defaults
            to ``Path.home()``. Used for tests.

    Returns:
        Flat dict of safe metadata fields.

    Raises:
        RuntimeError: If the post-extraction guard detects a leak.
    """
    home = home or Path.home()

    # Pre-populate every safe field with None so callers get a stable shape.
    result: dict[str, Any] = {k: None for k in _all_safe_keys()}

    # --- ~/.claude.json -----------------------------------------------------
    claude_json = _load_json(home / ".claude.json")
    if claude_json is not None:
        oauth = claude_json.get("oauthAccount")
        if isinstance(oauth, dict):
            for src_key, dst_key in _OAUTH_ACCOUNT_FIELDS.items():
                if src_key in oauth:
                    result[dst_key] = oauth[src_key]
        for src_key, dst_key in _TOP_LEVEL_FIELDS.items():
            if src_key in claude_json:
                result[dst_key] = claude_json[src_key]

    # --- ~/.claude/.credentials.json ---------------------------------------
    # Whitelist-only: we load the file but copy *only* the two safe fields.
    # Tokens (accessToken, refreshToken, expiresAt, scopes) are never
    # referenced by this module.
    creds_json = _load_json(home / ".claude" / ".credentials.json")
    if creds_json is not None:
        oauth = creds_json.get("claudeAiOauth")
        if isinstance(oauth, dict):
            for src_key, dst_key in _CREDENTIALS_SAFE_FIELDS.items():
                if src_key in oauth:
                    val = oauth[src_key]
                    # Defensive: refuse to copy anything non-primitive
                    if isinstance(val, (str, int, float, bool)):
                        result[dst_key] = val

    # --- ~/.claude/settings.json -------------------------------------------
    settings_json = _load_json(home / ".claude" / "settings.json")
    if settings_json is not None:
        status_line = settings_json.get("statusLine")
        if isinstance(status_line, dict):
            # statusLine is typically {"type": "command", "command": "..."}.
            cmd = status_line.get("command")
            if isinstance(cmd, str):
                result["status_line_command"] = cmd
        elif isinstance(status_line, str):
            result["status_line_command"] = status_line
        enabled = settings_json.get("enabledPlugins")
        if isinstance(enabled, (list, dict)):
            result["enabled_plugins"] = enabled

    # --- Derived fields ----------------------------------------------------
    result["plan_label"] = _derive_plan_label(
        rate_limit_tier=(
            result["rate_limit_tier"]
            if isinstance(result["rate_limit_tier"], str)
            else None
        ),
        subscription_type=(
            result["subscription_type"]
            if isinstance(result["subscription_type"], str)
            else None
        ),
    )
    result["installed_plugins"] = _read_installed_plugins(home)

    # Post-extraction guard: must not contain any secret-looking material.
    _check_no_secrets(result)
    return result
