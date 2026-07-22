"""The blind-pick refusal must name the remedy that fits the CAUSE.

``pick_healthy_account(require_quota_evidence=True)`` refuses when the quota
cache says nothing about the account it selected. It used to name one remedy —
"run ``sac accounts refresh-quota-cache``" — for two different causes, and for
one of them that command changes nothing:

  * the cache holds ZERO entries: the populator has never written a successful
    one, so re-running it is only the fix if accounts are stored to refresh;
  * the cache holds entries that do not cover this fleet: genuinely stale, and
    a refresh is exactly right.

Both inputs are exercised below. A message that reads the same for both would
be indistinguishable from no check at all.

No mocks: real on-disk credential snapshots and a real cache file.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from scitex_agent_container._creds._account_health import NoHealthyAccountError
from scitex_agent_container._creds._pick_healthy import pick_healthy_account

_NOW = 1_000_000.0


def _make_fresh_account(store: Path, slug: str) -> None:
    """A stored account whose OAuth snapshot is still valid at ``_NOW``."""
    acct = store / slug
    acct.mkdir(parents=True, exist_ok=True)
    creds = {
        "claudeAiOauth": {
            "accessToken": "sk-ant-FAKE-do-not-log",
            "refreshToken": "refresh-FAKE",
            "expiresAt": int((_NOW + 8 * 3600.0) * 1000),
        }
    }
    (acct / ".credentials.json").write_text(json.dumps(creds), encoding="utf-8")


def _write_cache(path: Path, accounts: dict) -> Path:
    path.write_text(
        json.dumps({"written_at": 1.0, "accounts": accounts}), encoding="utf-8"
    )
    return path


def _refuse(tmp_path: Path, cache_accounts: dict) -> str:
    """Drive the blind gate and return the refusal message."""
    store = tmp_path / "store"
    _make_fresh_account(store, "ywatanabe-scitex-ai")
    cache = _write_cache(tmp_path / "quota-cache.json", cache_accounts)
    with pytest.raises(NoHealthyAccountError) as excinfo:
        pick_healthy_account(
            "ywatanabe-scitex-ai",
            store_dir=store,
            home=tmp_path,
            now=_NOW,
            quota_cache_path=cache,
            require_quota_evidence=True,
        )
    return str(excinfo.value)


#: An entry for an account that is NOT in the store — the cache is populated,
#: just not with anything covering this fleet.
_OTHER_FLEET = {
    "someone-else-com": {"short": "someone", "h5": 4.0, "d7": 2.0, "ttl_h": 6.0}
}


# ---------------------------------------------------------------------------
# Cause A — the cache exists but holds ZERO entries
# ---------------------------------------------------------------------------


def test_empty_cache_refusal_names_sac_accounts_save(tmp_path: Path) -> None:
    # Arrange
    accounts: dict = {}
    # Act
    message = _refuse(tmp_path, accounts)
    # Assert — the remedy a refresh cannot substitute for.
    assert "sac accounts save" in message


def test_empty_cache_refusal_says_zero_entries(tmp_path: Path) -> None:
    # Arrange
    accounts: dict = {}
    # Act
    message = _refuse(tmp_path, accounts)
    # Assert
    assert "ZERO account entries" in message


def test_empty_cache_refusal_warns_the_refresh_may_not_help(tmp_path: Path) -> None:
    # Arrange
    accounts: dict = {}
    # Act
    message = _refuse(tmp_path, accounts)
    # Assert — the loop the old single-remedy text sent the operator into.
    assert "cannot help" in message


# ---------------------------------------------------------------------------
# Cause B — the cache is populated but stale for this fleet
# ---------------------------------------------------------------------------


def test_stale_cache_refusal_names_the_refresh_command(tmp_path: Path) -> None:
    # Arrange
    accounts = dict(_OTHER_FLEET)
    # Act
    message = _refuse(tmp_path, accounts)
    # Assert
    assert "sac accounts refresh-quota-cache" in message


def test_stale_cache_refusal_does_not_send_the_operator_to_save(
    tmp_path: Path,
) -> None:
    # Arrange — accounts ARE stored here; saving another would be noise.
    accounts = dict(_OTHER_FLEET)
    # Act
    message = _refuse(tmp_path, accounts)
    # Assert
    assert "sac accounts save" not in message


def test_stale_cache_refusal_reports_how_many_entries_it_saw(tmp_path: Path) -> None:
    # Arrange
    accounts = dict(_OTHER_FLEET)
    # Act
    message = _refuse(tmp_path, accounts)
    # Assert — the count is the evidence that the cache was actually read.
    assert "1 account entry" in message


def test_the_two_causes_do_not_produce_the_same_message(tmp_path: Path) -> None:
    # Arrange
    empty_dir = tmp_path / "empty"
    stale_dir = tmp_path / "stale"
    empty_dir.mkdir()
    stale_dir.mkdir()
    # Act
    empty_message = _refuse(empty_dir, {})
    stale_message = _refuse(stale_dir, dict(_OTHER_FLEET))
    # Assert — a check that says the same thing for every input checks nothing.
    assert empty_message != stale_message
