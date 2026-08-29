"""``sac accounts refresh-quota-cache`` against an EMPTY account store.

The case the command used to collapse into success. Its all-failed guard read
``attempted > 0 and ok == 0``; with no accounts stored ``attempted == 0``, so
the guard was skipped and the run exited 0 after writing an empty cache. An
operator following the boot picker's "run `sac accounts refresh-quota-cache`"
hint therefore saw "0 failed", concluded the cache was populated, and hit the
identical refusal on retry.

No mocks: a real (empty) account store via ``$HOME`` redirection, and a real
``--cache-path``. The counter-input section at the bottom runs the SAME
command against a store that DOES hold an account (one with no credentials
snapshot, so nothing reaches the network) and pins the different answer — an
exit-code assertion that cannot distinguish the two inputs proves nothing.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from click.testing import CliRunner

from scitex_agent_container._state.account_store import save_account
from scitex_agent_container.cli_pkg._account_refresh_quota_cache import (
    EXIT_NO_ACCOUNTS,
)
from scitex_agent_container.cli_pkg.account_group import account


@pytest.fixture(autouse=True)
def sandbox_home(tmp_path, env_save_restore) -> Path:
    """Redirect ``$HOME`` so the account store resolves inside ``tmp_path``."""
    home = tmp_path / "home"
    home.mkdir()
    env_save_restore.set("HOME", str(home))
    env_save_restore.delete("SAC_QUOTA_CACHE_PATH")
    return home


def _invoke(cache: Path, *extra: str):
    return CliRunner().invoke(
        account, ["refresh-quota-cache", "--cache-path", str(cache), *extra]
    )


# ---------------------------------------------------------------------------
# Exit code — "found nothing" is not "refreshed"
# ---------------------------------------------------------------------------


def test_empty_store_does_not_exit_zero(tmp_path: Path) -> None:
    # Arrange — the whole point: exit 0 here is the defect.
    cache = tmp_path / "quota-cache.json"
    # Act
    result = _invoke(cache)
    # Assert
    assert result.exit_code != 0


def test_empty_store_exits_with_the_no_accounts_code(tmp_path: Path) -> None:
    # Arrange
    cache = tmp_path / "quota-cache.json"
    # Act
    result = _invoke(cache)
    # Assert — distinct from 1 (every account failed) and from click's 2.
    assert result.exit_code == EXIT_NO_ACCOUNTS


def test_no_accounts_code_does_not_collide_with_click_usage_error() -> None:
    # Arrange — a caller branching on the exit code must be able to tell an
    # empty store from a mistyped flag; click spends 2 on UsageError.
    bad = CliRunner().invoke(account, ["refresh-quota-cache", "--nope"])
    # Act
    collides = bad.exit_code == EXIT_NO_ACCOUNTS
    # Assert
    assert not collides


# ---------------------------------------------------------------------------
# The cache file itself
# ---------------------------------------------------------------------------


def test_empty_store_creates_no_cache_file(tmp_path: Path) -> None:
    # Arrange
    cache = tmp_path / "quota-cache.json"
    # Act
    _invoke(cache)
    # Assert
    assert not cache.exists()


def test_empty_store_leaves_an_existing_cache_byte_identical(tmp_path: Path) -> None:
    # Arrange
    cache = tmp_path / "quota-cache.json"
    before = '{"written_at": 1.0, "accounts": {"a": {"short": "a"}}}'
    cache.write_text(before, encoding="utf-8")
    # Act
    _invoke(cache)
    # Assert
    assert cache.read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# What the operator is told
# ---------------------------------------------------------------------------


def test_empty_store_names_the_remedy_that_can_actually_help(tmp_path: Path) -> None:
    # Arrange
    cache = tmp_path / "quota-cache.json"
    # Act
    result = _invoke(cache)
    # Assert
    assert "sac accounts save" in result.output


def test_empty_store_never_claims_it_wrote_the_cache(tmp_path: Path) -> None:
    # Arrange — the old text said "wrote empty cache to <path>", which is both
    # a claim of success and (under merge) not even true.
    cache = tmp_path / "quota-cache.json"
    # Act
    result = _invoke(cache)
    # Assert
    assert "wrote" not in result.output.lower()


def test_empty_store_json_reports_written_false(tmp_path: Path) -> None:
    # Arrange
    import json

    cache = tmp_path / "quota-cache.json"
    # Act
    result = _invoke(cache, "--json")
    payload = json.loads(result.stdout)
    # Assert
    assert payload["written"] is False


def test_empty_store_json_reports_the_no_accounts_reason(tmp_path: Path) -> None:
    # Arrange
    import json

    cache = tmp_path / "quota-cache.json"
    # Act
    result = _invoke(cache, "--json")
    payload = json.loads(result.stdout)
    # Assert
    assert payload["reason"] == "no-accounts"


# ---------------------------------------------------------------------------
# Counter-input — the SAME command with ONE account stored
#
# The account has no credentials snapshot, so `_refresh_one` short-circuits
# before any usage fetch: offline, deterministic, and still an "attempted"
# account. Every assertion here must come out DIFFERENT from its empty-store
# twin above, or those assertions were never reading the store at all.
# ---------------------------------------------------------------------------


def test_stored_account_does_not_get_the_no_accounts_code(
    tmp_path: Path, sandbox_home: Path
) -> None:
    # Arrange
    save_account("a-gmail-com", {"email_address": "a@x"}, home=sandbox_home)
    cache = tmp_path / "quota-cache.json"
    # Act
    result = _invoke(cache)
    # Assert — every account failed, which is exit 1, NOT the empty-store code.
    assert result.exit_code == 1


def test_stored_account_still_writes_the_cache(
    tmp_path: Path, sandbox_home: Path
) -> None:
    # Arrange — the write must survive for the merge to keep meaning anything.
    save_account("a-gmail-com", {"email_address": "a@x"}, home=sandbox_home)
    cache = tmp_path / "quota-cache.json"
    # Act
    _invoke(cache)
    # Assert
    assert cache.is_file()


def test_stored_account_output_is_not_the_no_accounts_message(
    tmp_path: Path, sandbox_home: Path
) -> None:
    # Arrange
    save_account("a-gmail-com", {"email_address": "a@x"}, home=sandbox_home)
    cache = tmp_path / "quota-cache.json"
    # Act
    result = _invoke(cache)
    # Assert
    assert "sac accounts save" not in result.output
