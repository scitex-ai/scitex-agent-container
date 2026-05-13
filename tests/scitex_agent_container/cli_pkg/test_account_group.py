"""Tests for ``sac account`` group + top-level ``quota-watch``."""

from __future__ import annotations

import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from click.testing import CliRunner

from scitex_agent_container.cli_pkg.account_group import account, quota_watch


@pytest.fixture(autouse=True)
def sandbox_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    return home


def _install_fake_state(monkeypatch, **overrides):
    """Stub scitex_agent_container._state.account_store."""
    mod = types.ModuleType("scitex_agent_container._state.account_store")
    mod._store_path = overrides.get(
        "_store_path", lambda root, home: home / ".scitex" / "accounts"
    )
    mod.save_account = overrides.get("save_account", MagicMock())
    mod.list_accounts = overrides.get("list_accounts", lambda: [])
    mod.delete_account = overrides.get("delete_account", lambda n: True)
    mod.switch_account = overrides.get(
        "switch_account",
        lambda n: {"success": True, "message": f"switched to {n}"},
    )
    monkeypatch.setitem(sys.modules, "scitex_agent_container._state.account_store", mod)
    return mod


def _install_fake_credentials(monkeypatch, meta=None, raise_err=False):
    mod = types.ModuleType("scitex_agent_container._account.credentials")

    def reader(home=None):
        if raise_err:
            raise OSError("no credentials")
        return meta or {}

    mod.read_credentials_metadata = reader
    monkeypatch.setitem(sys.modules, "scitex_agent_container._account.credentials", mod)
    return mod


def _install_fake_quota(monkeypatch, **overrides):
    mod = types.ModuleType("scitex_agent_container._account.quota_watch")
    mod.check_and_rotate = overrides.get(
        "check_and_rotate",
        lambda threshold, dry_run: {"action": "ok", "message": "no rotation"},
    )
    mod.run_loop = overrides.get("run_loop", MagicMock())
    mod.survival_mode_check = overrides.get(
        "survival_mode_check", lambda: {"survival_mode": False, "message": ""}
    )
    monkeypatch.setitem(sys.modules, "scitex_agent_container._account.quota_watch", mod)
    return mod


# ---------------------------------------------------------------------------
# account save
# ---------------------------------------------------------------------------


def test_account_save_dry_run(monkeypatch, sandbox_home):
    runner = CliRunner()
    result = runner.invoke(account, ["save", "work", "--dry-run"])
    assert result.exit_code == 0
    assert "dry-run" in result.output
    assert "work" in result.output


def test_account_save_writes_meta_with_email(monkeypatch, sandbox_home):
    state = _install_fake_state(monkeypatch)
    _install_fake_credentials(monkeypatch, meta={"email_address": "x@y.z"})
    # create credentials file so the copy branch runs
    (sandbox_home / ".claude").mkdir()
    (sandbox_home / ".claude" / ".credentials.json").write_text("{}")

    runner = CliRunner()
    result = runner.invoke(account, ["save", "work", "--email", "explicit@example.com"])
    assert result.exit_code == 0, result.output
    state.save_account.assert_called_once()
    args, kwargs = state.save_account.call_args
    assert args[0] == "work"
    assert args[1] == {"email_address": "explicit@example.com"}
    assert "Saved account 'work'" in result.output


def test_account_save_autodetect_email(monkeypatch, sandbox_home):
    state = _install_fake_state(monkeypatch)
    _install_fake_credentials(monkeypatch, meta={"email_address": "auto@x.com"})
    runner = CliRunner()
    result = runner.invoke(account, ["save", "work"])
    assert result.exit_code == 0, result.output
    args, _ = state.save_account.call_args
    assert args[1] == {"email_address": "auto@x.com"}


def test_account_save_swallows_credentials_error(monkeypatch, sandbox_home):
    state = _install_fake_state(monkeypatch)
    _install_fake_credentials(monkeypatch, raise_err=True)
    runner = CliRunner()
    result = runner.invoke(account, ["save", "work"])
    assert result.exit_code == 0, result.output
    args, _ = state.save_account.call_args
    assert args[1] == {}


# ---------------------------------------------------------------------------
# account list
# ---------------------------------------------------------------------------


def test_account_list_empty(monkeypatch):
    _install_fake_state(monkeypatch, list_accounts=lambda: [])
    _install_fake_credentials(monkeypatch, meta={})
    # status_cmds._format_claude_account_block — stub
    sc_mod = types.ModuleType("scitex_agent_container.cli_pkg.status_cmds")
    sc_mod._format_claude_account_block = lambda meta: []
    monkeypatch.setitem(
        sys.modules, "scitex_agent_container.cli_pkg.status_cmds", sc_mod
    )

    runner = CliRunner()
    result = runner.invoke(account, ["list"])
    assert result.exit_code == 0
    assert "No accounts stored" in result.output


def test_account_list_with_accounts(monkeypatch):
    accts = [
        {"name": "work", "email_address": "w@x.com"},
        {"name": "personal"},
    ]
    _install_fake_state(monkeypatch, list_accounts=lambda: accts)
    _install_fake_credentials(monkeypatch, meta={"email_address": "active@x.com"})
    sc_mod = types.ModuleType("scitex_agent_container.cli_pkg.status_cmds")
    sc_mod._format_claude_account_block = lambda meta: [
        f"Active: {meta.get('email_address')}"
    ]
    monkeypatch.setitem(
        sys.modules, "scitex_agent_container.cli_pkg.status_cmds", sc_mod
    )

    runner = CliRunner()
    result = runner.invoke(account, ["list"])
    assert result.exit_code == 0, result.output
    assert "Stored accounts" in result.output
    assert "work" in result.output
    assert "personal" in result.output
    assert "(no email)" in result.output


def test_account_list_json(monkeypatch):
    _install_fake_state(monkeypatch, list_accounts=lambda: [{"name": "x"}])
    _install_fake_credentials(monkeypatch, meta={"email_address": "a@b"})
    runner = CliRunner()
    result = runner.invoke(account, ["list", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["active"] == {"email_address": "a@b"}
    assert payload["stored"] == [{"name": "x"}]


def test_account_list_json_swallows_credentials_error(monkeypatch):
    _install_fake_state(monkeypatch, list_accounts=lambda: [])
    _install_fake_credentials(monkeypatch, raise_err=True)
    runner = CliRunner()
    result = runner.invoke(account, ["list", "--json"])
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["active"] == {}


def test_account_list_human_swallows_credentials_error(monkeypatch):
    _install_fake_state(monkeypatch, list_accounts=lambda: [])
    _install_fake_credentials(monkeypatch, raise_err=True)
    sc_mod = types.ModuleType("scitex_agent_container.cli_pkg.status_cmds")
    sc_mod._format_claude_account_block = lambda meta: []
    monkeypatch.setitem(
        sys.modules, "scitex_agent_container.cli_pkg.status_cmds", sc_mod
    )
    runner = CliRunner()
    result = runner.invoke(account, ["list"])
    assert result.exit_code == 0, result.output


# ---------------------------------------------------------------------------
# account delete
# ---------------------------------------------------------------------------


def test_account_delete_dry_run(monkeypatch):
    _install_fake_state(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(account, ["delete", "work", "--dry-run"])
    assert result.exit_code == 0
    assert "dry-run" in result.output


def test_account_delete_requires_yes(monkeypatch):
    _install_fake_state(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(account, ["delete", "work"])
    assert result.exit_code == 2
    assert "Refusing" in result.output


def test_account_delete_success(monkeypatch):
    _install_fake_state(monkeypatch, delete_account=lambda n: True)
    runner = CliRunner()
    result = runner.invoke(account, ["delete", "work", "--yes"])
    assert result.exit_code == 0
    assert "Deleted" in result.output


def test_account_delete_not_found(monkeypatch):
    _install_fake_state(monkeypatch, delete_account=lambda n: False)
    runner = CliRunner()
    result = runner.invoke(account, ["delete", "ghost", "--yes"])
    assert result.exit_code == 1
    assert "not found" in result.output


# ---------------------------------------------------------------------------
# account switch
# ---------------------------------------------------------------------------


def test_account_switch_success(monkeypatch):
    _install_fake_state(
        monkeypatch,
        switch_account=lambda n: {"success": True, "message": f"now {n}"},
    )
    runner = CliRunner()
    result = runner.invoke(account, ["switch", "work"])
    assert result.exit_code == 0
    assert "now work" in result.output


def test_account_switch_failure(monkeypatch):
    _install_fake_state(
        monkeypatch,
        switch_account=lambda n: {"success": False, "message": "nope"},
    )
    runner = CliRunner()
    result = runner.invoke(account, ["switch", "missing"])
    assert result.exit_code == 1
    assert "nope" in result.output


# ---------------------------------------------------------------------------
# account watch-quota + top-level quota-watch
# ---------------------------------------------------------------------------


def test_watch_quota_once(monkeypatch):
    q = _install_fake_quota(
        monkeypatch,
        check_and_rotate=lambda threshold, dry_run: {
            "action": "rotated",
            "message": "did it",
        },
    )
    runner = CliRunner()
    result = runner.invoke(account, ["watch-quota", "--once"])
    assert result.exit_code == 0
    assert "rotated" in result.output
    q.run_loop.assert_not_called()


def test_watch_quota_dry_run_with_survival(monkeypatch):
    _install_fake_quota(
        monkeypatch,
        survival_mode_check=lambda: {"survival_mode": True, "message": "low!"},
    )
    runner = CliRunner()
    result = runner.invoke(account, ["watch-quota", "--dry-run"])
    assert result.exit_code == 0
    assert "SURVIVAL" in result.output


def test_watch_quota_daemon_runs_loop(monkeypatch):
    q = _install_fake_quota(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(account, ["watch-quota", "--daemon"])
    assert result.exit_code == 0, result.output
    assert "Forking" in result.output
    q.run_loop.assert_called_once()


def test_watch_quota_foreground_loop(monkeypatch):
    q = _install_fake_quota(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(account, ["watch-quota", "--log-file", "/tmp/x.log"])
    assert result.exit_code == 0
    q.run_loop.assert_called_once()


def test_top_level_quota_watch_once(monkeypatch):
    _install_fake_quota(
        monkeypatch,
        check_and_rotate=lambda threshold, dry_run: {"action": "noop", "message": "ok"},
    )
    runner = CliRunner()
    result = runner.invoke(quota_watch, ["--once"])
    assert result.exit_code == 0
    assert "noop" in result.output


def test_top_level_quota_watch_dry_run_survival(monkeypatch):
    _install_fake_quota(
        monkeypatch,
        survival_mode_check=lambda: {"survival_mode": True, "message": "low"},
    )
    runner = CliRunner()
    result = runner.invoke(quota_watch, ["--dry-run"])
    assert result.exit_code == 0
    assert "SURVIVAL" in result.output


def test_top_level_quota_watch_daemon(monkeypatch):
    q = _install_fake_quota(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(quota_watch, ["--daemon", "--log-file", "/tmp/q.log"])
    assert result.exit_code == 0
    assert "Forking" in result.output
    q.run_loop.assert_called_once()


def test_top_level_quota_watch_loop(monkeypatch):
    q = _install_fake_quota(monkeypatch)
    runner = CliRunner()
    result = runner.invoke(quota_watch, [])
    assert result.exit_code == 0
    q.run_loop.assert_called_once()
