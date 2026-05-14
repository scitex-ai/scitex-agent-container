"""Tests for quota_watch.check_and_rotate.

PA-306: no `unittest.mock`. `fetch_usage` is swapped at module level
via a hand-rolled fake (save/restore the attribute) — same effect as
``monkeypatch.setattr`` but no banned imports or fixtures.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

import scitex_agent_container._account.quota_watch as qw_mod
from scitex_agent_container._account.quota_watch import check_and_rotate

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
    claude_json = {
        "oauthAccount": {
            "emailAddress": email,
        }
    }
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
# Tests
# ---------------------------------------------------------------------------


def test_ok_no_rotation(tmp_path):
    home = _make_home(tmp_path)
    with _fake_fetch_usage({"used_pct_5h": 30.0, "used_pct_7d": 25.0, "error": None}):
        result = check_and_rotate(threshold=80.0, home=home)
    assert result["action"] == "ok"
    assert result["switched_to"] is None
    assert result["quota_5h_pct"] == 30.0


def test_no_accounts_alert(tmp_path):
    home = _make_home(tmp_path)
    store = tmp_path / "empty_store"
    store.mkdir()
    with _fake_fetch_usage({"used_pct_5h": 90.0, "used_pct_7d": 50.0, "error": None}):
        result = check_and_rotate(threshold=80.0, store_dir=store, home=home)
    assert result["action"] == "no_accounts"
    assert result["switched_to"] is None
    assert "ALERT" in result["message"]


def test_dry_run_rotation(tmp_path):
    home = _make_home(tmp_path, email="primary@example.com")
    store = _make_store(
        tmp_path,
        [{"name": "secondary", "email_address": "secondary@example.com"}],
    )
    with _fake_fetch_usage({"used_pct_5h": 85.0, "used_pct_7d": 40.0, "error": None}):
        result = check_and_rotate(
            threshold=80.0, store_dir=store, home=home, dry_run=True
        )
    assert result["action"] == "rotated(dry_run)"
    assert result["switched_to"] == "secondary"
    assert result["quota_5h_pct"] == 85.0


def test_error_handled(tmp_path):
    home = _make_home(tmp_path)
    with _fake_fetch_usage(
        {
            "used_pct_5h": None,
            "used_pct_7d": None,
            "error": "No access token found",
        }
    ):
        result = check_and_rotate(threshold=80.0, home=home)
    assert result["action"] == "error"
    assert "No access token found" in result["message"]
    assert result["quota_5h_pct"] is None


def test_actual_rotation(tmp_path):
    home = _make_home(tmp_path, email="primary@example.com")
    (home / ".claude").mkdir()
    store = _make_store(
        tmp_path,
        [{"name": "backup", "email_address": "backup@example.com"}],
    )
    with _fake_fetch_usage({"used_pct_5h": 92.0, "used_pct_7d": 70.0, "error": None}):
        result = check_and_rotate(threshold=80.0, store_dir=store, home=home)
    assert result["action"] == "rotated"
    assert result["switched_to"] == "backup"
    assert "92.0" in result["message"]


def test_warning_level(tmp_path):
    home = _make_home(tmp_path)
    # 75% of 80 = 60; usage at 65% should trigger warning
    with _fake_fetch_usage({"used_pct_5h": 65.0, "used_pct_7d": 10.0, "error": None}):
        result = check_and_rotate(threshold=80.0, home=home)
    assert result["action"] == "warning"
    assert result["switched_to"] is None


def test_select_next_skips_current(tmp_path):
    from scitex_agent_container._account.quota_watch import _select_next_account

    accounts = [
        {"name": "a", "email_address": "a@x.com", "quota_5h_used_pct": 50.0},
        {"name": "b", "email_address": "b@x.com", "quota_5h_used_pct": 10.0},
    ]
    result = _select_next_account(accounts, current_email="a@x.com")
    assert result["name"] == "b"


def test_select_next_returns_none_when_only_one(tmp_path):
    from scitex_agent_container._account.quota_watch import _select_next_account

    accounts = [{"name": "only", "email_address": "only@x.com"}]
    result = _select_next_account(accounts, current_email="only@x.com")
    assert result is None
