"""Tests for the account_store layout and the ~/.scitex/sac short-name alias.

Every test here MUST sandbox `Path.home()` — the helpers fall back to
the real `Path.home()` when no `home=` override applies, and a single
regression in `_store_path` would otherwise pollute the operator's
real ``~/.scitex/agent-container/accounts/`` with test fixtures like
"alpha"/"beta". The autouse ``_isolate_home`` fixture below
monkey-patches `Path.home` for the duration of every test so even a
regressed code path lands the writes under ``tmp_path``.

Historical bug: between the v3 spec realignment commit (Agent A's
home-arg refactor) and the follow-up `if home != Path.home()` guard,
this fixture didn't exist; "alpha" + "beta" were created in the real
home and shipped to the operator. This fixture is the belt to the
guard's suspenders.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scitex_agent_container._state.account_store import (
    _CANONICAL_ROOT_NAME,
    _METADATA_FILENAME,
    _SHORT_ROOT_NAME,
    delete_account,
    list_accounts,
    save_account,
    switch_account,
)


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path: Path):
    """Force Path.home() to point inside tmp_path for the test's duration.

    Belt-and-suspenders: even if account_store's `home=...` plumbing
    regresses, no test write can ever land outside tmp_path. PA-306:
    no `monkeypatch.setattr` — Path.home() reads $HOME on Unix, so
    explicit env save/restore is the real equivalent.
    """
    import os

    saved = os.environ.get("HOME")
    os.environ["HOME"] = str(tmp_path)
    try:
        yield
    finally:
        if saved is None:
            os.environ.pop("HOME", None)
        else:
            os.environ["HOME"] = saved


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
