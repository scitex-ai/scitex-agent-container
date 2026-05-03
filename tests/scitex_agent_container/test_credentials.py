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
    _derive_plan_label,
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
    # Every safe key present; scalars None, installed_plugins defaults
    # to [] so consumers can always iterate without a None check.
    for key in _all_safe_keys():
        assert key in result
        if key == "installed_plugins":
            assert result[key] == []
        else:
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
        [sys.executable, "-m", "scitex_agent_container", "agent", "status", "--json"],
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


# ---------------------------------------------------------------------------
# 6. Plan-label derivation
# ---------------------------------------------------------------------------


def test_plan_label_from_rate_limit_tier(tmp_path: Path) -> None:
    _write_credentials_json(
        tmp_path,
        {
            "claudeAiOauth": {
                "accessToken": "sk-ant-fake",
                "subscriptionType": "max",
                "rateLimitTier": "default_claude_max_20x",
                "expiresAt": 1776451091741,
            }
        },
    )
    result = read_credentials_metadata(home=tmp_path)
    assert result["plan_label"] == "Max 20x"
    assert result["oauth_expires_at"] == 1776451091741


def test_plan_label_falls_back_to_subscription_type(tmp_path: Path) -> None:
    _write_credentials_json(
        tmp_path,
        {
            "claudeAiOauth": {
                "accessToken": "sk-ant-fake",
                "subscriptionType": "pro",
                "rateLimitTier": "unseen_future_tier",
                "expiresAt": 1,
            }
        },
    )
    result = read_credentials_metadata(home=tmp_path)
    # rate_limit_tier is not in _PLAN_LABELS -> fall through to subscription.
    assert result["plan_label"] == "Pro"


def test_plan_label_unknown_plan_returns_none(tmp_path: Path) -> None:
    _write_credentials_json(
        tmp_path,
        {
            "claudeAiOauth": {
                "subscriptionType": "enterprise_super_mega",
                "rateLimitTier": "default_claude_enterprise_mega",
            }
        },
    )
    result = read_credentials_metadata(home=tmp_path)
    assert result["plan_label"] is None
    # Raw fields still exposed so the dashboard can show the unknown tier.
    assert result["rate_limit_tier"] == "default_claude_enterprise_mega"
    assert result["subscription_type"] == "enterprise_super_mega"


def test_derive_plan_label_pure_function() -> None:
    assert _derive_plan_label("default_claude_max_20x", None) == "Max 20x"
    assert _derive_plan_label("default_claude_max_5x", "max") == "Max 5x"
    assert _derive_plan_label(None, "max") == "Max"
    assert _derive_plan_label(None, None) is None
    assert _derive_plan_label("unknown", "unknown") is None


# ---------------------------------------------------------------------------
# 7. Installed plugins listing
# ---------------------------------------------------------------------------


def test_installed_plugins_parsed(tmp_path: Path) -> None:
    plugins_dir = tmp_path / ".claude" / "plugins"
    plugins_dir.mkdir(parents=True)
    (plugins_dir / "installed_plugins.json").write_text(
        json.dumps(
            {
                "version": 2,
                "plugins": {
                    "claude-hud@claude-hud": [
                        {
                            "scope": "user",
                            "version": "0.0.10",
                            "installedAt": "2026-03-18T00:00:26.724Z",
                            "lastUpdated": "2026-04-10T08:25:12.944Z",
                            "installPath": "/path/to/plugin",
                        }
                    ],
                    "telegram@claude-plugins-official": [
                        {
                            "scope": "local",
                            "version": "0.0.4",
                            "installedAt": "2026-04-10T08:25:12.944Z",
                            "projectPath": "/home/u/proj/foo",
                        }
                    ],
                },
            }
        )
    )
    result = read_credentials_metadata(home=tmp_path)
    plugins = result["installed_plugins"]
    assert isinstance(plugins, list)
    names = sorted(p["name"] for p in plugins)
    assert names == [
        "claude-hud@claude-hud",
        "telegram@claude-plugins-official",
    ]
    hud = next(p for p in plugins if p["name"] == "claude-hud@claude-hud")
    assert hud["version"] == "0.0.10"
    assert hud["scope"] == "user"
    assert hud["installed_at"] == "2026-03-18T00:00:26.724Z"


def test_installed_plugins_multi_scope_same_plugin(tmp_path: Path) -> None:
    # One plugin, two scopes (user + local) -> two entries.
    plugins_dir = tmp_path / ".claude" / "plugins"
    plugins_dir.mkdir(parents=True)
    (plugins_dir / "installed_plugins.json").write_text(
        json.dumps(
            {
                "plugins": {
                    "pyright-lsp@claude-plugins-official": [
                        {"scope": "local", "version": "1.0.0"},
                        {"scope": "user", "version": "1.0.0"},
                    ]
                }
            }
        )
    )
    result = read_credentials_metadata(home=tmp_path)
    plugins = result["installed_plugins"]
    assert len(plugins) == 2
    scopes = sorted(p["scope"] for p in plugins)
    assert scopes == ["local", "user"]


def test_installed_plugins_malformed_json(tmp_path: Path) -> None:
    plugins_dir = tmp_path / ".claude" / "plugins"
    plugins_dir.mkdir(parents=True)
    (plugins_dir / "installed_plugins.json").write_text("not json {")
    result = read_credentials_metadata(home=tmp_path)
    assert result["installed_plugins"] == []


# ---------------------------------------------------------------------------
# 8. expires_at does NOT trip the secret guard
# ---------------------------------------------------------------------------


def test_expires_at_passes_secret_guard(tmp_path: Path) -> None:
    """Regression: oauth_expires_at is an integer and must not be
    classified as a secret despite living next to accessToken."""
    _write_credentials_json(
        tmp_path,
        {
            "claudeAiOauth": {
                "accessToken": "sk-ant-SECRET",
                "refreshToken": "REFRESH",
                "expiresAt": 1776451091741,
                "subscriptionType": "max",
                "rateLimitTier": "default_claude_max_20x",
            }
        },
    )
    result = read_credentials_metadata(home=tmp_path)
    assert result["oauth_expires_at"] == 1776451091741
    # And the secret guard still would have caught a leaked token.
    blob = json.dumps(result).lower()
    assert "sk-ant" not in blob
