"""Tests for ``_account.agent_account.resolve_agent_account_label``.

No-mocks: every case writes real ``~/.claude.json`` /
``~/.claude/.credentials.json`` files and real ``sac accounts`` store
entries under ``tmp_path`` and resolves against them. No
``unittest.mock``, no ``monkeypatch``, no ``MagicMock`` — the resolver
reads bytes on disk, so the tests put bytes on disk.
"""

from __future__ import annotations

import json
from pathlib import Path

from scitex_agent_container._account.agent_account import (
    resolve_agent_account_label,
)
from scitex_agent_container._state.account_store import save_account

# ---------------------------------------------------------------------------
# Real credential-file fixtures (helpers write real JSON to tmp_path).
# ---------------------------------------------------------------------------


def _write_credentials(home: Path, *, expires_at: int = 9_999_999_999_000) -> None:
    """Write a real ``~/.claude/.credentials.json`` under ``home``.

    ``expires_at`` is far in the future by default so any token-expiry
    check the resolver path triggers treats it as live.
    """
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    (claude_dir / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "sk-ant-SECRET-TOKEN",
                    "refreshToken": "REFRESH-SECRET",
                    "expiresAt": expires_at,
                    "scopes": ["user:inference"],
                    "subscriptionType": "max",
                    "rateLimitTier": "default_claude_max_20x",
                }
            }
        )
    )


def _write_claude_json(home: Path, email: str | None) -> None:
    """Write ``~/.claude.json`` with (or without) an oauth email."""
    oauth = {"emailAddress": email} if email is not None else {}
    (home / ".claude.json").write_text(json.dumps({"oauthAccount": oauth}))


# ---------------------------------------------------------------------------
# 1. Env-override branch (agent brings its own credential).
# ---------------------------------------------------------------------------


def test_api_key_env_override_returns_fingerprint_label(tmp_path):
    # Arrange
    env = {"SAC_ANTHROPIC_API_KEY": "sk-ant-api03-ABCDEFGHabcd1234"}
    # Act
    label = resolve_agent_account_label(env, home=tmp_path)
    # Assert
    assert label == "apikey:…1234"


def test_oauth_token_env_override_returns_sac_env_label(tmp_path):
    # Arrange
    env = {"SAC_ANTHROPIC_API_KEY": "sk-ant-oat01-OPAQUE-BEARER"}
    # Act
    label = resolve_agent_account_label(env, home=tmp_path)
    # Assert
    assert label == "sac-env"


def test_env_override_wins_over_host_credentials(tmp_path):
    # Arrange — host OAuth file present, but agent overrides via env.
    _write_credentials(tmp_path)
    _write_claude_json(tmp_path, "host@example.com")
    env = {"SAC_ANTHROPIC_API_KEY": "sk-ant-api03-zzzz9999"}
    # Act
    label = resolve_agent_account_label(env, home=tmp_path)
    # Assert
    assert label == "apikey:…9999"


def test_blank_env_override_falls_through_to_host(tmp_path):
    # Arrange — a whitespace-only override must NOT count as a credential.
    _write_credentials(tmp_path)
    _write_claude_json(tmp_path, "host@example.com")
    env = {"SAC_ANTHROPIC_API_KEY": "   "}
    # Act
    label = resolve_agent_account_label(env, home=tmp_path)
    # Assert
    assert label == "host@example.com"


# ---------------------------------------------------------------------------
# 2. Host shared-OAuth branch — identity from ~/.claude.json email.
# ---------------------------------------------------------------------------


def test_host_oauth_email_when_no_saved_account_match(tmp_path):
    # Arrange
    _write_credentials(tmp_path)
    _write_claude_json(tmp_path, "solo@example.com")
    # Act
    label = resolve_agent_account_label({}, home=tmp_path)
    # Assert
    assert label == "solo@example.com"


def test_host_oauth_prefers_saved_account_name_on_email_match(tmp_path):
    # Arrange — a saved account whose email matches the host OAuth email.
    _write_credentials(tmp_path)
    _write_claude_json(tmp_path, "ywata@example.com")
    save_account("primary", {"email_address": "ywata@example.com"}, home=tmp_path)
    # Act
    label = resolve_agent_account_label({}, home=tmp_path)
    # Assert
    assert label == "primary (ywata@example.com)"


def test_host_oauth_ignores_saved_account_with_different_email(tmp_path):
    # Arrange — saved account exists but its email does not match.
    _write_credentials(tmp_path)
    _write_claude_json(tmp_path, "current@example.com")
    save_account("other", {"email_address": "different@example.com"}, home=tmp_path)
    # Act
    label = resolve_agent_account_label({}, home=tmp_path)
    # Assert
    assert label == "current@example.com"


# ---------------------------------------------------------------------------
# 3. Fallback branches — never crash.
# ---------------------------------------------------------------------------


def test_no_credentials_file_returns_unknown(tmp_path):
    # Arrange — empty home: no credentials.json, no env override.
    # Act
    label = resolve_agent_account_label({}, home=tmp_path)
    # Assert
    assert label == "unknown"


def test_credentials_present_but_no_email_returns_default(tmp_path):
    # Arrange — credentials file present, but oauthAccount has no email.
    _write_credentials(tmp_path)
    _write_claude_json(tmp_path, None)
    # Act
    label = resolve_agent_account_label({}, home=tmp_path)
    # Assert
    assert label == "default"


def test_credentials_present_but_no_claude_json_returns_default(tmp_path):
    # Arrange — credentials file present, ~/.claude.json entirely absent.
    _write_credentials(tmp_path)
    # Act
    label = resolve_agent_account_label({}, home=tmp_path)
    # Assert
    assert label == "default"


def test_none_env_inherits_host_account(tmp_path):
    # Arrange — env=None (not just empty) must still resolve host OAuth.
    _write_credentials(tmp_path)
    _write_claude_json(tmp_path, "inherited@example.com")
    # Act
    label = resolve_agent_account_label(None, home=tmp_path)
    # Assert
    assert label == "inherited@example.com"


# ---------------------------------------------------------------------------
# 4. Pinned-account branch — spec.claude.account wins over host OAuth.
# ---------------------------------------------------------------------------


def test_assigned_account_returns_name_and_email(tmp_path):
    # Arrange — agent pinned to a saved account; resolver pulls its email.
    save_account("alpha", {"email_address": "alpha@example.com"}, home=tmp_path)
    # Act
    label = resolve_agent_account_label({}, home=tmp_path, assigned_account="alpha")
    # Assert
    assert label == "alpha (alpha@example.com)"


def test_assigned_account_overrides_host_oauth_identity(tmp_path):
    # Arrange — host /login is a DIFFERENT account; the pin must win
    # because the runtime copies the pinned snapshot in at start.
    _write_credentials(tmp_path)
    _write_claude_json(tmp_path, "host@example.com")
    save_account("beta", {"email_address": "beta@example.com"}, home=tmp_path)
    # Act
    label = resolve_agent_account_label({}, home=tmp_path, assigned_account="beta")
    # Assert
    assert label == "beta (beta@example.com)"


def test_assigned_account_with_no_saved_email_returns_bare_name(tmp_path):
    # Arrange — saved account exists but has no recorded email.
    save_account("gamma", {}, home=tmp_path)
    # Act
    label = resolve_agent_account_label({}, home=tmp_path, assigned_account="gamma")
    # Assert
    assert label == "gamma"


def test_assigned_account_missing_snapshot_returns_bare_name(tmp_path):
    # Arrange — no saved account dir at all; resolver degrades to the
    # bare assigned name rather than crashing or falling to host OAuth.
    # Act
    label = resolve_agent_account_label({}, home=tmp_path, assigned_account="ghost")
    # Assert
    assert label == "ghost"


def test_assigned_account_wins_over_env_override(tmp_path):
    # Arrange — both a pin AND an env key are present; the pin (frozen
    # snapshot copied in at start) is the definitive account.
    save_account("delta", {"email_address": "delta@example.com"}, home=tmp_path)
    env = {"SAC_ANTHROPIC_API_KEY": "sk-ant-api03-zzzz9999"}
    # Act
    label = resolve_agent_account_label(env, home=tmp_path, assigned_account="delta")
    # Assert
    assert label == "delta (delta@example.com)"


def test_empty_assigned_account_falls_through_to_host(tmp_path):
    # Arrange — assigned_account="" must NOT short-circuit; host OAuth wins.
    _write_credentials(tmp_path)
    _write_claude_json(tmp_path, "host@example.com")
    # Act
    label = resolve_agent_account_label({}, home=tmp_path, assigned_account="")
    # Assert
    assert label == "host@example.com"
