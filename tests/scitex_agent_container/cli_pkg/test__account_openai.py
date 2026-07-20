"""Provider-aware OpenAI coverage for ``sac accounts list``."""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from scitex_agent_container._state.account_store import save_account
from scitex_agent_container.cli_pkg._account_list_render import (
    openai_account_name,
)
from scitex_agent_container.cli_pkg.account_group import account


@pytest.fixture(autouse=True)
def sandbox_home(tmp_path: Path, env_save_restore) -> Path:
    home = tmp_path / "home"
    home.mkdir()
    env_save_restore.set("HOME", str(home))
    env_save_restore.delete("CODEX_HOME")
    env_save_restore.delete("SCITEX_GENAI_CODEX_HOMES")
    return home


def _write_codex_auth(home: Path, directory: str = ".codex") -> None:
    claims = {
        "email": "same@example.com",
        "name": "Same Person",
        "https://api.openai.com/auth": {
            "chatgpt_account_id": "openai-account",
            "chatgpt_plan_type": "plus",
            "organizations": [
                {"title": "Personal", "role": "owner", "is_default": True}
            ],
        },
    }
    payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode()
    auth = {
        "auth_mode": "chatgpt",
        "tokens": {
            "id_token": f"header.{payload.rstrip('=')}.signature",
            "refresh_token": "refresh-must-not-appear",
        },
        "last_refresh": "2026-07-01T00:00:00Z",
    }
    path = home / directory / "auth.json"
    path.parent.mkdir()
    path.write_text(json.dumps(auth))


def test_openai_account_name_is_email_slug():
    # Arrange
    metadata = {"email_address": "Same@Example.com"}
    # Act
    name = openai_account_name(metadata)
    # Assert
    assert name == "same-example-com"


def test_human_list_shows_openai_account_block(sandbox_home: Path):
    # Arrange
    _write_codex_auth(sandbox_home)
    # Act
    result = CliRunner().invoke(account, ["list"])
    # Assert
    assert "OpenAI Codex account" in result.output


def test_human_list_shows_both_provider_identities(sandbox_home: Path):
    # Arrange
    _write_codex_auth(sandbox_home)
    save_account("same-example-com", {}, home=sandbox_home)
    # Act
    result = CliRunner().invoke(account, ["list"])
    # Assert
    assert "claude-code" in result.output and "openai" in result.output


def test_human_list_never_emits_refresh_token(sandbox_home: Path):
    # Arrange
    _write_codex_auth(sandbox_home)
    # Act
    result = CliRunner().invoke(account, ["list"])
    # Assert
    assert "refresh-must-not-appear" not in result.output


def test_json_has_distinct_qualified_ids(sandbox_home: Path):
    # Arrange
    _write_codex_auth(sandbox_home)
    save_account("same-example-com", {}, home=sandbox_home)
    # Act
    result = CliRunner().invoke(account, ["list", "--json"])
    identities = {
        item["qualified_id"] for item in json.loads(result.output)["accounts"]
    }
    # Assert
    assert identities == {
        "claude-code:same-example-com",
        "openai:same-example-com",
    }


def test_json_preserves_legacy_stored_list(sandbox_home: Path):
    # Arrange
    _write_codex_auth(sandbox_home)
    save_account("same-example-com", {}, home=sandbox_home)
    # Act
    result = CliRunner().invoke(account, ["list", "--json"])
    # Assert
    assert json.loads(result.output)["stored"][0]["name"] == "same-example-com"


def test_json_lists_every_gateway_account(sandbox_home: Path, env_save_restore):
    # Arrange
    first = sandbox_home / ".codex-primary"
    second = sandbox_home / ".codex-secondary"
    _write_codex_auth(sandbox_home, first.name)
    _write_codex_auth(sandbox_home, second.name)
    env_save_restore.set(
        "SCITEX_GENAI_CODEX_HOMES",
        f"{first}{os.pathsep}{second}",
    )
    # Act
    result = CliRunner().invoke(account, ["list", "--json"])
    payload = json.loads(result.output)
    identities = [item["qualified_id"] for item in payload["accounts"]]
    # Assert
    assert identities == ["openai:codex-primary", "openai:codex-secondary"]


def test_sync_openai_collects_active_login(sandbox_home: Path):
    # Arrange
    _write_codex_auth(sandbox_home)
    # Act
    result = CliRunner().invoke(account, ["sync-openai"])
    stored = (
        sandbox_home
        / ".scitex"
        / "agent-container"
        / "accounts"
        / "openai"
        / "same-example-com"
        / "auth.json"
    )
    # Assert
    assert (result.exit_code, stored.is_file(), stored.stat().st_mode & 0o777) == (
        0,
        True,
        0o600,
    )
