"""Tests for ``sac account`` group + top-level ``quota-watch``.

PA-306 no-mocks: every test here exercises real production collaborators.

Sandboxing strategy
-------------------
* ``HOME`` env var is redirected to ``tmp_path`` — ``Path.home()`` reads
  ``$HOME`` on POSIX, so the entire account-store cascade (which keys
  off ``home``) lands inside the test's tmpdir. See the parallel
  isolation pattern in ``tests/scitex_agent_container/_state/test_account_store.py``.

* ``account save`` / ``list`` / ``delete`` / ``switch`` use the real
  ``_state.account_store`` functions on a real filesystem.

* ``watch-quota`` / ``quota-watch`` are exercised only through the
  ``--once`` and ``--dry-run`` code paths. In those paths the real
  ``check_and_rotate`` calls ``fetch_usage`` which, with no Anthropic
  credentials on disk, returns ``{"error": ...}`` — and the command
  reports that honestly. The daemon / infinite-loop / survival paths
  were previously mock-only (they asserted on
  ``run_loop.assert_called_once()`` against a ``MagicMock``); they are
  deleted here because there is no honest way to drive ``run_loop``
  (it loops forever calling ``time.sleep``) or to force
  ``survival_mode_check`` into ``True`` without a real-or-faked
  Anthropic API — and adding a test-only injection seam to production
  is also forbidden.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from click.testing import CliRunner

from scitex_agent_container._state.account_store import (
    _METADATA_FILENAME,
    save_account,
)
from scitex_agent_container.cli_pkg.account_group import account, quota_watch

# ---------------------------------------------------------------------------
# Fixtures + helpers
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def sandbox_home(tmp_path, env_save_restore):
    """Redirect ``$HOME`` so ``Path.home()`` resolves inside ``tmp_path``.

    Honest no-mocks alternative to ``monkeypatch.setattr(Path, "home", ...)``:
    ``Path.home()`` on POSIX reads ``os.environ['HOME']`` (see CPython
    ``pathlib`` / ``os.path.expanduser``), so a real env mutation is the
    real equivalent.
    """
    home = tmp_path / "home"
    home.mkdir()
    env_save_restore.set("HOME", str(home))
    return home


def _accounts_dir(home: Path) -> Path:
    return home / ".scitex" / "agent-container" / "accounts"


def _meta_file(home: Path, name: str) -> Path:
    return _accounts_dir(home) / name / _METADATA_FILENAME


def _seed_active_credentials(home: Path, *, email: str | None = None) -> None:
    """Write a minimal ``~/.claude.json`` so the auto-detect path picks up.

    Real format read by ``read_credentials_metadata``.
    """
    (home / ".claude").mkdir(exist_ok=True)
    (home / ".claude" / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"subscriptionType": "max"}})
    )
    if email is not None:
        (home / ".claude.json").write_text(
            json.dumps({"oauthAccount": {"emailAddress": email}})
        )


# ---------------------------------------------------------------------------
# account save — dry-run
# ---------------------------------------------------------------------------


def test_account_save_dry_run_exits_zero(sandbox_home):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["save", "work", "--dry-run"])
    # Assert
    assert result.exit_code == 0


def test_account_save_dry_run_announces_action(sandbox_home):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["save", "work", "--dry-run"])
    # Assert
    assert "dry-run" in result.output and "work" in result.output


def test_account_save_dry_run_does_not_persist(sandbox_home):
    # Arrange
    runner = CliRunner()
    # Act
    runner.invoke(account, ["save", "work", "--dry-run"])
    # Assert
    assert not (_accounts_dir(sandbox_home) / "work").exists()


# ---------------------------------------------------------------------------
# account save — explicit email
# ---------------------------------------------------------------------------


def test_account_save_explicit_email_exits_zero(sandbox_home):
    # Arrange
    _seed_active_credentials(sandbox_home)
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["save", "work", "--email", "explicit@example.com"])
    # Assert
    assert result.exit_code == 0, result.output


def test_account_save_explicit_email_announces_save(sandbox_home):
    # Arrange
    _seed_active_credentials(sandbox_home)
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["save", "work", "--email", "explicit@example.com"])
    # Assert
    assert "Saved account 'work'" in result.output


def test_account_save_explicit_email_writes_metadata_file(sandbox_home):
    # Arrange
    _seed_active_credentials(sandbox_home)
    runner = CliRunner()
    # Act
    runner.invoke(account, ["save", "work", "--email", "explicit@example.com"])
    # Assert
    assert _meta_file(sandbox_home, "work").is_file()


def test_account_save_explicit_email_records_email_in_metadata(sandbox_home):
    # Arrange
    _seed_active_credentials(sandbox_home)
    runner = CliRunner()
    # Act
    runner.invoke(account, ["save", "work", "--email", "explicit@example.com"])
    payload = json.loads(_meta_file(sandbox_home, "work").read_text())
    # Assert
    assert payload["email_address"] == "explicit@example.com"


def test_account_save_explicit_email_copies_credentials_snapshot(sandbox_home):
    # Arrange
    _seed_active_credentials(sandbox_home)
    runner = CliRunner()
    # Act
    runner.invoke(account, ["save", "work", "--email", "explicit@example.com"])
    snapshot = _accounts_dir(sandbox_home) / "work" / ".credentials.json"
    # Assert
    assert snapshot.is_file()


# ---------------------------------------------------------------------------
# account save — autodetect email from ~/.claude.json
# ---------------------------------------------------------------------------


def test_account_save_autodetect_exits_zero(sandbox_home):
    # Arrange
    (sandbox_home / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": "auto@example.com"}})
    )
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["save", "work"])
    # Assert
    assert result.exit_code == 0, result.output


def test_account_save_autodetect_records_email_in_metadata(sandbox_home):
    # Arrange
    (sandbox_home / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": "auto@example.com"}})
    )
    runner = CliRunner()
    # Act
    runner.invoke(account, ["save", "work"])
    payload = json.loads(_meta_file(sandbox_home, "work").read_text())
    # Assert
    assert payload["email_address"] == "auto@example.com"


# ---------------------------------------------------------------------------
# account save — credentials unreadable
# ---------------------------------------------------------------------------


def test_account_save_corrupt_credentials_still_exits_zero(sandbox_home):
    # Arrange
    (sandbox_home / ".claude.json").write_text("{ not valid json")
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["save", "work"])
    # Assert
    assert result.exit_code == 0, result.output


def test_account_save_corrupt_credentials_writes_metadata_without_email(
    sandbox_home,
):
    # Arrange
    (sandbox_home / ".claude.json").write_text("{ not valid json")
    runner = CliRunner()
    # Act
    runner.invoke(account, ["save", "work"])
    payload = json.loads(_meta_file(sandbox_home, "work").read_text())
    # Assert
    assert "email_address" not in payload


# ---------------------------------------------------------------------------
# account list
# ---------------------------------------------------------------------------


def test_account_list_empty_reports_no_accounts(sandbox_home):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["list"])
    # Assert
    assert "No accounts stored" in result.output


def test_account_list_shows_header(sandbox_home):
    # Arrange
    save_account("work", {"email_address": "w@example.com"}, home=sandbox_home)
    save_account("personal", {}, home=sandbox_home)
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["list"])
    # Assert
    assert "Stored accounts" in result.output


def test_account_list_shows_first_account_name(sandbox_home):
    # Arrange
    save_account("work", {"email_address": "w@example.com"}, home=sandbox_home)
    save_account("personal", {}, home=sandbox_home)
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["list"])
    # Assert
    assert "work" in result.output


def test_account_list_shows_first_account_email(sandbox_home):
    # Arrange
    save_account("work", {"email_address": "w@example.com"}, home=sandbox_home)
    save_account("personal", {}, home=sandbox_home)
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["list"])
    # Assert
    assert "w@example.com" in result.output


def test_account_list_shows_second_account_name(sandbox_home):
    # Arrange
    save_account("work", {"email_address": "w@example.com"}, home=sandbox_home)
    save_account("personal", {}, home=sandbox_home)
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["list"])
    # Assert
    assert "personal" in result.output


def test_account_list_uses_no_email_placeholder(sandbox_home):
    # Arrange
    save_account("work", {"email_address": "w@example.com"}, home=sandbox_home)
    save_account("personal", {}, home=sandbox_home)
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["list"])
    # Assert
    assert "(no email)" in result.output


def test_account_list_json_exits_zero(sandbox_home):
    # Arrange
    save_account("x", {"email_address": "x@example.com"}, home=sandbox_home)
    (sandbox_home / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": "active@example.com"}})
    )
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["list", "--json"])
    # Assert
    assert result.exit_code == 0, result.output


def test_account_list_json_surfaces_active_email(sandbox_home):
    # Arrange
    save_account("x", {"email_address": "x@example.com"}, home=sandbox_home)
    (sandbox_home / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": "active@example.com"}})
    )
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["list", "--json"])
    payload = json.loads(result.output)
    # Assert
    assert payload["active"]["email_address"] == "active@example.com"


def test_account_list_json_lists_stored_accounts(sandbox_home):
    # Arrange
    save_account("x", {"email_address": "x@example.com"}, home=sandbox_home)
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["list", "--json"])
    payload = json.loads(result.output)
    # Assert
    assert [a["name"] for a in payload["stored"]] == ["x"]


def test_account_list_json_tolerates_corrupt_credentials_active_email(sandbox_home):
    # Arrange
    (sandbox_home / ".claude.json").write_text("{ not valid json")
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["list", "--json"])
    payload = json.loads(result.output)
    # Assert
    assert payload["active"]["email_address"] is None


def test_account_list_json_tolerates_corrupt_credentials_empty_stored(sandbox_home):
    # Arrange
    (sandbox_home / ".claude.json").write_text("{ not valid json")
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["list", "--json"])
    payload = json.loads(result.output)
    # Assert
    assert payload["stored"] == []


def test_account_list_human_tolerates_corrupt_credentials(sandbox_home):
    # Arrange
    (sandbox_home / ".claude.json").write_text("{ not valid json")
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["list"])
    # Assert
    assert result.exit_code == 0 and "No accounts stored" in result.output


# ---------------------------------------------------------------------------
# account delete
# ---------------------------------------------------------------------------


def test_account_delete_dry_run_exits_zero(sandbox_home):
    # Arrange
    save_account("work", {}, home=sandbox_home)
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["delete", "work", "--dry-run"])
    # Assert
    assert result.exit_code == 0


def test_account_delete_dry_run_announces_action(sandbox_home):
    # Arrange
    save_account("work", {}, home=sandbox_home)
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["delete", "work", "--dry-run"])
    # Assert
    assert "dry-run" in result.output


def test_account_delete_dry_run_preserves_account_dir(sandbox_home):
    # Arrange
    save_account("work", {}, home=sandbox_home)
    runner = CliRunner()
    # Act
    runner.invoke(account, ["delete", "work", "--dry-run"])
    # Assert
    assert (_accounts_dir(sandbox_home) / "work").is_dir()


def test_account_delete_without_yes_exits_two(sandbox_home):
    # Arrange
    save_account("work", {}, home=sandbox_home)
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["delete", "work"])
    # Assert
    assert result.exit_code == 2


def test_account_delete_without_yes_emits_refusal(sandbox_home):
    # Arrange
    save_account("work", {}, home=sandbox_home)
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["delete", "work"])
    # Assert
    assert "Refusing" in result.output


def test_account_delete_without_yes_preserves_account_dir(sandbox_home):
    # Arrange
    save_account("work", {}, home=sandbox_home)
    runner = CliRunner()
    # Act
    runner.invoke(account, ["delete", "work"])
    # Assert
    assert (_accounts_dir(sandbox_home) / "work").is_dir()


def test_account_delete_success_exits_zero(sandbox_home):
    # Arrange
    save_account("work", {}, home=sandbox_home)
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["delete", "work", "--yes"])
    # Assert
    assert result.exit_code == 0


def test_account_delete_success_announces_deletion(sandbox_home):
    # Arrange
    save_account("work", {}, home=sandbox_home)
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["delete", "work", "--yes"])
    # Assert
    assert "Deleted" in result.output


def test_account_delete_success_removes_account_dir(sandbox_home):
    # Arrange
    save_account("work", {}, home=sandbox_home)
    runner = CliRunner()
    # Act
    runner.invoke(account, ["delete", "work", "--yes"])
    # Assert
    assert not (_accounts_dir(sandbox_home) / "work").exists()


def test_account_delete_not_found_exits_one(sandbox_home):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["delete", "ghost", "--yes"])
    # Assert
    assert result.exit_code == 1


def test_account_delete_not_found_announces(sandbox_home):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["delete", "ghost", "--yes"])
    # Assert
    assert "not found" in result.output


# ---------------------------------------------------------------------------
# account switch
# ---------------------------------------------------------------------------


def _stage_switchable_account(home: Path) -> None:
    """Pre-create a saved account *with* a real credential snapshot file."""
    save_account("work", {"email_address": "w@example.com"}, home=home)
    acct_dir = _accounts_dir(home) / "work"
    (acct_dir / ".credentials.json").write_text(
        json.dumps({"claudeAiOauth": {"subscriptionType": "max"}})
    )


def test_account_switch_success_exits_zero(sandbox_home):
    # Arrange
    _stage_switchable_account(sandbox_home)
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["switch", "work"])
    # Assert
    assert result.exit_code == 0, result.output


def test_account_switch_success_announces(sandbox_home):
    # Arrange
    _stage_switchable_account(sandbox_home)
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["switch", "work"])
    # Assert
    assert "Switched to account 'work'" in result.output


def test_account_switch_success_copies_credentials(sandbox_home):
    # Arrange
    _stage_switchable_account(sandbox_home)
    runner = CliRunner()
    # Act
    runner.invoke(account, ["switch", "work"])
    # Assert
    assert (sandbox_home / ".claude" / ".credentials.json").is_file()


def test_account_switch_success_does_not_leak_metadata(sandbox_home):
    # Arrange
    _stage_switchable_account(sandbox_home)
    runner = CliRunner()
    # Act
    runner.invoke(account, ["switch", "work"])
    # Assert
    assert not (sandbox_home / ".claude" / _METADATA_FILENAME).exists()


def test_account_switch_missing_exits_one(sandbox_home):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["switch", "missing"])
    # Assert
    assert result.exit_code == 1


def test_account_switch_missing_announces(sandbox_home):
    # Arrange
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["switch", "missing"])
    # Assert
    assert "No account directory" in result.output


# ---------------------------------------------------------------------------
# watch-quota — single-shot (--once / --dry-run) paths only.
#
# Honest scope: with no Anthropic credentials on disk, ``fetch_usage``
# (called by ``check_and_rotate``) returns ``{"error": ...}`` and the
# command surfaces a single ``[error] ...`` (or equivalent) line on
# stdout. That is the real behaviour of the production code in this
# configuration — and the command's wiring (option parsing, branch
# selection, echo formatting, exit code) is what these CLI tests are
# responsible for.
#
# The previously-existing tests for ``--daemon`` and the bare
# infinite-loop invocation were mock-only: they replaced ``run_loop``
# with a ``MagicMock`` and asserted ``run_loop.assert_called_once()``.
# There is no honest way to exercise ``run_loop`` in-process (it loops
# forever calling ``time.sleep``); driving it would require either a
# test-only production seam (forbidden by PA-306) or a subprocess with
# a kill signal (out of scope for these CLI tests). Those tests are
# deleted rather than re-greened with SimpleNamespace.
# ---------------------------------------------------------------------------


def _ensure_no_credentials(home: Path) -> None:
    """Ensure no Anthropic credentials are reachable from ``home``.

    With no ``~/.claude/.credentials.json``, ``fetch_usage`` returns an
    ``{"error": ...}`` dict without issuing any network request — a
    real, deterministic code path.
    """
    claude = home / ".claude"
    claude.mkdir(exist_ok=True)
    cred = claude / ".credentials.json"
    if cred.exists():
        cred.unlink()


# Parametrize on the (command, argv) pair so each test body is straight-line
# (STX-TQ006 forbids ``if/else`` inside a parametrized test body).
_ONCE_CASES = [
    pytest.param(account, ["watch-quota", "--once"], id="account-subcommand"),
    pytest.param(quota_watch, ["--once"], id="top-level"),
]
_DRY_CASES = [
    pytest.param(account, ["watch-quota", "--dry-run"], id="account-subcommand"),
    pytest.param(quota_watch, ["--dry-run"], id="top-level"),
]


@pytest.mark.parametrize("cmd,argv", _ONCE_CASES)
def test_watch_quota_once_exits_zero_without_credentials(sandbox_home, cmd, argv):
    # Arrange
    _ensure_no_credentials(sandbox_home)
    runner = CliRunner()
    # Act
    result = runner.invoke(cmd, argv)
    # Assert
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize("cmd,argv", _ONCE_CASES)
def test_watch_quota_once_emits_bracketed_action(sandbox_home, cmd, argv):
    # Arrange
    _ensure_no_credentials(sandbox_home)
    runner = CliRunner()
    # Act
    result = runner.invoke(cmd, argv)
    # Assert: ``check_and_rotate`` formats results as ``[<action>] <message>``.
    assert result.output.startswith("[")


@pytest.mark.parametrize("cmd,argv", _DRY_CASES)
def test_watch_quota_dry_run_exits_zero_without_credentials(sandbox_home, cmd, argv):
    # Arrange
    _ensure_no_credentials(sandbox_home)
    runner = CliRunner()
    # Act
    result = runner.invoke(cmd, argv)
    # Assert
    assert result.exit_code == 0, result.output


@pytest.mark.parametrize("cmd,argv", _DRY_CASES)
def test_watch_quota_dry_run_emits_bracketed_action(sandbox_home, cmd, argv):
    # Arrange
    _ensure_no_credentials(sandbox_home)
    runner = CliRunner()
    # Act
    result = runner.invoke(cmd, argv)
    # Assert
    assert result.output.startswith("[")


# ---------------------------------------------------------------------------
# Branch coverage closure — account list active-credentials block printed
# ---------------------------------------------------------------------------


def test_account_list_prints_blank_line_after_active_credentials_block(sandbox_home):
    # Arrange — seed an active oauth email so _format_claude_account_block
    # yields a non-empty list and the post-block blank line fires.
    _seed_active_credentials(sandbox_home, email="active@example.com")
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["list"])
    # Assert — header from the rendered block proves the truthy branch fired.
    assert "Claude Code account" in result.output


# ---------------------------------------------------------------------------
# Branch coverage closure — watch-quota survival-mode echo
# ---------------------------------------------------------------------------


def _seed_usage_cache(home: Path, *, used_pct_5h: float) -> None:
    """Seed the real claude_usage cache to drive survival_mode_check=True.

    fetch_usage reads ``~/.scitex/cache/claude_usage.json`` first; entries
    younger than 5min short-circuit the network call. Writing a fresh
    cache row with used_pct_5h above SURVIVAL_THRESHOLD lets us drive the
    real survival_mode_check into its True branch without any mocking.
    """
    from datetime import datetime, timezone

    cache_dir = home / ".scitex" / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "used_tokens_5h": 9999,
        "limit_tokens_5h": 10000,
        "used_pct_5h": used_pct_5h,
        "reset_at_5h": None,
        "used_tokens_7d": None,
        "limit_tokens_7d": None,
        "used_pct_7d": None,
        "reset_at_7d": None,
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "from_cache": False,
        "error": None,
    }
    (cache_dir / "claude_usage.json").write_text(json.dumps(payload))


def test_watch_quota_once_echoes_survival_banner_when_single_account_over_threshold(
    sandbox_home,
):
    # Arrange — exactly one stored account + cached quota above threshold.
    save_account("only", {"email_address": "o@example.com"}, home=sandbox_home)
    _seed_usage_cache(sandbox_home, used_pct_5h=99.5)
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["watch-quota", "--once"])
    # Assert
    assert "[SURVIVAL]" in result.output


# ---------------------------------------------------------------------------
# account list — offline plan/tier + cache-only usage enrichment
# ---------------------------------------------------------------------------


def _store_dir(home: Path) -> Path:
    return home / ".scitex" / "agent-container" / "accounts"


def _write_plan_snapshot(home: Path, name: str, *, subscription: str, tier: str):
    """Write a real account snapshot .credentials.json with the two
    non-secret plan fields."""
    save_account(name, {"email_address": f"{name}@x"}, home=home)
    (_store_dir(home) / name / ".credentials.json").write_text(
        json.dumps(
            {
                "claudeAiOauth": {
                    "accessToken": "sk-ant-SECRET",
                    "subscriptionType": subscription,
                    "rateLimitTier": tier,
                }
            }
        )
    )


def test_account_list_human_shows_offline_plan_label(sandbox_home):
    # Arrange
    _write_plan_snapshot(
        sandbox_home, "work", subscription="max", tier="default_claude_max_20x"
    )
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["list"])
    # Assert
    assert "Max 20x" in result.output


def test_account_list_human_shows_usage_dash_when_no_cache(sandbox_home):
    # Arrange — snapshot present, but no per-account usage.json cache.
    _write_plan_snapshot(
        sandbox_home, "work", subscription="pro", tier="default_claude_pro"
    )
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["list"])
    # Assert — the rich table renders ``-`` cells for missing usage% and
    # As-of (the prior ``usage: —`` was a flat-line format; the new
    # renderer emits a `rich.table.Table` with one cell per metric).
    assert "5h%" in result.output and "7d%" in result.output
    # Confirm the empty-data cells are rendered as `-`.
    assert " - " in result.output


def test_account_list_json_includes_plan_label(sandbox_home):
    # Arrange
    _write_plan_snapshot(
        sandbox_home, "work", subscription="max", tier="default_claude_max_5x"
    )
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["list", "--json"])
    payload = json.loads(result.output)
    # Assert
    assert payload["stored"][0]["plan_label"] == "Max 5x"


def test_account_list_json_usage_is_none_when_no_cache(sandbox_home):
    # Arrange
    _write_plan_snapshot(
        sandbox_home, "work", subscription="pro", tier="default_claude_pro"
    )
    runner = CliRunner()
    # Act
    result = runner.invoke(account, ["list", "--json"])
    payload = json.loads(result.output)
    # Assert
    assert payload["stored"][0]["usage"] is None
