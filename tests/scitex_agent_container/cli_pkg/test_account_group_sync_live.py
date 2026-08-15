"""Tests for ``sac accounts sync-live`` + the ``list`` freshness column.

PA-306 no-mocks: every test drives the real click commands via
``CliRunner`` against a real tmp ``$HOME``. ``Path.home()`` reads
``$HOME`` on POSIX, so the autouse ``sandbox_home`` redirect lands the
whole account-store cascade inside the test tmpdir.

AAA markers (TQ002), descriptive names (TQ003), one assertion each
(TQ007).
"""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from scitex_agent_container._state.account_store import save_account
from scitex_agent_container.cli_pkg.account_group import account


@pytest.fixture(autouse=True)
def sandbox_home(tmp_path, env_save_restore):
    """Redirect ``$HOME`` so ``Path.home()`` resolves inside ``tmp_path``."""
    home = tmp_path / "home"
    home.mkdir()
    env_save_restore.set("HOME", str(home))
    return home


def _write_live(home: Path, email: str, expires_at_ms: int) -> None:
    claude = home / ".claude"
    claude.mkdir(parents=True, exist_ok=True)
    (claude / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"expiresAt": expires_at_ms}})
    )
    (home / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": email}})
    )


def _snapshot_with_expiry(home: Path, name: str, expires_at_ms: int) -> None:
    """Save a store snapshot carrying an explicit OAuth expiry."""
    save_account(name, {"email_address": f"{name}@x"}, home=home)
    (
        home / ".scitex" / "agent-container" / "accounts" / name / ".credentials.json"
    ).write_text(json.dumps({"claudeAiOauth": {"expiresAt": expires_at_ms}}))


# ---------------------------------------------------------------------------
# sync-live — happy path
# ---------------------------------------------------------------------------


def test_sync_live_exits_zero_for_valid_live(sandbox_home):
    # Arrange
    _write_live(sandbox_home, "alpha@example.com", int((time.time() + 3_600) * 1_000))
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["sync-live"])
    # Assert
    assert result.exit_code == 0, result.output


def test_sync_live_reports_saved_store_name(sandbox_home):
    # Arrange
    _write_live(sandbox_home, "alpha@example.com", int((time.time() + 3_600) * 1_000))
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["sync-live"])
    # Assert
    assert "alpha-example-com" in result.output


def test_sync_live_json_action_saved(sandbox_home):
    # Arrange
    _write_live(sandbox_home, "ywatanabe@scitex.ai", int((time.time() + 3_600) * 1_000))
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["sync-live", "--json"])
    payload = json.loads(result.stdout)
    # Assert
    assert payload["action"] == "saved"


def test_sync_live_idempotent_reports_up_to_date(sandbox_home):
    # Arrange — sync once, then sync again over the unchanged live cred.
    _write_live(sandbox_home, "alpha@example.com", int((time.time() + 3_600) * 1_000))
    runner = CliRunner()
    runner.invoke(account, ["sync-live"])
    # Act
    result = runner.invoke(account, ["sync-live", "--json"])
    payload = json.loads(result.stdout)
    # Assert
    assert payload["action"] == "up-to-date"


# ---------------------------------------------------------------------------
# sync-live — fail loud
# ---------------------------------------------------------------------------


def test_sync_live_exits_nonzero_when_live_expired(sandbox_home):
    # Arrange — expired live cred must NOT be saved.
    _write_live(sandbox_home, "alpha@example.com", int((time.time() - 10_000) * 1_000))
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["sync-live"])
    # Assert
    assert result.exit_code == 1


def test_sync_live_exits_nonzero_when_live_absent(sandbox_home):
    # Arrange — only ~/.claude.json email, no credentials file.
    (sandbox_home / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": "x@y.com"}})
    )
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["sync-live"])
    # Assert
    assert result.exit_code == 1


def test_sync_live_error_message_points_at_login(sandbox_home):
    # Arrange
    _write_live(sandbox_home, "alpha@example.com", int((time.time() - 10_000) * 1_000))
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["sync-live"])
    # Assert
    assert "claude /login" in result.output


# ---------------------------------------------------------------------------
# list — freshness column
# ---------------------------------------------------------------------------


def test_list_human_shows_valid_for_future_snapshot(sandbox_home):
    # Arrange
    _snapshot_with_expiry(sandbox_home, "work", int((time.time() + 3_600) * 1_000))
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["list"])
    # Assert
    assert "VALID" in result.output


def test_list_human_shows_expired_for_past_snapshot(sandbox_home):
    # Arrange
    _snapshot_with_expiry(sandbox_home, "stale", int((time.time() - 3_600) * 1_000))
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["list"])
    # Assert
    assert "EXPIRED" in result.output


def test_list_human_shows_absent_when_snapshot_has_no_expiry(sandbox_home):
    # Arrange — metadata only, no .credentials.json with an expiry.
    save_account("bare", {"email_address": "bare@x"}, home=sandbox_home)
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["list"])
    # Assert
    assert "ABSENT" in result.output


def test_list_json_includes_freshness_state(sandbox_home):
    # Arrange
    _snapshot_with_expiry(sandbox_home, "work", int((time.time() + 3_600) * 1_000))
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["list", "--json"])
    payload = json.loads(result.stdout)
    # Assert
    assert payload["stored"][0]["freshness"] == "VALID"


def test_list_json_includes_freshness_hours(sandbox_home):
    # Arrange
    _snapshot_with_expiry(sandbox_home, "work", int((time.time() + 3_600) * 1_000))
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["list", "--json"])
    payload = json.loads(result.stdout)
    # Assert
    assert payload["stored"][0]["freshness_hours"] is not None
