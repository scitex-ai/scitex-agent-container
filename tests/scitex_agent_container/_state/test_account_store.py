"""Tests for the account_store layout and the ~/.scitex/sac short-name alias."""

from __future__ import annotations

import json
from pathlib import Path

from scitex_agent_container._state.account_store import (
    _CANONICAL_ROOT_NAME,
    _METADATA_FILENAME,
    _SHORT_ROOT_NAME,
    delete_account,
    list_accounts,
    save_account,
    switch_account,
)


def test_save_then_list_round_trip(tmp_path: Path) -> None:
    home = tmp_path
    save_account("alpha", {"email_address": "a@x"}, home=home)
    save_account("beta", {"email_address": "b@x"}, home=home)
    listed = list_accounts(home=home)
    names = sorted(a["name"] for a in listed)
    assert names == ["alpha", "beta"]


def test_metadata_lives_inside_account_dir(tmp_path: Path) -> None:
    home = tmp_path
    save_account("alpha", {"email_address": "a@x"}, home=home)
    meta = (
        home / ".scitex" / "agent-container" / "accounts" / "alpha" / _METADATA_FILENAME
    )
    assert meta.is_file()
    payload = json.loads(meta.read_text())
    assert payload["name"] == "alpha"
    assert payload["email_address"] == "a@x"
    # The pre-refactor sibling shape must NOT exist anymore
    old_layout_meta = home / ".scitex" / "agent-container" / "accounts" / "alpha.json"
    assert not old_layout_meta.exists()


def test_switch_copies_credentials_but_not_metadata(tmp_path: Path) -> None:
    home = tmp_path
    save_account("alpha", {"email_address": "a@x"}, home=home)
    acct_dir = home / ".scitex" / "agent-container" / "accounts" / "alpha"
    (acct_dir / ".credentials.json").write_text('{"claudeAiOauth": {"k": "v"}}')

    result = switch_account("alpha", home=home)
    assert result["success"] is True

    claude_creds = home / ".claude" / ".credentials.json"
    assert claude_creds.is_file()
    # Metadata must never leak into ~/.claude/
    assert not (home / ".claude" / _METADATA_FILENAME).exists()


def test_delete_removes_account_dir(tmp_path: Path) -> None:
    home = tmp_path
    save_account("alpha", {"email_address": "a@x"}, home=home)
    acct_dir = home / ".scitex" / "agent-container" / "accounts" / "alpha"
    assert acct_dir.is_dir()
    assert delete_account("alpha", home=home) is True
    assert not acct_dir.exists()
    assert delete_account("alpha", home=home) is False  # idempotent


def test_short_name_alias_created_on_first_save(tmp_path: Path) -> None:
    home = tmp_path
    short = home / ".scitex" / _SHORT_ROOT_NAME
    assert not short.exists()
    save_account("alpha", {"email_address": "a@x"}, home=home)
    assert short.is_symlink()
    assert short.readlink() == Path(_CANONICAL_ROOT_NAME)
    # Reading through the alias yields the same account
    via_alias = short / "accounts" / "alpha" / _METADATA_FILENAME
    assert via_alias.is_file()


def test_short_name_alias_not_created_when_real_dir_exists(tmp_path: Path) -> None:
    home = tmp_path
    short = home / ".scitex" / _SHORT_ROOT_NAME
    short.mkdir(parents=True)
    save_account("alpha", {"email_address": "a@x"}, home=home)
    # Refuses to clobber a real directory
    assert short.is_dir() and not short.is_symlink()
