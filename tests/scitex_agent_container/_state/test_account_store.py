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

TQ cleanup: module docstring summarises intent (TQ001); every test
carries AAA markers (TQ002); descriptive names spell out the verified
behaviour (TQ003); each test asserts exactly one fact (TQ007).
Same-shape invariants over a single arrange/act collapse into
``pytest.parametrize`` cases. No mocks/monkeypatch (PA-306) — the
``_isolate_home`` fixture uses explicit ``os.environ`` save/restore.
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


# ---------------------------------------------------------------------------
# save_account / list_accounts round-trip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("name", ["alpha", "beta"])
def test_list_accounts_returns_each_saved_account_name(
    tmp_path: Path, name: str
) -> None:
    # Arrange
    home = tmp_path
    save_account("alpha", {"email_address": "a@x"}, home=home)
    save_account("beta", {"email_address": "b@x"}, home=home)

    # Act
    listed_names = sorted(a["name"] for a in list_accounts(home=home))

    # Assert
    assert name in listed_names


def test_list_accounts_skips_openai_provider_namespace(tmp_path: Path) -> None:
    # Arrange
    provider_home = (
        tmp_path
        / ".scitex"
        / "agent-container"
        / "accounts"
        / "openai"
        / "example-account"
    )
    provider_home.mkdir(parents=True)
    provider_home.joinpath("auth.json").write_text("{}")
    # Act
    accounts = list_accounts(home=tmp_path)
    # Assert
    assert accounts == []


# ---------------------------------------------------------------------------
# metadata layout — lives inside per-account dir, never at the sibling path
# ---------------------------------------------------------------------------


def test_save_account_writes_metadata_file_inside_account_dir(
    tmp_path: Path,
) -> None:
    # Arrange
    home = tmp_path

    # Act
    save_account("alpha", {"email_address": "a@x"}, home=home)

    # Assert
    meta = (
        home / ".scitex" / "agent-container" / "accounts" / "alpha" / _METADATA_FILENAME
    )
    assert meta.is_file()


@pytest.mark.parametrize(
    ("field", "expected"),
    [("name", "alpha"), ("email_address", "a@x")],
)
def test_saved_metadata_json_carries_expected_field(
    tmp_path: Path, field: str, expected: str
) -> None:
    # Arrange
    home = tmp_path
    save_account("alpha", {"email_address": "a@x"}, home=home)
    meta = (
        home / ".scitex" / "agent-container" / "accounts" / "alpha" / _METADATA_FILENAME
    )

    # Act
    payload = json.loads(meta.read_text())

    # Assert
    assert payload[field] == expected


def test_save_account_does_not_create_pre_refactor_sibling_json(
    tmp_path: Path,
) -> None:
    # Arrange
    home = tmp_path

    # Act
    save_account("alpha", {"email_address": "a@x"}, home=home)

    # Assert
    old_layout_meta = home / ".scitex" / "agent-container" / "accounts" / "alpha.json"
    assert not old_layout_meta.exists()


# ---------------------------------------------------------------------------
# switch_account — copies credentials, isolates metadata
# ---------------------------------------------------------------------------


@pytest.fixture
def _switched_alpha_home(tmp_path: Path) -> Path:
    """Arrange: save alpha + drop credentials, then perform switch."""
    home = tmp_path
    save_account("alpha", {"email_address": "a@x"}, home=home)
    acct_dir = home / ".scitex" / "agent-container" / "accounts" / "alpha"
    (acct_dir / ".credentials.json").write_text('{"claudeAiOauth": {"k": "v"}}')
    switch_account("alpha", home=home)
    return home


def test_switch_account_reports_success_true(tmp_path: Path) -> None:
    # Arrange
    home = tmp_path
    save_account("alpha", {"email_address": "a@x"}, home=home)
    acct_dir = home / ".scitex" / "agent-container" / "accounts" / "alpha"
    (acct_dir / ".credentials.json").write_text('{"claudeAiOauth": {"k": "v"}}')

    # Act
    result = switch_account("alpha", home=home)

    # Assert
    assert result["success"] is True


def test_switch_account_copies_credentials_into_claude_dir(
    _switched_alpha_home: Path,
) -> None:
    # Arrange
    home = _switched_alpha_home
    # Act
    claude_creds = home / ".claude" / ".credentials.json"
    # Assert
    assert claude_creds.is_file()


def test_switch_account_does_not_leak_metadata_into_claude_dir(
    _switched_alpha_home: Path,
) -> None:
    # Arrange
    home = _switched_alpha_home
    # Act
    leaked = home / ".claude" / _METADATA_FILENAME
    # Assert
    assert not leaked.exists()


# ---------------------------------------------------------------------------
# delete_account — removes the per-account directory; idempotent
# ---------------------------------------------------------------------------


def test_delete_account_returns_true_when_account_exists(tmp_path: Path) -> None:
    # Arrange
    home = tmp_path
    save_account("alpha", {"email_address": "a@x"}, home=home)

    # Act
    deleted = delete_account("alpha", home=home)

    # Assert
    assert deleted is True


def test_delete_account_removes_the_account_directory(tmp_path: Path) -> None:
    # Arrange
    home = tmp_path
    save_account("alpha", {"email_address": "a@x"}, home=home)
    acct_dir = home / ".scitex" / "agent-container" / "accounts" / "alpha"

    # Act
    delete_account("alpha", home=home)

    # Assert
    assert not acct_dir.exists()


def test_delete_account_returns_false_on_second_call_for_idempotency(
    tmp_path: Path,
) -> None:
    # Arrange
    home = tmp_path
    save_account("alpha", {"email_address": "a@x"}, home=home)
    delete_account("alpha", home=home)

    # Act
    second = delete_account("alpha", home=home)

    # Assert
    assert second is False


# ---------------------------------------------------------------------------
# Short-name alias (~/.scitex/sac -> agent-container)
# ---------------------------------------------------------------------------


def test_first_save_creates_short_name_alias_as_symlink(tmp_path: Path) -> None:
    # Arrange
    home = tmp_path
    short = home / ".scitex" / _SHORT_ROOT_NAME

    # Act
    save_account("alpha", {"email_address": "a@x"}, home=home)

    # Assert
    assert short.is_symlink()


def test_short_name_alias_points_to_canonical_root_name(tmp_path: Path) -> None:
    # Arrange
    home = tmp_path

    # Act
    save_account("alpha", {"email_address": "a@x"}, home=home)

    # Assert
    short = home / ".scitex" / _SHORT_ROOT_NAME
    assert short.readlink() == Path(_CANONICAL_ROOT_NAME)


def test_account_metadata_is_reachable_through_short_name_alias(
    tmp_path: Path,
) -> None:
    # Arrange
    home = tmp_path

    # Act
    save_account("alpha", {"email_address": "a@x"}, home=home)

    # Assert
    via_alias = (
        home / ".scitex" / _SHORT_ROOT_NAME / "accounts" / "alpha" / _METADATA_FILENAME
    )
    assert via_alias.is_file()


def test_save_does_not_clobber_existing_real_short_name_directory(
    tmp_path: Path,
) -> None:
    # Arrange
    home = tmp_path
    short = home / ".scitex" / _SHORT_ROOT_NAME
    short.mkdir(parents=True)

    # Act
    save_account("alpha", {"email_address": "a@x"}, home=home)

    # Assert
    assert not short.is_symlink()


def test_existing_real_short_name_directory_remains_a_directory_after_save(
    tmp_path: Path,
) -> None:
    # Arrange
    home = tmp_path
    short = home / ".scitex" / _SHORT_ROOT_NAME
    short.mkdir(parents=True)

    # Act
    save_account("alpha", {"email_address": "a@x"}, home=home)

    # Assert
    assert short.is_dir()
