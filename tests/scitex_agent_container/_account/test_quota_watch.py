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
    """Create a fake account store populated with accounts."""
    store = tmp_path / "store"
    store.mkdir()
    for acct in accounts:
        name = acct["name"]
        cred_dir = store / name
        cred_dir.mkdir()
        (cred_dir / "account.json").write_text(json.dumps(acct))
        (cred_dir / ".credentials.json").write_text(json.dumps({"claudeAiOauth": {}}))
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


def test_select_next_account_skips_account_matching_current_email():
    # Arrange
    accounts = [
        {"name": "a", "email_address": "a@x.com", "quota_5h_used_pct": 50.0},
        {"name": "b", "email_address": "b@x.com", "quota_5h_used_pct": 10.0},
    ]
    # Act
    result = _select_next_account(accounts, current_email="a@x.com")
    # Assert
    assert result["name"] == "b"


def test_select_next_account_returns_none_when_only_current_account_present():
    # Arrange
    accounts = [{"name": "only", "email_address": "only@x.com"}]
    # Act
    result = _select_next_account(accounts, current_email="only@x.com")
    # Assert
    assert result is None


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
    assert "cannot rotate" in result["message"]


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
