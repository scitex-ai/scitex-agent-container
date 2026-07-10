"""Tests for the Mode B action layer (task #13, op-2026-06-12-13).

``rotate_account`` reuses ``quota_watch._select_next_account`` (health
gate + lowest-5h-usage pick) and ``account_store.switch_account``
(credential copy + rotation audit) — the SAME primitives the periodic
``sac accounts watch-quota`` loop already relies on — skipping only
the proactive threshold/``fetch_usage`` gate, because a REACTIVE
caller already knows a rotation is warranted.

Fixtures mirror ``test_quota_watch.py``'s ``_make_home`` / `_make_store`
helpers exactly (same on-disk shapes ``_select_next_account`` /
``switch_account`` already read) so this test suite exercises the
real filesystem primitives, no mocks, and — critically — every call
passes explicit ``home=`` / ``store_dir=`` so nothing ever falls back
to the real ``Path.home()`` / SciTeX local-state cascade.

Test style (STX-TQ002 / STX-TQ007): explicit ``# Arrange`` / ``# Act``
/ ``# Assert`` markers each on their own line, in order; one logical
assertion per test.
"""

from __future__ import annotations

import json
import time
from pathlib import Path

from scitex_agent_container._account.rotate_account import (
    ROTATE_EVENT,
    RotateResult,
    rotate_account,
)

# ---------------------------------------------------------------------------
# Fixtures (mirrors test_quota_watch.py's _make_home / _make_store)
# ---------------------------------------------------------------------------


def _make_home(tmp_path: Path, email: str = "primary@example.com") -> Path:
    home = tmp_path / "home"
    home.mkdir()
    (home / ".claude").mkdir()
    (home / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"emailAddress": email}})
    )
    return home


def _make_store(tmp_path: Path, accounts: list[dict]) -> Path:
    """Populate a fake accounts store. ``_health`` control key: see
    test_quota_watch.py's identical helper for the full contract
    (``"valid"`` / ``"expired"`` / ``"absent"``)."""
    future_ms = int((time.time() + 30 * 24 * 3600) * 1000)
    past_ms = int((time.time() - 24 * 3600) * 1000)
    store = tmp_path / "store"
    store.mkdir()
    for raw in accounts:
        acct = dict(raw)
        health = acct.pop("_health", "valid")
        name = acct["name"]
        cred_dir = store / name
        cred_dir.mkdir()
        (cred_dir / "account.json").write_text(json.dumps(acct))
        if health == "absent":
            continue
        expires = future_ms if health == "valid" else past_ms
        (cred_dir / ".credentials.json").write_text(
            json.dumps({"claudeAiOauth": {"expiresAt": expires}})
        )
    return store


# ---------------------------------------------------------------------------
# Successful rotation
# ---------------------------------------------------------------------------


def test_rotate_account_switches_to_healthy_candidate(tmp_path):
    # Arrange
    home = _make_home(tmp_path)
    store = _make_store(
        tmp_path, [{"name": "backup", "email_address": "backup@example.com"}]
    )
    # Act
    result = rotate_account(reason="test", store_dir=store, home=home)
    # Assert
    assert result.action == "rotated"


def test_rotate_account_switched_to_names_the_target_account(tmp_path):
    # Arrange
    home = _make_home(tmp_path)
    store = _make_store(
        tmp_path, [{"name": "backup", "email_address": "backup@example.com"}]
    )
    # Act
    result = rotate_account(reason="test", store_dir=store, home=home)
    # Assert
    assert result.switched_to == "backup"


def test_rotate_account_from_account_records_current_email(tmp_path):
    # Arrange
    home = _make_home(tmp_path, email="primary@example.com")
    store = _make_store(
        tmp_path, [{"name": "backup", "email_address": "backup@example.com"}]
    )
    # Act
    result = rotate_account(reason="test", store_dir=store, home=home)
    # Assert
    assert result.from_account == "primary@example.com"


def test_rotate_account_message_carries_the_reason(tmp_path):
    # Arrange
    home = _make_home(tmp_path)
    store = _make_store(
        tmp_path, [{"name": "backup", "email_address": "backup@example.com"}]
    )
    # Act
    result = rotate_account(
        reason="reactive http_429 (pattern='\\\\b429\\\\b')", store_dir=store, home=home
    )
    # Assert
    assert "http_429" in result.message


def test_rotate_account_actually_copies_credential_file(tmp_path):
    # Arrange
    home = _make_home(tmp_path)
    store = _make_store(
        tmp_path, [{"name": "backup", "email_address": "backup@example.com"}]
    )
    # Act
    rotate_account(reason="test", store_dir=store, home=home)
    # Assert — switch_account's real effect: the live credentials file
    # now exists under home/.claude/ (copied from the backup snapshot).
    assert (home / ".claude" / ".credentials.json").is_file()


def test_rotate_account_picks_lowest_quota_among_multiple_healthy(tmp_path):
    # Arrange
    home = _make_home(tmp_path)
    store = _make_store(
        tmp_path,
        [
            {"name": "high", "email_address": "high@example.com", "quota_5h_used_pct": 70.0},
            {"name": "low", "email_address": "low@example.com", "quota_5h_used_pct": 5.0},
        ],
    )
    # Act
    result = rotate_account(reason="test", store_dir=store, home=home)
    # Assert
    assert result.switched_to == "low"


def test_rotate_account_writes_reactive_rotate_audit_event(tmp_path):
    # Arrange
    home = _make_home(tmp_path)
    store = _make_store(
        tmp_path, [{"name": "backup", "email_address": "backup@example.com"}]
    )
    # Act
    rotate_account(reason="test", store_dir=store, home=home)
    audit_lines = (store / "rotation-audit.jsonl").read_text(
        encoding="utf-8"
    ).strip().splitlines()
    last_record = json.loads(audit_lines[-1])
    # Assert
    assert last_record["event"] == ROTATE_EVENT


# ---------------------------------------------------------------------------
# No healthy candidate — stay put (never rotate onto a dead/absent token)
# ---------------------------------------------------------------------------


def test_rotate_account_only_current_account_stored_stays_put(tmp_path):
    # Arrange
    home = _make_home(tmp_path, email="primary@example.com")
    store = _make_store(
        tmp_path, [{"name": "primary", "email_address": "primary@example.com"}]
    )
    # Act
    result = rotate_account(reason="test", store_dir=store, home=home)
    # Assert
    assert result.action == "no_accounts"


def test_rotate_account_expired_only_candidate_stays_put(tmp_path):
    # Arrange — the 2026-07-06 regression shape: an EXPIRED account must
    # never be selected, even as the only candidate.
    home = _make_home(tmp_path)
    store = _make_store(
        tmp_path,
        [
            {
                "name": "expired-backup",
                "email_address": "backup@example.com",
                "_health": "expired",
            }
        ],
    )
    # Act
    result = rotate_account(reason="test", store_dir=store, home=home)
    # Assert
    assert result.action == "no_accounts"


def test_rotate_account_no_candidate_does_not_touch_switched_to(tmp_path):
    # Arrange
    home = _make_home(tmp_path)
    store = tmp_path / "empty_store"
    store.mkdir()
    # Act
    result = rotate_account(reason="test", store_dir=store, home=home)
    # Assert
    assert result.switched_to is None


def test_rotate_account_no_candidate_message_says_staying_put(tmp_path):
    # Arrange
    home = _make_home(tmp_path)
    store = tmp_path / "empty_store"
    store.mkdir()
    # Act
    result = rotate_account(reason="test", store_dir=store, home=home)
    # Assert
    assert "staying put" in result.message


# ---------------------------------------------------------------------------
# RotateResult — dataclass contract
# ---------------------------------------------------------------------------


def test_rotate_account_returns_rotate_result_instance(tmp_path):
    # Arrange
    home = _make_home(tmp_path)
    store = tmp_path / "empty_store"
    store.mkdir()
    # Act
    result = rotate_account(reason="test", store_dir=store, home=home)
    # Assert
    assert isinstance(result, RotateResult)
