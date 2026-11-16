"""Account collection requires approval before touching the credential backend."""

from __future__ import annotations

import json

import pytest
from click.testing import CliRunner

from scitex_agent_container._account import codex_account
from scitex_agent_container.cli_pkg._main import main
from tests.scitex_agent_container.cli_pkg.test__account_openai import _write_codex_auth


@pytest.fixture
def owned_login(tmp_path, env_save_restore):
    env_save_restore.set("HOME", str(tmp_path))
    env_save_restore.delete("CODEX_HOME")
    env_save_restore.delete("SCITEX_GENAI_CODEX_HOMES")
    _write_codex_auth(tmp_path)
    return tmp_path


@pytest.fixture
def backend_trap():
    calls = []
    previous = codex_account.sync_codex_account

    def refuse(**kwargs):
        calls.append(kwargs)
        raise AssertionError("credential backend must not run")

    codex_account.sync_codex_account = refuse
    try:
        yield calls
    finally:
        codex_account.sync_codex_account = previous


@pytest.mark.parametrize("flags", [[], ["--name", "shared"]])
def test_collection_refuses_without_approval(owned_login, backend_trap, flags):
    # Arrange
    store = owned_login / ".scitex/agent-container/accounts/openai"
    # Act
    result = CliRunner().invoke(main, ["accounts", "sync-openai", *flags])
    # Assert
    assert (
        result.exit_code,
        "--yes" in result.output,
        backend_trap,
        store.exists(),
    ) == (2, True, [], False)


@pytest.mark.parametrize(
    "flags", [["--dry-run"], ["--dry-run", "--yes"], ["-y", "--dry-run"]]
)
def test_preview_wins_over_approval_without_backend(owned_login, backend_trap, flags):
    # Arrange
    source = owned_login / ".codex/auth.json"
    before = source.read_bytes()
    # Act
    result = CliRunner().invoke(
        main, ["accounts", "sync-openai", "--name", "shared", *flags]
    )
    # Assert
    assert (result.exit_code, backend_trap, source.read_bytes() == before) == (
        0,
        [],
        True,
    )


def test_preview_reports_arguments_only_even_with_unreadable_login(
    owned_login, backend_trap
):
    # Arrange
    source = owned_login / ".codex/auth.json"
    source.unlink()
    source.mkdir()
    # Act
    result = CliRunner().invoke(
        main, ["--json", "accounts", "sync-openai", "--dry-run"]
    )
    # Assert
    assert json.loads(result.stdout) == {
        "action": "sync-openai",
        "dry_run": True,
        "name": None,
    }


@pytest.mark.parametrize("approval", ["--yes", "-y"])
def test_approved_collection_preserves_secret_file(owned_login, approval):
    # Arrange
    source = owned_login / ".codex/auth.json"
    store = (
        owned_login
        / ".scitex/agent-container/accounts/openai/same-example-com/auth.json"
    )
    # Act
    result = CliRunner().invoke(main, ["accounts", "sync-openai", approval])
    # Assert
    assert (result.exit_code, store.read_bytes(), store.stat().st_mode & 0o777) == (
        0,
        source.read_bytes(),
        0o600,
    )


def test_approved_explicit_account_name_keeps_provider_directory(owned_login):
    # Arrange
    store = owned_login / ".scitex/agent-container/accounts/openai/shared/account.json"
    # Act
    result = CliRunner().invoke(
        main, ["accounts", "sync-openai", "--name", "shared", "--yes"]
    )
    # Assert
    assert (result.exit_code, json.loads(store.read_text())["qualified_id"]) == (
        0,
        "openai:shared",
    )


def test_approved_collection_does_not_print_synthetic_token(owned_login):
    # Arrange
    token = "refresh-must-not-appear"
    # Act
    result = CliRunner().invoke(main, ["accounts", "sync-openai", "--yes"])
    # Assert
    assert (result.exit_code, token in result.output) == (0, False)
