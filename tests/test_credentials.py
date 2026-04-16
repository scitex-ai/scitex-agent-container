"""Tests for scitex_agent_container.credentials.read_credentials_metadata."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from scitex_agent_container.credentials import (
    _FORBIDDEN_SUBSTRINGS,
    _all_safe_keys,
    read_credentials_metadata,
)


def _write_claude_json(home: Path, data: dict) -> None:
    (home / ".claude.json").write_text(json.dumps(data))


def _write_credentials_json(home: Path, data: dict) -> None:
    claude_dir = home / ".claude"
    claude_dir.mkdir(exist_ok=True)
    (claude_dir / ".credentials.json").write_text(json.dumps(data))


def _write_settings_json(home: Path, data: dict) -> None:
    claude_dir = home / ".claude"
    claude_dir.mkdir(exist_ok=True)
    (claude_dir / "settings.json").write_text(json.dumps(data))


# ---------------------------------------------------------------------------
# 1. Happy path
# ---------------------------------------------------------------------------


def test_happy_path_all_fields(tmp_path: Path) -> None:
    _write_claude_json(
        tmp_path,
        {
            "oauthAccount": {
                "accountUuid": "uuid-123",
                "emailAddress": "ywata1989@gmail.com",
                "organizationUuid": "org-uuid",
                "organizationName": "ywata1989@gmail.com's Organization",
                "billingType": "stripe_subscription",
                "accountCreatedAt": "2025-05-01T00:00:00Z",
                "subscriptionCreatedAt": "2025-05-30T19:59:34Z",
                "hasExtraUsageEnabled": False,
                "displayName": "Yusuke",
                "organizationRole": "owner",
            },
            "hasAvailableSubscription": True,
            "cachedExtraUsageDisabledReason": "out_of_credits",
            "numStartups": 42,
            "installMethod": "npm",
            "claudeCodeFirstTokenDate": "2025-05-30",
            "firstStartTime": "2025-05-30T20:00:00Z",
            "hasCompletedOnboarding": True,
        },
    )
    _write_credentials_json(
        tmp_path,
        {
            "claudeAiOauth": {
                "accessToken": "sk-ant-SECRET",
                "refreshToken": "REFRESH-SECRET",
                "expiresAt": 9999999999,
                "scopes": ["user:inference"],
                "subscriptionType": "max",
                "rateLimitTier": "default_claude_max_20x",
            }
        },
    )
    _write_settings_json(
        tmp_path,
        {
            "permissions": {"allow": []},
            "statusLine": {"type": "command", "command": "claude-hud"},
            "enabledPlugins": ["hud"],
        },
    )

    result = read_credentials_metadata(home=tmp_path)

    assert result["email_address"] == "ywata1989@gmail.com"
    assert result["organization_name"] == "ywata1989@gmail.com's Organization"
    assert result["display_name"] == "Yusuke"
    assert result["billing_type"] == "stripe_subscription"
    assert result["subscription_created_at"] == "2025-05-30T19:59:34Z"
    assert result["has_available_subscription"] is True
    assert result["cached_extra_usage_disabled_reason"] == "out_of_credits"
    assert result["num_startups"] == 42
    assert result["subscription_type"] == "max"
    assert result["rate_limit_tier"] == "default_claude_max_20x"
    assert result["status_line_command"] == "claude-hud"
    assert result["enabled_plugins"] == ["hud"]


# ---------------------------------------------------------------------------
# 2. Token-leak regression
# ---------------------------------------------------------------------------


def test_no_token_leak(tmp_path: Path) -> None:
    _write_claude_json(
        tmp_path,
        {
            "oauthAccount": {
                "emailAddress": "u@example.com",
                # Intentionally polluted extra keys that must NOT be copied.
                "accessToken": "sk-ant-FAKE",
                "refreshToken": "REFRESH-FAKE",
            },
            "apiKey": "sk-ant-should-not-appear",
        },
    )
    _write_credentials_json(
        tmp_path,
        {
            "claudeAiOauth": {
                "accessToken": "sk-ant-REAL-SECRET",
                "refreshToken": "REFRESH-REAL",
                "expiresAt": 9999999999,
                "subscriptionType": "max",
                "rateLimitTier": "default",
            }
        },
    )

    result = read_credentials_metadata(home=tmp_path)

    # Every safe key is present (with None when absent).
    for key in _all_safe_keys():
        assert key in result

    # Grep-style scan: no forbidden substring anywhere.
    blob = json.dumps(result).lower()
    for needle in ("sk-ant", "bearer ", "accesstoken", "refreshtoken"):
        assert needle not in blob, f"leaked {needle!r} in {result}"
    for needle in _FORBIDDEN_SUBSTRINGS:
        assert needle not in blob, f"leaked {needle!r} in {result}"

    # The two safe credential fields did come through.
    assert result["subscription_type"] == "max"
    assert result["rate_limit_tier"] == "default"


# ---------------------------------------------------------------------------
# 3. Missing-file tolerance
# ---------------------------------------------------------------------------


def test_missing_files_tolerated(tmp_path: Path) -> None:
    # tmp_path is completely empty — no claude files at all.
    result = read_credentials_metadata(home=tmp_path)
    assert isinstance(result, dict)
    # Every safe key present, all None.
    for key in _all_safe_keys():
        assert key in result
        assert result[key] is None


# ---------------------------------------------------------------------------
# 4. Partial-file tolerance
# ---------------------------------------------------------------------------


def test_partial_claude_json_no_oauth_account(tmp_path: Path) -> None:
    _write_claude_json(
        tmp_path,
        {
            "numStartups": 5,
            "installMethod": "npm",
            # no oauthAccount key
        },
    )
    result = read_credentials_metadata(home=tmp_path)
    assert result["num_startups"] == 5
    assert result["install_method"] == "npm"
    # oauthAccount-derived fields are None.
    assert result["email_address"] is None
    assert result["organization_name"] is None
    assert result["display_name"] is None
    # credentials-derived fields are None.
    assert result["subscription_type"] is None
    assert result["rate_limit_tier"] is None


# ---------------------------------------------------------------------------
# 5. CLI integration: `status --json` contains claude_account
# ---------------------------------------------------------------------------


def test_status_json_contains_claude_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir()
    _write_claude_json(
        fake_home,
        {
            "oauthAccount": {
                "emailAddress": "test@example.com",
                "organizationName": "Test Org",
                "displayName": "Tester",
                "billingType": "stripe_subscription",
                "subscriptionCreatedAt": "2025-01-01T00:00:00Z",
            },
            "hasAvailableSubscription": True,
        },
    )
    _write_credentials_json(
        fake_home,
        {
            "claudeAiOauth": {
                "accessToken": "sk-ant-nope",
                "refreshToken": "nope",
                "subscriptionType": "max",
                "rateLimitTier": "default_claude_max_20x",
            }
        },
    )

    env = {
        "HOME": str(fake_home),
        "PATH": "/usr/bin:/bin:/usr/local/bin",
        # Registry dir lives under HOME — keep it empty so no agents listed.
    }
    result = subprocess.run(
        [sys.executable, "-m", "scitex_agent_container", "status", "--json"],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    assert "claude_account" in payload
    acct = payload["claude_account"]
    assert acct["email_address"] == "test@example.com"
    assert acct["subscription_type"] == "max"
    assert acct["rate_limit_tier"] == "default_claude_max_20x"
    # Regression: no token material in the JSON output blob.
    blob = result.stdout.lower()
    for needle in ("sk-ant", "accesstoken", "refreshtoken", "claudeaioauth"):
        assert needle not in blob
