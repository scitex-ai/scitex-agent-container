"""Tests for quota_watch.check_and_rotate.

PA-306: no ``unittest.mock``. ``fetch_usage`` is swapped at module level
via a hand-rolled context manager (save/restore the attribute) — same
effect as ``monkeypatch.setattr`` but no banned imports or fixtures.

TQ cleanup: module docstring summarises intent (TQ001), every test carries
AAA markers (TQ002), descriptive names spell out the behaviour being
verified (TQ003), and each test asserts exactly one fact (TQ007).
Same-shape result-field invariants over a single arrange/act collapse into
``pytest.parametrize`` over ``(key, expected)`` pairs.
"""

from __future__ import annotations

import json
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import pytest

import scitex_agent_container._account.quota_watch as qw_mod
from scitex_agent_container._account.quota_watch import (
    _DEFAULT_LOG_PATH,
    _select_next_account,
    check_and_rotate,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@contextmanager
def _fake_fetch_usage(payload: dict[str, Any]) -> Iterator[None]:
    """Swap ``qw_mod.fetch_usage`` for a stub returning ``payload``."""
    saved = qw_mod.fetch_usage
    qw_mod.fetch_usage = lambda *_a, **_kw: payload  # type: ignore[assignment]
    try:
        yield
    finally:
        qw_mod.fetch_usage = saved  # type: ignore[assignment]


def _make_home(tmp_path: Path, email: str = "test@example.com") -> Path:
    """Create a minimal fake home with .claude.json."""
    home = tmp_path / "home"
    home.mkdir()
    claude_json = {"oauthAccount": {"emailAddress": email}}
    (home / ".claude.json").write_text(json.dumps(claude_json))
    return home


def _make_store(tmp_path: Path, accounts: list[dict]) -> Path:
    """Create a fake account store populated with accounts.

    Each account dict may carry a ``_health`` control key (stripped before
    it is written to ``account.json``):

      * ``"valid"``  (default) — writes a ``.credentials.json`` whose
        ``claudeAiOauth.expiresAt`` is 30 days in the FUTURE → healthy.
      * ``"expired"`` — writes a snapshot whose ``expiresAt`` is in the
        PAST → unhealthy (the exact shape of the 2026-07-06 bug).
      * ``"absent"`` — writes NO ``.credentials.json`` → unhealthy.

    Freshness is what :func:`_creds._pick_healthy.account_health` reads, so
    this seam lets the tests exercise the health gate without any network.
    """
    future_ms = int((time.time() + 30 * 24 * 3600) * 1000)
    past_ms = int((time.time() - 24 * 3600) * 1000)
    store = tmp_path / "store"
    store.mkdir()
    for raw in accounts:
        acct = dict(raw)  # copy: never mutate the caller's dict
        health = acct.pop("_health", "valid")
        name = acct["name"]
        cred_dir = store / name
        cred_dir.mkdir()
        (cred_dir / "account.json").write_text(json.dumps(acct))
        if health == "absent":
            continue  # no snapshot on disk → ABSENT → unhealthy
        expires = future_ms if health == "valid" else past_ms
        (cred_dir / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"expiresAt": expires}})
        )
    return store


# ---------------------------------------------------------------------------
# check_and_rotate — below threshold, no rotation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("action", "ok"),
        ("switched_to", None),
        ("quota_5h_pct", 30.0),
    ],
)
def test_check_and_rotate_below_threshold_returns_ok_status(tmp_path, field, expected):
    # Arrange
    home = _make_home(tmp_path)
    # Act
    with _fake_fetch_usage({"used_pct_5h": 30.0, "used_pct_7d": 25.0, "error": None}):
        result = check_and_rotate(threshold=80.0, home=home)
    # Assert
    assert result[field] == expected


# ---------------------------------------------------------------------------
# check_and_rotate — over threshold, no stored accounts to rotate to
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("action", "no_accounts"),
        ("switched_to", None),
    ],
)
def test_check_and_rotate_over_threshold_without_accounts_reports_no_accounts(
    tmp_path, field, expected
):
    # Arrange
    home = _make_home(tmp_path)
    store = tmp_path / "empty_store"
    store.mkdir()
    # Act
    with _fake_fetch_usage({"used_pct_5h": 90.0, "used_pct_7d": 50.0, "error": None}):
        result = check_and_rotate(threshold=80.0, store_dir=store, home=home)
    # Assert
    assert result[field] == expected


def test_check_and_rotate_over_threshold_without_accounts_emits_alert_in_message(
    tmp_path,
):
    # Arrange
    home = _make_home(tmp_path)
    store = tmp_path / "empty_store"
    store.mkdir()
    # Act
    with _fake_fetch_usage({"used_pct_5h": 90.0, "used_pct_7d": 50.0, "error": None}):
        result = check_and_rotate(threshold=80.0, store_dir=store, home=home)
    # Assert
    assert "ALERT" in result["message"]


# ---------------------------------------------------------------------------
# check_and_rotate — dry-run rotation
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("action", "rotated(dry_run)"),
        ("switched_to", "secondary"),
        ("quota_5h_pct", 85.0),
    ],
)
def test_check_and_rotate_dry_run_picks_secondary_account_without_switching(
    tmp_path, field, expected
):
    # Arrange
    home = _make_home(tmp_path, email="primary@example.com")
    store = _make_store(
        tmp_path,
        [{"name": "secondary", "email_address": "secondary@example.com"}],
    )
    # Act
    with _fake_fetch_usage({"used_pct_5h": 85.0, "used_pct_7d": 40.0, "error": None}):
        result = check_and_rotate(
            threshold=80.0, store_dir=store, home=home, dry_run=True
        )
    # Assert
    assert result[field] == expected


# ---------------------------------------------------------------------------
# check_and_rotate — fetch_usage reports an error
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("action", "error"),
        ("quota_5h_pct", None),
    ],
)
def test_check_and_rotate_propagates_fetch_usage_error_status(
    tmp_path, field, expected
):
    # Arrange
    home = _make_home(tmp_path)
    payload = {
        "used_pct_5h": None,
        "used_pct_7d": None,
        "error": "No access token found",
    }
    # Act
    with _fake_fetch_usage(payload):
        result = check_and_rotate(threshold=80.0, home=home)
    # Assert
    assert result[field] == expected


def test_check_and_rotate_includes_fetch_usage_error_text_in_message(tmp_path):
    # Arrange
    home = _make_home(tmp_path)
    payload = {
        "used_pct_5h": None,
        "used_pct_7d": None,
        "error": "No access token found",
    }
    # Act
    with _fake_fetch_usage(payload):
        result = check_and_rotate(threshold=80.0, home=home)
    # Assert
    assert "No access token found" in result["message"]


# ---------------------------------------------------------------------------
# check_and_rotate — actual rotation (over threshold, account available)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("action", "rotated"),
        ("switched_to", "backup"),
    ],
)
def test_check_and_rotate_over_threshold_rotates_to_backup_account(
    tmp_path, field, expected
):
    # Arrange
    home = _make_home(tmp_path, email="primary@example.com")
    (home / ".claude").mkdir()
    store = _make_store(
        tmp_path,
        [{"name": "backup", "email_address": "backup@example.com"}],
    )
    # Act
    with _fake_fetch_usage({"used_pct_5h": 92.0, "used_pct_7d": 70.0, "error": None}):
        result = check_and_rotate(threshold=80.0, store_dir=store, home=home)
    # Assert
    assert result[field] == expected


def test_check_and_rotate_rotation_message_includes_current_5h_usage_value(tmp_path):
    # Arrange
    home = _make_home(tmp_path, email="primary@example.com")
    (home / ".claude").mkdir()
    store = _make_store(
        tmp_path,
        [{"name": "backup", "email_address": "backup@example.com"}],
    )
    # Act
    with _fake_fetch_usage({"used_pct_5h": 92.0, "used_pct_7d": 70.0, "error": None}):
        result = check_and_rotate(threshold=80.0, store_dir=store, home=home)
    # Assert
    assert "92.0" in result["message"]


# ---------------------------------------------------------------------------
# check_and_rotate — warning level (above 75% of threshold, below threshold)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "expected"),
    [
        ("action", "warning"),
        ("switched_to", None),
    ],
)
def test_check_and_rotate_between_warn_and_threshold_returns_warning(
    tmp_path, field, expected
):
    # Arrange
    # 75% of 80 = 60; usage at 65% should trigger warning
    home = _make_home(tmp_path)
    # Act
    with _fake_fetch_usage({"used_pct_5h": 65.0, "used_pct_7d": 10.0, "error": None}):
        result = check_and_rotate(threshold=80.0, home=home)
    # Assert
    assert result[field] == expected


# ---------------------------------------------------------------------------
# _select_next_account — selection logic
# ---------------------------------------------------------------------------


def test_select_next_account_skips_account_matching_current_email(tmp_path):
    # Arrange
    accounts = [
        {"name": "a", "email_address": "a@x.com", "quota_5h_used_pct": 50.0},
        {"name": "b", "email_address": "b@x.com", "quota_5h_used_pct": 10.0},
    ]
    store = _make_store(tmp_path, accounts)
    # Act
    result = _select_next_account(accounts, current_email="a@x.com", store_dir=store)
    # Assert
    assert result["name"] == "b"


def test_select_next_account_returns_none_when_only_current_account_present(tmp_path):
    # Arrange
    accounts = [{"name": "only", "email_address": "only@x.com"}]
    store = _make_store(tmp_path, accounts)
    # Act
    result = _select_next_account(accounts, current_email="only@x.com", store_dir=store)
    # Assert
    assert result is None


# ---------------------------------------------------------------------------
# _select_next_account — credential-health gate (2026-07-06 regression)
# ---------------------------------------------------------------------------


def test_select_next_account_excludes_expired_account_even_at_zero_quota(tmp_path):
    # Arrange — the EXACT bug: an EXPIRED account reads 0.0% and would win
    # on pure-quota ordering, but must be excluded as unhealthy.
    accounts = [
        {
            "name": "expired",
            "email_address": "expired@x.com",
            "quota_5h_used_pct": 0.0,
            "_health": "expired",
        },
        {
            "name": "healthy",
            "email_address": "healthy@x.com",
            "quota_5h_used_pct": 40.0,
        },
    ]
    store = _make_store(tmp_path, accounts)
    # Act
    result = _select_next_account(
        accounts, current_email="cur@x.com", store_dir=store
    )
    # Assert
    assert result["name"] == "healthy"


def test_select_next_account_picks_lowest_quota_among_healthy(tmp_path):
    # Arrange
    accounts = [
        {"name": "high", "email_address": "high@x.com", "quota_5h_used_pct": 70.0},
        {"name": "low", "email_address": "low@x.com", "quota_5h_used_pct": 5.0},
        {
            "name": "expired-zero",
            "email_address": "ez@x.com",
            "quota_5h_used_pct": 0.0,
            "_health": "expired",
        },
    ]
    store = _make_store(tmp_path, accounts)
    # Act
    result = _select_next_account(
        accounts, current_email="cur@x.com", store_dir=store
    )
    # Assert
    assert result["name"] == "low"


def test_select_next_account_returns_none_when_all_non_current_unhealthy(tmp_path):
    # Arrange — every non-current candidate is expired or absent.
    accounts = [
        {"name": "cur", "email_address": "cur@x.com", "quota_5h_used_pct": 90.0},
        {
            "name": "dead",
            "email_address": "dead@x.com",
            "quota_5h_used_pct": 0.0,
            "_health": "expired",
        },
        {
            "name": "gone",
            "email_address": "gone@x.com",
            "quota_5h_used_pct": 0.0,
            "_health": "absent",
        },
    ]
    store = _make_store(tmp_path, accounts)
    # Act
    result = _select_next_account(
        accounts, current_email="cur@x.com", store_dir=store
    )
    # Assert
    assert result is None


def test_select_next_account_never_selects_current_even_if_lowest(tmp_path):
    # Arrange — current account has the lowest quota AND is healthy.
    accounts = [
        {"name": "cur", "email_address": "cur@x.com", "quota_5h_used_pct": 1.0},
        {"name": "other", "email_address": "other@x.com", "quota_5h_used_pct": 80.0},
    ]
    store = _make_store(tmp_path, accounts)
    # Act
    result = _select_next_account(
        accounts, current_email="cur@x.com", store_dir=store
    )
    # Assert
    assert result["name"] == "other"


def test_check_and_rotate_over_threshold_excludes_expired_backup(tmp_path):
    # Arrange — over threshold, but the ONLY other account has an expired
    # credential at 0% quota. End-to-end guard against the live bug: stay
    # put, never rotate to the dead token.
    home = _make_home(tmp_path, email="primary@example.com")
    (home / ".claude").mkdir()
    store = _make_store(
        tmp_path,
        [
            {
                "name": "expired-backup",
                "email_address": "backup@example.com",
                "quota_5h_used_pct": 0.0,
                "_health": "expired",
            }
        ],
    )
    # Act
    with _fake_fetch_usage({"used_pct_5h": 92.0, "used_pct_7d": 70.0, "error": None}):
        result = check_and_rotate(threshold=80.0, store_dir=store, home=home)
    # Assert
    assert result["action"] == "no_accounts"


def test_check_and_rotate_over_threshold_expired_backup_does_not_switch(tmp_path):
    # Arrange
    home = _make_home(tmp_path, email="primary@example.com")
    (home / ".claude").mkdir()
    store = _make_store(
        tmp_path,
        [
            {
                "name": "expired-backup",
                "email_address": "backup@example.com",
                "quota_5h_used_pct": 0.0,
                "_health": "expired",
            }
        ],
    )
    # Act
    with _fake_fetch_usage({"used_pct_5h": 92.0, "used_pct_7d": 70.0, "error": None}):
        result = check_and_rotate(threshold=80.0, store_dir=store, home=home)
    # Assert
    assert result["switched_to"] is None


# ---------------------------------------------------------------------------
# check_and_rotate — only the current account is stored
# ---------------------------------------------------------------------------


def test_check_and_rotate_only_current_account_stored_reports_no_accounts(tmp_path):
    # Arrange
    home = _make_home(tmp_path, email="primary@example.com")
    store = _make_store(
        tmp_path,
        [{"name": "primary", "email_address": "primary@example.com"}],
    )
    # Act
    with _fake_fetch_usage({"used_pct_5h": 92.0, "used_pct_7d": 70.0, "error": None}):
        result = check_and_rotate(threshold=80.0, store_dir=store, home=home)
    # Assert
    assert result["action"] == "no_accounts"


def test_check_and_rotate_only_current_account_message_mentions_cannot_rotate(tmp_path):
    # Arrange
    home = _make_home(tmp_path, email="primary@example.com")
    store = _make_store(
        tmp_path,
        [{"name": "primary", "email_address": "primary@example.com"}],
    )
    # Act
    with _fake_fetch_usage({"used_pct_5h": 92.0, "used_pct_7d": 70.0, "error": None}):
        result = check_and_rotate(threshold=80.0, store_dir=store, home=home)
    # Assert
    assert "staying put" in result["message"]


# ---------------------------------------------------------------------------
# check_and_rotate — unexpected exception path returns error dict
# ---------------------------------------------------------------------------


@contextmanager
def _exploding_fetch_usage() -> Iterator[None]:
    """Swap ``qw_mod.fetch_usage`` for a stub that raises an exception."""
    saved = qw_mod.fetch_usage

    def _boom(*_a, **_kw):
        raise RuntimeError("boom from fetch_usage")

    qw_mod.fetch_usage = _boom  # type: ignore[assignment]
    try:
        yield
    finally:
        qw_mod.fetch_usage = saved  # type: ignore[assignment]


def test_check_and_rotate_unexpected_exception_returns_error_action(tmp_path):
    # Arrange
    home = _make_home(tmp_path)
    # Act
    with _exploding_fetch_usage():
        result = check_and_rotate(threshold=80.0, home=home)
    # Assert
    assert result["action"] == "error"


def test_check_and_rotate_unexpected_exception_message_quotes_exception_text(tmp_path):
    # Arrange
    home = _make_home(tmp_path)
    # Act
    with _exploding_fetch_usage():
        result = check_and_rotate(threshold=80.0, home=home)
    # Assert
    assert "boom from fetch_usage" in result["message"]


# ---------------------------------------------------------------------------
# survival_mode_check
# ---------------------------------------------------------------------------


def test_survival_mode_check_single_account_high_quota_flags_survival(tmp_path):
    # Arrange
    home = _make_home(tmp_path, email="primary@example.com")
    store = _make_store(
        tmp_path,
        [{"name": "primary", "email_address": "primary@example.com"}],
    )
    # Act
    with _fake_fetch_usage({"used_pct_5h": 75.0, "used_pct_7d": 20.0, "error": None}):
        result = qw_mod.survival_mode_check(store_dir=store, home=home)
    # Assert
    assert result["survival_mode"] is True


def test_survival_mode_check_single_account_high_quota_message_contains_marker(
    tmp_path,
):
    # Arrange
    home = _make_home(tmp_path, email="primary@example.com")
    store = _make_store(
        tmp_path,
        [{"name": "primary", "email_address": "primary@example.com"}],
    )
    # Act
    with _fake_fetch_usage({"used_pct_5h": 75.0, "used_pct_7d": 20.0, "error": None}):
        result = qw_mod.survival_mode_check(store_dir=store, home=home)
    # Assert
    assert "SURVIVAL MODE" in result["message"]


def test_survival_mode_check_multi_account_does_not_flag_survival(tmp_path):
    # Arrange
    home = _make_home(tmp_path, email="primary@example.com")
    store = _make_store(
        tmp_path,
        [
            {"name": "primary", "email_address": "primary@example.com"},
            {"name": "backup", "email_address": "backup@example.com"},
        ],
    )
    # Act
    with _fake_fetch_usage({"used_pct_5h": 99.0, "used_pct_7d": 10.0, "error": None}):
        result = qw_mod.survival_mode_check(store_dir=store, home=home)
    # Assert
    assert result["survival_mode"] is False


def test_survival_mode_check_low_quota_does_not_flag_survival(tmp_path):
    # Arrange
    home = _make_home(tmp_path, email="primary@example.com")
    store = _make_store(
        tmp_path,
        [{"name": "primary", "email_address": "primary@example.com"}],
    )
    # Act
    with _fake_fetch_usage({"used_pct_5h": 10.0, "used_pct_7d": 5.0, "error": None}):
        result = qw_mod.survival_mode_check(store_dir=store, home=home)
    # Assert
    assert result["survival_mode"] is False


def test_survival_mode_check_ok_message_includes_account_count(tmp_path):
    # Arrange
    home = _make_home(tmp_path, email="primary@example.com")
    store = _make_store(
        tmp_path,
        [{"name": "primary", "email_address": "primary@example.com"}],
    )
    # Act
    with _fake_fetch_usage({"used_pct_5h": 10.0, "used_pct_7d": 5.0, "error": None}):
        result = qw_mod.survival_mode_check(store_dir=store, home=home)
    # Assert
    assert "1 account" in result["message"]


def test_survival_mode_check_exception_returns_safe_default(tmp_path):
    # Arrange
    home = _make_home(tmp_path)
    # Act
    with _exploding_fetch_usage():
        result = qw_mod.survival_mode_check(home=home)
    # Assert
    assert result["survival_mode"] is False


def test_survival_mode_check_exception_message_quotes_exception_text(tmp_path):
    # Arrange
    home = _make_home(tmp_path)
    # Act
    with _exploding_fetch_usage():
        result = qw_mod.survival_mode_check(home=home)
    # Assert
    assert "boom from fetch_usage" in result["message"]


def test_default_log_path_lives_under_agent_container():
    # Arrange
    expected_suffix = Path("agent-container") / "logs" / "quota-watch.log"
    # Act
    actual_suffix = Path(*_DEFAULT_LOG_PATH.parts[-3:])
    # Assert
    assert actual_suffix == expected_suffix
