"""Tests for scitex_agent_container._account.credentials.read_credentials_metadata."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from scitex_agent_container._account.credentials import (
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
# 1. Happy path — every safe field is mapped through.
# ---------------------------------------------------------------------------


_HAPPY_CLAUDE_JSON = {
    "oauthAccount": {
        "accountUuid": "uuid-123",
        "emailAddress": "beta@example.com",
        "organizationUuid": "org-uuid",
        "organizationName": "beta@example.com's Organization",
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
}

_HAPPY_CREDENTIALS_JSON = {
    "claudeAiOauth": {
        "accessToken": "sk-ant-SECRET",
        "refreshToken": "REFRESH-SECRET",
        "expiresAt": 9_999_999_999,
        "scopes": ["user:inference"],
        "subscriptionType": "max",
        "rateLimitTier": "default_claude_max_20x",
    }
}

_HAPPY_SETTINGS_JSON = {
    "permissions": {"allow": []},
    "statusLine": {"type": "command", "command": "claude-hud"},
    "enabledPlugins": ["hud"],
}


@pytest.fixture
def happy_path_result(tmp_path: Path) -> dict:
    # Arrange
    _write_claude_json(tmp_path, _HAPPY_CLAUDE_JSON)
    _write_credentials_json(tmp_path, _HAPPY_CREDENTIALS_JSON)
    _write_settings_json(tmp_path, _HAPPY_SETTINGS_JSON)
    # Act
    return read_credentials_metadata(home=tmp_path)


@pytest.mark.parametrize(
    "key,expected",
    [
        ("email_address", "beta@example.com"),
        ("organization_name", "beta@example.com's Organization"),
        ("display_name", "Yusuke"),
        ("billing_type", "stripe_subscription"),
        ("subscription_created_at", "2025-05-30T19:59:34Z"),
        ("has_available_subscription", True),
        ("cached_extra_usage_disabled_reason", "out_of_credits"),
        ("num_startups", 42),
        ("subscription_type", "max"),
        ("rate_limit_tier", "default_claude_max_20x"),
        ("status_line_command", "claude-hud"),
        ("enabled_plugins", ["hud"]),
    ],
)
def test_happy_path_field_value_matches_expected(
    happy_path_result: dict, key: str, expected: object
) -> None:
    # Arrange
    # (fixture wires inputs and computes result)
    # Act
    actual = happy_path_result[key]
    # Assert
    assert actual == expected


# ---------------------------------------------------------------------------
# 2. Token-leak regression — credentials.json secrets must never surface.
# ---------------------------------------------------------------------------


_LEAK_CLAUDE_JSON = {
    "oauthAccount": {
        "emailAddress": "u@example.com",
        # Intentionally polluted extra keys that must NOT be copied.
        "accessToken": "sk-ant-FAKE",
        "refreshToken": "REFRESH-FAKE",
    },
    "apiKey": "sk-ant-should-not-appear",
}

_LEAK_CREDENTIALS_JSON = {
    "claudeAiOauth": {
        "accessToken": "sk-ant-REAL-SECRET",
        "refreshToken": "REFRESH-REAL",
        "expiresAt": 9_999_999_999,
        "subscriptionType": "max",
        "rateLimitTier": "default",
    }
}


@pytest.fixture
def leak_guard_result(tmp_path: Path) -> dict:
    # Arrange
    _write_claude_json(tmp_path, _LEAK_CLAUDE_JSON)
    _write_credentials_json(tmp_path, _LEAK_CREDENTIALS_JSON)
    # Act
    return read_credentials_metadata(home=tmp_path)


@pytest.mark.parametrize("key", sorted(_all_safe_keys()))
def test_token_leak_result_contains_every_safe_key(
    leak_guard_result: dict, key: str
) -> None:
    # Arrange
    # (fixture provides result)
    # Act
    keys = leak_guard_result.keys()
    # Assert
    assert key in keys


@pytest.mark.parametrize("needle", ["sk-ant", "bearer ", "accesstoken", "refreshtoken"])
def test_token_leak_known_needle_absent_from_result(
    leak_guard_result: dict, needle: str
) -> None:
    # Arrange
    blob = json.dumps(leak_guard_result).lower()
    # Act
    leaked = needle in blob
    # Assert
    assert not leaked, f"leaked {needle!r} in {leak_guard_result}"


@pytest.mark.parametrize("needle", sorted(_FORBIDDEN_SUBSTRINGS))
def test_token_leak_forbidden_substring_absent_from_result(
    leak_guard_result: dict, needle: str
) -> None:
    # Arrange
    blob = json.dumps(leak_guard_result).lower()
    # Act
    leaked = needle in blob
    # Assert
    assert not leaked, f"leaked {needle!r} in {leak_guard_result}"


def test_token_leak_subscription_type_still_surfaced(
    leak_guard_result: dict,
) -> None:
    # Arrange
    # (fixture provides result)
    # Act
    value = leak_guard_result["subscription_type"]
    # Assert
    assert value == "max"


def test_token_leak_rate_limit_tier_still_surfaced(
    leak_guard_result: dict,
) -> None:
    # Arrange
    # (fixture provides result)
    # Act
    value = leak_guard_result["rate_limit_tier"]
    # Assert
    assert value == "default"


# ---------------------------------------------------------------------------
# 3. Missing-file tolerance — empty $HOME yields a fully-shaped dict.
# ---------------------------------------------------------------------------


@pytest.fixture
def empty_home_result(tmp_path: Path) -> dict:
    # Arrange
    # tmp_path is completely empty — no claude files at all.
    # Act
    return read_credentials_metadata(home=tmp_path)


def test_missing_files_returns_dict(empty_home_result: dict) -> None:
    # Arrange
    # (fixture provides result)
    # Act
    actual_type = type(empty_home_result)
    # Assert
    assert isinstance(empty_home_result, dict) and actual_type is dict


@pytest.mark.parametrize("key", sorted(_all_safe_keys()))
def test_missing_files_every_safe_key_is_present(
    empty_home_result: dict, key: str
) -> None:
    # Arrange
    # (fixture provides result)
    # Act
    present = key in empty_home_result
    # Assert
    assert present


@pytest.mark.parametrize(
    "key", sorted(k for k in _all_safe_keys() if k != "installed_plugins")
)
def test_missing_files_scalar_safe_keys_default_to_none(
    empty_home_result: dict, key: str
) -> None:
    # Arrange
    # (fixture provides result)
    # Act
    value = empty_home_result[key]
    # Assert
    assert value is None


def test_missing_files_installed_plugins_defaults_to_empty_list(
    empty_home_result: dict,
) -> None:
    # Arrange
    # (fixture provides result)
    # Act
    value = empty_home_result["installed_plugins"]
    # Assert
    assert value == []


# ---------------------------------------------------------------------------
# 4. Partial-file tolerance — claude.json without oauthAccount block.
# ---------------------------------------------------------------------------


@pytest.fixture
def partial_no_oauth_result(tmp_path: Path) -> dict:
    # Arrange
    _write_claude_json(
        tmp_path,
        {
            "numStartups": 5,
            "installMethod": "npm",
            # no oauthAccount key
        },
    )
    # Act
    return read_credentials_metadata(home=tmp_path)


@pytest.mark.parametrize(
    "key,expected",
    [
        ("num_startups", 5),
        ("install_method", "npm"),
        # oauthAccount-derived fields are None.
        ("email_address", None),
        ("organization_name", None),
        ("display_name", None),
        # credentials-derived fields are None.
        ("subscription_type", None),
        ("rate_limit_tier", None),
    ],
)
def test_partial_no_oauth_field_value_matches_expected(
    partial_no_oauth_result: dict, key: str, expected: object
) -> None:
    # Arrange
    # (fixture provides result)
    # Act
    actual = partial_no_oauth_result[key]
    # Assert
    assert actual == expected


# ---------------------------------------------------------------------------
# 5. CLI integration: `agents accounts list --json` contains claude_account
#
# Account info moved out of `sac agents status` (mixed-noun output was
# noisy); the active Claude credentials are now surfaced by the
# dedicated `sac accounts list` command.
# ---------------------------------------------------------------------------


@pytest.fixture
def accounts_list_json_payload(tmp_path: Path) -> tuple[dict, str]:
    # Arrange
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
    }
    # Preserve PYTHONPATH so the subprocess can import the package when it is
    # provided via PYTHONPATH (the CI SIF layers a ``uv pip install --target``
    # onto a read-only venv) rather than installed into the interpreter's own
    # site-packages (the bare-runner case). Without this the in-SIF release test
    # fails with ``No module named scitex_agent_container``.
    if os.environ.get("PYTHONPATH"):
        env["PYTHONPATH"] = os.environ["PYTHONPATH"]
    # Act
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "scitex_agent_container",
            "accounts",
            "list",
            "--json",
        ],
        capture_output=True,
        text=True,
        env=env,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout)
    return payload, result.stdout


def test_accounts_list_json_payload_has_active_key(
    accounts_list_json_payload: tuple[dict, str],
) -> None:
    # Arrange
    payload, _ = accounts_list_json_payload
    # Act
    keys = payload.keys()
    # Assert
    assert "active" in keys


@pytest.mark.parametrize(
    "key,expected",
    [
        ("email_address", "test@example.com"),
        ("subscription_type", "max"),
        ("rate_limit_tier", "default_claude_max_20x"),
    ],
)
def test_accounts_list_json_active_field_matches_expected(
    accounts_list_json_payload: tuple[dict, str], key: str, expected: str
) -> None:
    # Arrange
    payload, _ = accounts_list_json_payload
    acct = payload["active"]
    # Act
    actual = acct[key]
    # Assert
    assert actual == expected


@pytest.mark.parametrize(
    "needle", ["sk-ant", "accesstoken", "refreshtoken", "claudeaioauth"]
)
def test_accounts_list_json_stdout_contains_no_token_material(
    accounts_list_json_payload: tuple[dict, str], needle: str
) -> None:
    # Arrange
    _, stdout = accounts_list_json_payload
    blob = stdout.lower()
    # Act
    leaked = needle in blob
    # Assert
    assert not leaked


# ---------------------------------------------------------------------------
# 6. Plan-label derivation
# ---------------------------------------------------------------------------


@pytest.fixture
def max_20x_result(tmp_path: Path) -> dict:
    # Arrange
    _write_credentials_json(
        tmp_path,
        {
            "claudeAiOauth": {
                "accessToken": "sk-ant-fake",
                "subscriptionType": "max",
                "rateLimitTier": "default_claude_max_20x",
                "expiresAt": 1_776_451_091_741,
            }
        },
    )
    # Act
    return read_credentials_metadata(home=tmp_path)


def test_plan_label_max_20x_tier_maps_to_friendly_label(
    max_20x_result: dict,
) -> None:
    # Arrange
    # (fixture provides result)
    # Act
    label = max_20x_result["plan_label"]
    # Assert
    assert label == "Max 20x"


def test_plan_label_max_20x_passes_expires_at_through(
    max_20x_result: dict,
) -> None:
    # Arrange
    # (fixture provides result)
    # Act
    value = max_20x_result["oauth_expires_at"]
    # Assert
    assert value == 1_776_451_091_741


def test_plan_label_falls_back_to_subscription_type_when_tier_unknown(
    tmp_path: Path,
) -> None:
    # Arrange
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
    # Act
    result = read_credentials_metadata(home=tmp_path)
    # Assert
    # rate_limit_tier is not in _PLAN_LABELS -> fall through to subscription.
    assert result["plan_label"] == "Pro"


@pytest.fixture
def unknown_plan_result(tmp_path: Path) -> dict:
    # Arrange
    _write_credentials_json(
        tmp_path,
        {
            "claudeAiOauth": {
                "subscriptionType": "enterprise_super_mega",
                "rateLimitTier": "default_claude_enterprise_mega",
            }
        },
    )
    # Act
    return read_credentials_metadata(home=tmp_path)


def test_plan_label_unknown_plan_label_is_none(unknown_plan_result: dict) -> None:
    # Arrange
    # (fixture provides result)
    # Act
    label = unknown_plan_result["plan_label"]
    # Assert
    assert label is None


def test_plan_label_unknown_plan_exposes_raw_rate_limit_tier(
    unknown_plan_result: dict,
) -> None:
    # Arrange
    # (fixture provides result)
    # Act
    value = unknown_plan_result["rate_limit_tier"]
    # Assert
    # Raw fields still exposed so the dashboard can show the unknown tier.
    assert value == "default_claude_enterprise_mega"


def test_plan_label_unknown_plan_exposes_raw_subscription_type(
    unknown_plan_result: dict,
) -> None:
    # Arrange
    # (fixture provides result)
    # Act
    value = unknown_plan_result["subscription_type"]
    # Assert
    assert value == "enterprise_super_mega"


@pytest.mark.parametrize(
    "rate_limit_tier,subscription_type,expected",
    [
        ("default_claude_max_20x", None, "Max 20x"),
        ("default_claude_max_5x", "max", "Max 5x"),
        (None, "max", "Max"),
        (None, None, None),
        ("unknown", "unknown", None),
    ],
)
def test_derive_plan_label_pure_function_maps_inputs_to_expected(
    rate_limit_tier: str | None,
    subscription_type: str | None,
    expected: str | None,
) -> None:
    # Arrange
    # (inputs come from parametrize)
    # Act
    actual = _derive_plan_label(rate_limit_tier, subscription_type)
    # Assert
    assert actual == expected


# ---------------------------------------------------------------------------
# 7. Installed plugins listing
# ---------------------------------------------------------------------------


@pytest.fixture
def installed_plugins_two_distinct(tmp_path: Path) -> list[dict]:
    # Arrange
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
    # Act
    result = read_credentials_metadata(home=tmp_path)
    return result["installed_plugins"]


def test_installed_plugins_parsed_returns_list(
    installed_plugins_two_distinct: list[dict],
) -> None:
    # Arrange
    plugins = installed_plugins_two_distinct
    # Act
    is_list = isinstance(plugins, list)
    # Assert
    assert is_list


def test_installed_plugins_parsed_names_match_expected_pair(
    installed_plugins_two_distinct: list[dict],
) -> None:
    # Arrange
    plugins = installed_plugins_two_distinct
    # Act
    names = sorted(p["name"] for p in plugins)
    # Assert
    assert names == [
        "claude-hud@claude-hud",
        "telegram@claude-plugins-official",
    ]


@pytest.mark.parametrize(
    "field,expected",
    [
        ("version", "0.0.10"),
        ("scope", "user"),
        ("installed_at", "2026-03-18T00:00:26.724Z"),
    ],
)
def test_installed_plugins_parsed_hud_entry_field_matches(
    installed_plugins_two_distinct: list[dict], field: str, expected: str
) -> None:
    # Arrange
    hud = next(
        p
        for p in installed_plugins_two_distinct
        if p["name"] == "claude-hud@claude-hud"
    )
    # Act
    actual = hud[field]
    # Assert
    assert actual == expected


@pytest.fixture
def multi_scope_plugins(tmp_path: Path) -> list[dict]:
    # Arrange
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
    # Act
    result = read_credentials_metadata(home=tmp_path)
    return result["installed_plugins"]


def test_installed_plugins_multi_scope_yields_one_entry_per_scope(
    multi_scope_plugins: list[dict],
) -> None:
    # Arrange
    plugins = multi_scope_plugins
    # Act
    count = len(plugins)
    # Assert
    assert count == 2


def test_installed_plugins_multi_scope_exposes_both_scope_labels(
    multi_scope_plugins: list[dict],
) -> None:
    # Arrange
    plugins = multi_scope_plugins
    # Act
    scopes = sorted(p["scope"] for p in plugins)
    # Assert
    assert scopes == ["local", "user"]


def test_installed_plugins_malformed_json_returns_empty_list(
    tmp_path: Path,
) -> None:
    # Arrange
    plugins_dir = tmp_path / ".claude" / "plugins"
    plugins_dir.mkdir(parents=True)
    (plugins_dir / "installed_plugins.json").write_text("not json {")
    # Act
    result = read_credentials_metadata(home=tmp_path)
    # Assert
    assert result["installed_plugins"] == []


# ---------------------------------------------------------------------------
# 8. expires_at does NOT trip the secret guard
# ---------------------------------------------------------------------------


@pytest.fixture
def expires_at_secret_guard_result(tmp_path: Path) -> dict:
    """Regression: oauth_expires_at is an integer and must not be
    classified as a secret despite living next to accessToken."""
    # Arrange
    _write_credentials_json(
        tmp_path,
        {
            "claudeAiOauth": {
                "accessToken": "sk-ant-SECRET",
                "refreshToken": "REFRESH",
                "expiresAt": 1_776_451_091_741,
                "subscriptionType": "max",
                "rateLimitTier": "default_claude_max_20x",
            }
        },
    )
    # Act
    return read_credentials_metadata(home=tmp_path)


def test_expires_at_int_value_surfaces_unchanged(
    expires_at_secret_guard_result: dict,
) -> None:
    # Arrange
    # (fixture provides result)
    # Act
    value = expires_at_secret_guard_result["oauth_expires_at"]
    # Assert
    assert value == 1_776_451_091_741


def test_expires_at_does_not_leak_token_material(
    expires_at_secret_guard_result: dict,
) -> None:
    # Arrange
    blob = json.dumps(expires_at_secret_guard_result).lower()
    # Act
    leaked = "sk-ant" in blob
    # Assert
    # And the secret guard still would have caught a leaked token.
    assert not leaked
