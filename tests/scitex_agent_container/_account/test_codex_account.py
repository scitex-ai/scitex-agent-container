"""Secret-safe Codex account metadata extraction tests."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest

from scitex_agent_container._account.codex_account import (
    CodexAccountSyncError,
    read_codex_account_metadata,
    read_codex_accounts_metadata,
    sync_codex_account,
)


@pytest.fixture(autouse=True)
def clean_codex_home(env_save_restore):
    env_save_restore.delete("CODEX_HOME")
    env_save_restore.delete("SCITEX_GENAI_CODEX_HOMES")


def _jwt(payload: dict) -> str:
    encoded = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    return f"header.{encoded.rstrip('=')}.signature"


def _write_auth(home: Path, payload: dict) -> Path:
    path = home / ".codex" / "auth.json"
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps(payload))
    return path


def _chatgpt_auth(
    *,
    account_id: str = "account-123",
    last_refresh: str = "2026-06-20T01:02:03Z",
) -> dict:
    claims = {
        "email": "person@example.com",
        "name": "Example Person",
        "https://api.openai.com/auth": {
            "chatgpt_account_id": account_id,
            "chatgpt_plan_type": "plus",
            "chatgpt_subscription_active_start": "2026-06-01T00:00:00Z",
            "chatgpt_subscription_active_until": "2026-07-01T00:00:00Z",
            "organizations": [
                {"title": "Personal", "role": "owner", "is_default": True}
            ],
        },
    }
    return {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": "sk-never-emit",
        "tokens": {
            "id_token": _jwt(claims),
            "access_token": _jwt(
                {
                    "exp": 4_000_000_000,
                    "https://api.openai.com/auth": {"chatgpt_account_id": account_id},
                }
            ),
            "refresh_token": "refresh-never-emit",
            "account_id": "fallback-account",
        },
        "last_refresh": last_refresh,
    }


def test_missing_auth_file_returns_empty(tmp_path: Path):
    # Arrange
    home = tmp_path
    # Act
    result = read_codex_account_metadata(home=home)
    # Assert
    assert result == {}


def test_reads_chatgpt_email(tmp_path: Path):
    # Arrange
    _write_auth(tmp_path, _chatgpt_auth())
    # Act
    result = read_codex_account_metadata(home=tmp_path)
    # Assert
    assert result["email_address"] == "person@example.com"


def test_reads_chatgpt_plan(tmp_path: Path):
    # Arrange
    _write_auth(tmp_path, _chatgpt_auth())
    # Act
    result = read_codex_account_metadata(home=tmp_path)
    # Assert
    assert result["plan_type"] == "plus"


def test_reads_default_organization(tmp_path: Path):
    # Arrange
    _write_auth(tmp_path, _chatgpt_auth())
    # Act
    result = read_codex_account_metadata(home=tmp_path)
    # Assert
    assert result["organization_name"] == "Personal"


def test_result_never_contains_source_secrets(tmp_path: Path):
    # Arrange
    _write_auth(tmp_path, _chatgpt_auth())
    # Act
    rendered = json.dumps(read_codex_account_metadata(home=tmp_path))
    # Assert
    assert "never-emit" not in rendered


def test_api_key_mode_reports_mode_without_key(tmp_path: Path):
    # Arrange
    _write_auth(
        tmp_path,
        {"auth_mode": "apikey", "OPENAI_API_KEY": "sk-never-emit"},
    )
    # Act
    result = read_codex_account_metadata(home=tmp_path)
    # Assert
    assert result == {
        "auth_mode": "apikey",
        "email_address": None,
        "display_name": None,
        "account_id": None,
        "plan_type": None,
        "organization_name": None,
        "organization_role": None,
        "subscription_active_start": None,
        "subscription_active_until": None,
        "last_refresh": None,
    }


def test_malformed_token_keeps_non_secret_file_metadata(tmp_path: Path):
    # Arrange
    _write_auth(
        tmp_path,
        {"auth_mode": "chatgpt", "tokens": {"id_token": "not-a-jwt"}},
    )
    # Act
    result = read_codex_account_metadata(home=tmp_path)
    # Assert
    assert result["auth_mode"] == "chatgpt"


def test_codex_home_overrides_default_home(tmp_path: Path, env_save_restore):
    # Arrange
    codex_home = tmp_path / "custom-codex"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text(json.dumps(_chatgpt_auth()))
    env_save_restore.set("CODEX_HOME", str(codex_home))
    # Act
    result = read_codex_account_metadata(home=tmp_path / "other")
    # Assert
    assert result["account_id"] == "account-123"


def test_gateway_homes_return_all_provider_accounts(tmp_path: Path, env_save_restore):
    # Arrange
    first = tmp_path / "primary"
    second = tmp_path / "secondary"
    for codex_home in (first, second):
        codex_home.mkdir()
        (codex_home / "auth.json").write_text(json.dumps(_chatgpt_auth()))
    env_save_restore.set("SCITEX_GENAI_CODEX_HOMES", f"{first}{os.pathsep}{second}")
    # Act
    result = read_codex_accounts_metadata(home=tmp_path / "unused")
    # Assert
    assert [item["gateway_alias"] for item in result] == ["primary", "secondary"]


def test_sync_collects_account_under_provider_qualified_store(tmp_path: Path):
    # Arrange
    source = _write_auth(tmp_path, _chatgpt_auth())
    # Act
    destination = sync_codex_account(home=tmp_path)
    metadata = json.loads(destination.with_name("account.json").read_text())
    # Assert
    assert (
        destination.relative_to(tmp_path).as_posix(),
        destination.read_bytes(),
        destination.stat().st_mode & 0o777,
        metadata["qualified_id"],
        "tokens" in metadata,
    ) == (
        ".scitex/agent-container/accounts/openai/person-example-com/auth.json",
        source.read_bytes(),
        0o600,
        "openai:person-example-com",
        False,
    )


def test_sync_fails_loud_without_identity_or_explicit_name(tmp_path: Path):
    # Arrange
    _write_auth(tmp_path, {"auth_mode": "apikey", "OPENAI_API_KEY": "secret"})

    # Act
    def sync() -> Path:
        return sync_codex_account(home=tmp_path)

    # Assert
    with pytest.raises(CodexAccountSyncError, match="explicit account name"):
        sync()


def test_stored_accounts_are_default_gateway_accounts(tmp_path: Path):
    # Arrange
    _write_auth(tmp_path, _chatgpt_auth())
    sync_codex_account(home=tmp_path)
    # Act
    result = read_codex_accounts_metadata(home=tmp_path)
    # Assert
    assert [item["gateway_alias"] for item in result] == ["person-example-com"]


def test_sync_does_not_replace_newer_stored_credential(tmp_path: Path):
    # Arrange
    source = _write_auth(tmp_path, _chatgpt_auth(last_refresh="2026-06-21T01:02:03Z"))
    destination = sync_codex_account(home=tmp_path)
    stored = destination.read_bytes()
    source.write_text(json.dumps(_chatgpt_auth(last_refresh="2026-06-20T01:02:03Z")))
    # Act
    result = sync_codex_account(home=tmp_path)
    # Assert
    assert (result, destination.read_bytes()) == (destination, stored)


def test_sync_rejects_identity_change_for_existing_alias(tmp_path: Path):
    # Arrange
    source = _write_auth(tmp_path, _chatgpt_auth(account_id="account-one"))
    sync_codex_account(home=tmp_path, name="shared")
    source.write_text(
        json.dumps(
            _chatgpt_auth(
                account_id="account-two",
                last_refresh="2026-06-21T01:02:03Z",
            )
        )
    )

    # Act
    def sync() -> Path:
        return sync_codex_account(home=tmp_path, name="shared")

    # Assert
    with pytest.raises(CodexAccountSyncError, match="identity differs"):
        sync()


def test_present_empty_provider_store_fails_loud(tmp_path: Path):
    # Arrange
    (tmp_path / ".scitex" / "agent-container" / "accounts" / "openai").mkdir(
        parents=True
    )

    # Act
    def read() -> list[dict]:
        return read_codex_accounts_metadata(home=tmp_path)

    # Assert
    with pytest.raises(CodexAccountSyncError, match="contains no auth files"):
        read()


def test_malformed_stored_credential_fails_loud(tmp_path: Path):
    # Arrange
    path = (
        tmp_path
        / ".scitex"
        / "agent-container"
        / "accounts"
        / "openai"
        / "broken"
        / "auth.json"
    )
    path.parent.mkdir(parents=True)
    path.write_text("{}")

    # Act
    def read() -> list[dict]:
        return read_codex_accounts_metadata(home=tmp_path)

    # Assert
    with pytest.raises(CodexAccountSyncError, match="missing or malformed"):
        read()
